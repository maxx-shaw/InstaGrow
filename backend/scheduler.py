"""
APScheduler background jobs:
  - auto_unfollow_job : unfollow people who haven't followed back after N days
  - auto_follow_job   : follow queued accounts one-at-a-time throughout the day
  - scan_fof_job      : scan followers-of-following to populate the follow queue
  - daily_reset_job   : reset daily counters at midnight
"""
import datetime
import logging
import random
import time

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger

import instagram_client as ig
import events as ev
from database import (
    SessionLocal,
    Account,
    FollowRecord,
    FollowQueue,
    Blacklist,
    Whitelist,
    AutomationSettings,
    ActivityLog,
)

logger = logging.getLogger("socialreach.scheduler")
scheduler = BackgroundScheduler(timezone="UTC")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_active_account(db):
    return db.query(Account).filter(Account.is_active == True).first()


def _get_settings(db, account_id: int) -> AutomationSettings:
    s = db.query(AutomationSettings).filter(
        AutomationSettings.account_id == account_id
    ).first()
    if not s:
        s = AutomationSettings(account_id=account_id)
        db.add(s)
        db.commit()
        db.refresh(s)
    return s


def _log(db, account_id, action_type, target_username=None,
         target_user_id=None, details=None, success=True):
    entry = ActivityLog(
        account_id=account_id,
        action_type=action_type,
        target_username=target_username,
        target_user_id=target_user_id,
        details=details,
        success=success,
    )
    db.add(entry)
    db.commit()


def _within_active_hours(s: AutomationSettings) -> bool:
    now_hour = datetime.datetime.utcnow().hour
    return s.active_hours_start <= now_hour < s.active_hours_end


def _reset_daily_counts_if_needed(db, s: AutomationSettings):
    today = datetime.date.today().isoformat()
    if s.daily_reset_date != today:
        s.follows_today = 0
        s.unfollows_today = 0
        s.daily_reset_date = today
        db.commit()


def _is_blacklisted(db, account_id: int, target_user_id: str) -> bool:
    return db.query(Blacklist).filter(
        Blacklist.account_id == account_id,
        Blacklist.target_user_id == target_user_id,
    ).first() is not None


def _is_whitelisted(db, account_id: int, target_user_id: str) -> bool:
    return db.query(Whitelist).filter(
        Whitelist.account_id == account_id,
        Whitelist.target_user_id == target_user_id,
    ).first() is not None


# ---------------------------------------------------------------------------
# Job: Auto-unfollow
# ---------------------------------------------------------------------------

def auto_unfollow_job():
    if not ig.is_logged_in():
        return

    db = SessionLocal()
    try:
        account = _get_active_account(db)
        if not account:
            return

        s = _get_settings(db, account.id)
        if not s.auto_unfollow_enabled:
            return
        if not _within_active_hours(s):
            return

        _reset_daily_counts_if_needed(db, s)

        if s.unfollows_today >= s.unfollow_per_day:
            logger.info("Daily unfollow limit reached (%d)", s.unfollow_per_day)
            return

        cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=s.unfollow_after_days)

        query = db.query(FollowRecord).filter(
            FollowRecord.account_id == account.id,
            FollowRecord.is_active == True,
            FollowRecord.followed_at <= cutoff,
            FollowRecord.unfollowed_at == None,
        )
        if s.unfollow_only_non_followers:
            query = query.filter(FollowRecord.followed_back != True)

        # Exclude whitelisted accounts
        whitelist_ids = {
            r.target_user_id
            for r in db.query(Whitelist.target_user_id)
            .filter(Whitelist.account_id == account.id)
            .all()
        }
        candidates = [
            r for r in query.order_by(FollowRecord.followed_at.asc()).all()
            if r.target_user_id not in whitelist_ids
        ]

        remaining = s.unfollow_per_day - s.unfollows_today
        batch = candidates[:remaining]

        logger.info("Auto-unfollow: %d candidates, doing up to %d", len(candidates), len(batch))

        ev.job_start("unfollow", len(batch))

        for idx, record in enumerate(batch, 1):
            if not ig.is_logged_in():
                break
            delay = random.randint(30, 90)
            ev.job_action("unfollow", record.target_username, True, idx, delay_seconds=delay)
            try:
                ig.unfollow_user(record.target_username, delay_min=30, delay_max=90)
                record.unfollowed_at = datetime.datetime.utcnow()
                record.is_active = False
                s.unfollows_today += 1
                db.commit()
                ev.job_action("unfollow", record.target_username, True, idx, delay_seconds=0)
                _log(db, account.id, "unfollow",
                     target_username=record.target_username,
                     target_user_id=record.target_user_id,
                     details=f"Auto-unfollow after {s.unfollow_after_days}d")
            except Exception as e:
                ev.job_action("unfollow", record.target_username, False, idx)
                _log(db, account.id, "error",
                     target_username=record.target_username,
                     details=str(e), success=False)
                logger.error("Unfollow error for %s: %s", record.target_username, e)
                time.sleep(60)

        s.last_unfollow_run = datetime.datetime.utcnow()
        db.commit()
        ev.job_complete("unfollow")

    finally:
        db.close()


