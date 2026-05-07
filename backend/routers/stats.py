import datetime
import logging
from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from database import (
    get_db, Account, FollowRecord, Blacklist,
    FollowQueue, ActivityLog, AutomationSettings
)

logger = logging.getLogger("instagrow.stats")
router = APIRouter(prefix="/stats", tags=["stats"])


def _active_account(db: Session):
    return db.query(Account).filter(Account.is_active == True).first()


@router.get("/dashboard")
def dashboard(db: Session = Depends(get_db)):
    account = _active_account(db)
    if not account:
        return {"error": "No active account"}

    aid = account.id
    now = datetime.datetime.utcnow()
    week_ago = now - datetime.timedelta(days=7)

    total_following = db.query(FollowRecord).filter(
        FollowRecord.account_id == aid,
        FollowRecord.is_active == True,
    ).count()

    not_following_back = db.query(FollowRecord).filter(
        FollowRecord.account_id == aid,
        FollowRecord.is_active == True,
        FollowRecord.followed_back == False,
    ).count()

    following_back = db.query(FollowRecord).filter(
        FollowRecord.account_id == aid,
        FollowRecord.is_active == True,
        FollowRecord.followed_back == True,
    ).count()

    unfollowed_this_week = db.query(FollowRecord).filter(
        FollowRecord.account_id == aid,
        FollowRecord.is_active == False,
        FollowRecord.unfollowed_at >= week_ago,
    ).count()

    followed_this_week = db.query(FollowRecord).filter(
        FollowRecord.account_id == aid,
        FollowRecord.followed_at >= week_ago,
    ).count()

    blacklist_count = db.query(Blacklist).filter(
        Blacklist.account_id == aid
    ).count()

    queue_count = db.query(FollowQueue).filter(
        FollowQueue.account_id == aid
    ).count()

    # Automation settings for daily counts
    settings = db.query(AutomationSettings).filter(
        AutomationSettings.account_id == aid
    ).first()

    return {
        "total_following": total_following,
        "not_following_back": not_following_back,
        "following_back": following_back,
        "unfollowed_this_week": unfollowed_this_week,
        "followed_this_week": followed_this_week,
        "blacklist_count": blacklist_count,
        "queue_count": queue_count,
        "follows_today": settings.follows_today if settings else 0,
        "unfollows_today": settings.unfollows_today if settings else 0,
        "follow_limit_today": settings.follow_per_day if settings else 0,
        "unfollow_limit_today": settings.unfollow_per_day if settings else 0,
        "auto_follow_enabled": settings.auto_follow_enabled if settings else False,
        "auto_unfollow_enabled": settings.auto_unfollow_enabled if settings else False,
    }


@router.get("/activity")
def activity_log(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    action_type: str = Query("all"),
    db: Session = Depends(get_db),
):
    account = _active_account(db)
    if not account:
        return {"total": 0, "logs": []}

    q = db.query(ActivityLog).filter(ActivityLog.account_id == account.id)
    if action_type != "all":
        q = q.filter(ActivityLog.action_type == action_type)

    total = q.count()
    logs = (
        q.order_by(ActivityLog.timestamp.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )

    return {
        "total": total,
        "page": page,
        "per_page": per_page,
        "logs": [
            {
                "id": l.id,
                "action_type": l.action_type,
                "target_username": l.target_username,
                "target_user_id": l.target_user_id,
                "details": l.details,
                "success": l.success,
                "timestamp": l.timestamp.isoformat() if l.timestamp else None,
            }
            for l in logs
        ],
    }


@router.get("/follow-trend")
def follow_trend(days: int = Query(14, ge=1, le=90), db: Session = Depends(get_db)):
    """Return daily follow/unfollow counts for the last N days."""
    account = _active_account(db)
    if not account:
        return {"data": []}

    now = datetime.datetime.utcnow().date()
    result = []

    for i in range(days - 1, -1, -1):
        day = now - datetime.timedelta(days=i)
        day_start = datetime.datetime.combine(day, datetime.time.min)
        day_end = datetime.datetime.combine(day, datetime.time.max)

        follows = db.query(FollowRecord).filter(
            FollowRecord.account_id == account.id,
            FollowRecord.followed_at >= day_start,
            FollowRecord.followed_at <= day_end,
        ).count()

        unfollows = db.query(FollowRecord).filter(
            FollowRecord.account_id == account.id,
            FollowRecord.unfollowed_at >= day_start,
            FollowRecord.unfollowed_at <= day_end,
        ).count()

        result.append({
            "date": day.isoformat(),
            "follows": follows,
            "unfollows": unfollows,
        })

    return {"data": result}