# ---------------------------------------------------------------------------
# Job: Auto-follow (one account per run from the queue)
# ---------------------------------------------------------------------------

def auto_follow_job():
    if not ig.is_logged_in():
        return

    db = SessionLocal()
    try:
        account = _get_active_account(db)
        if not account:
            return

        s = _get_settings(db, account.id)
        if not s.auto_follow_enabled:
            return
        if not _within_active_hours(s):
            return

        _reset_daily_counts_if_needed(db, s)

        if s.follows_today >= s.follow_per_day:
            logger.info("Daily follow limit reached (%d)", s.follow_per_day)
            return

        # Pick one item from the queue (highest mutual count first)
        item = (
            db.query(FollowQueue)
            .filter(FollowQueue.account_id == account.id)
            .order_by(FollowQueue.mutual_count.desc(), FollowQueue.added_at.asc())
            .first()
        )
        if not item:
            logger.info("Follow queue empty — nothing to follow")
            return

        # Skip blacklisted
        if _is_blacklisted(db, account.id, item.target_user_id):
            db.delete(item)
            db.commit()
            return

        # Skip already following
        existing = db.query(FollowRecord).filter(
            FollowRecord.account_id == account.id,
            FollowRecord.target_user_id == item.target_user_id,
            FollowRecord.is_active == True,
        ).first()
        if existing:
            db.delete(item)
            db.commit()
            return

        queue_remaining = db.query(FollowQueue).filter(
            FollowQueue.account_id == account.id
        ).count()
        ev.job_start("follow", min(s.follow_per_day - s.follows_today, queue_remaining))
        ev.job_action("follow", item.target_username, True, s.follows_today + 1,
                      delay_seconds=s.follow_delay_max)

        try:
            ig.follow_user(
                item.target_username,
                delay_min=s.follow_delay_min,
                delay_max=s.follow_delay_max,
            )

            record = FollowRecord(
                account_id=account.id,
                target_user_id=item.target_user_id,
                target_username=item.target_username,
                target_full_name=item.target_full_name,
                target_profile_pic=item.target_profile_pic,
                followed_at=datetime.datetime.utcnow(),
                source=item.source,
                is_active=True,
            )
            db.add(record)
            db.delete(item)
            s.follows_today += 1
            s.last_follow_run = datetime.datetime.utcnow()
            db.commit()

            ev.job_action("follow", item.target_username, True, s.follows_today, delay_seconds=0)
            ev.job_complete("follow")
            _log(db, account.id, "follow",
                 target_username=item.target_username,
                 target_user_id=item.target_user_id,
                 details=f"Auto-follow, mutuals={item.mutual_count}")

        except Exception as e:
            _log(db, account.id, "error",
                 target_username=item.target_username,
                 details=str(e), success=False)
            logger.error("Follow error for %s: %s", item.target_username, e)
            # Remove from queue so we don't keep retrying a broken account
            db.delete(item)
            db.commit()

    finally:
        db.close()


# ---------------------------------------------------------------------------
# Job: Scan followers-of-following to build the follow queue
# ---------------------------------------------------------------------------

def scan_fof_job():
    """
    Pick a random sample of accounts I follow, get their followers,
    filter by mutual-follow count >= threshold, add to queue.
    """
    if not ig.is_logged_in():
        return

    db = SessionLocal()
    try:
        account = _get_active_account(db)
        if not account:
            return

        s = _get_settings(db, account.id)

        logger.info("Starting FOF scan for account %s", account.username)

        my_user_id = ig.get_my_user_id()
        if not my_user_id:
            return

        # Get my following list
        following_map = ig.get_following(my_user_id, amount=0)
        following_ids = set(str(uid) for uid in following_map.keys())

        if not following_ids:
            return

        # Sample up to scan_depth accounts to probe
        sample_ids = random.sample(
            list(following_ids),
            min(s.scan_depth, len(following_ids)),
        )

        # Collect candidate user info
        candidates: dict[str, dict] = {}   # user_id → {username, full_name, pic, count}

        # Get blacklist + already-following + already-queued sets for fast lookup
        blacklisted_ids = {
            r.target_user_id
            for r in db.query(Blacklist.target_user_id)
            .filter(Blacklist.account_id == account.id)
            .all()
        }
        already_following_ids = {
            r.target_user_id
            for r in db.query(FollowRecord.target_user_id)
            .filter(
                FollowRecord.account_id == account.id,
                FollowRecord.is_active == True,
            )
            .all()
        }
        queued_ids = {
            r.target_user_id
            for r in db.query(FollowQueue.target_user_id)
            .filter(FollowQueue.account_id == account.id)
            .all()
        }

        skip_ids = blacklisted_ids | already_following_ids | queued_ids | following_ids
        skip_ids.add(my_user_id)

        for probe_id in sample_ids:
            try:
                followers_map = ig.get_followers(probe_id, amount=200)
                for uid, user in followers_map.items():
                    uid_str = str(uid)
                    if uid_str in skip_ids:
                        continue
                    if uid_str not in candidates:
                        candidates[uid_str] = {
                            "username": user.username,
                            "full_name": user.full_name,
                            "profile_pic": str(user.profile_pic_url) if user.profile_pic_url else None,
                            "count": 0,
                        }
                    candidates[uid_str]["count"] += 1
                time.sleep(random.uniform(3, 8))
            except Exception as e:
                logger.warning("Error fetching followers of %s: %s", probe_id, e)
                continue

        # Filter by mutual threshold and add to queue
        added = 0
        for uid_str, info in candidates.items():
            if info["count"] < s.mutual_follows_threshold:
                continue
            entry = FollowQueue(
                account_id=account.id,
                target_user_id=uid_str,
                target_username=info["username"],
                target_full_name=info["full_name"],
                target_profile_pic=info["profile_pic"],
                mutual_count=info["count"],
                source="auto_fof",
            )
            db.add(entry)
            added += 1

        s.last_scan_run = datetime.datetime.utcnow()
        db.commit()

        _log(db, account.id, "scan",
             details=f"FOF scan complete. Added {added} to queue (probed {len(sample_ids)} accounts)")
        logger.info("FOF scan done: added %d to queue", added)

    finally:
        db.close()


# ---------------------------------------------------------------------------
# Job: Check follow-back status for recent follows
# ---------------------------------------------------------------------------

def check_follow_back_job():
    """Update followed_back status for follows made in the last 30 days."""
    if not ig.is_logged_in():
        return

    db = SessionLocal()
    try:
        account = _get_active_account(db)
        if not account:
            return

        cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=30)
        records = (
            db.query(FollowRecord)
            .filter(
                FollowRecord.account_id == account.id,
                FollowRecord.is_active == True,
                FollowRecord.followed_at >= cutoff,
                FollowRecord.followed_back == None,
            )
            .limit(50)
            .all()
        )

        if not records:
            return

        my_user_id = ig.get_my_user_id()
        try:
            followers_map = ig.get_followers(my_user_id, amount=0)
            follower_ids = set(str(uid) for uid in followers_map.keys())
        except Exception as e:
            logger.error("Could not fetch followers for check_follow_back: %s", e)
            return

        now = datetime.datetime.utcnow()
        for record in records:
            record.followed_back = record.target_user_id in follower_ids
            record.followed_back_checked_at = now

        db.commit()
        logger.info("Updated follow-back status for %d records", len(records))

    finally:
        db.close()


# ---------------------------------------------------------------------------
# Job: Daily reset
# ---------------------------------------------------------------------------

def daily_reset_job():
    db = SessionLocal()
    try:
        account = _get_active_account(db)
        if not account:
            return
        s = _get_settings(db, account.id)
        s.follows_today = 0
        s.unfollows_today = 0
        s.daily_reset_date = datetime.date.today().isoformat()
        db.commit()
        logger.info("Daily counters reset")
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Scheduler setup
# ---------------------------------------------------------------------------

def start_scheduler():
    # Auto-unfollow: check every 30 minutes
    scheduler.add_job(
        auto_unfollow_job,
        trigger=IntervalTrigger(minutes=30),
        id="auto_unfollow",
        replace_existing=True,
        max_instances=1,
    )

    # Auto-follow: check every N minutes (spaced throughout the day)
    # Fires frequently; the job itself checks the daily limit and active hours.
    scheduler.add_job(
        auto_follow_job,
        trigger=IntervalTrigger(minutes=15),
        id="auto_follow",
        replace_existing=True,
        max_instances=1,
    )

    # FOF scan: once every 6 hours
    scheduler.add_job(
        scan_fof_job,
        trigger=IntervalTrigger(hours=6),
        id="scan_fof",
        replace_existing=True,
        max_instances=1,
    )

    # Check follow-back status: every 4 hours
    scheduler.add_job(
        check_follow_back_job,
        trigger=IntervalTrigger(hours=4),
        id="check_follow_back",
        replace_existing=True,
        max_instances=1,
    )

    # Daily reset at midnight UTC
    scheduler.add_job(
        daily_reset_job,
        trigger=CronTrigger(hour=0, minute=0),
        id="daily_reset",
        replace_existing=True,
    )

    scheduler.start()
    logger.info("Scheduler started")


def trigger_scan_now():
    """Manually trigger a FOF scan."""
    scheduler.add_job(
        scan_fof_job,
        id="scan_fof_manual",
        replace_existing=True,
        max_instances=1,
    )


def trigger_unfollow_now():
    """Manually trigger the unfollow job."""
    scheduler.add_job(
        auto_unfollow_job,
        id="auto_unfollow_manual",
        replace_existing=True,
        max_instances=1,
    )


def trigger_check_follow_back_now():
    scheduler.add_job(
        check_follow_back_job,
        id="check_follow_back_manual",
        replace_existing=True,
        max_instances=1,
    )
