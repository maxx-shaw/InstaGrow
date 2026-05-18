import datetime
import logging
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

import instagram_client as ig
from database import (
    get_db, Account, FollowRecord, Blacklist, Whitelist, ActivityLog, FollowQueue
)

logger = logging.getLogger("instagrow.follows")
router = APIRouter(prefix="/follows", tags=["follows"])


def _require_login():
    if not ig.is_logged_in():
        raise HTTPException(401, "Not logged in to Instagram")


def _active_account(db: Session) -> Account:
    acc = db.query(Account).filter(Account.is_active == True).first()
    if not acc:
        raise HTTPException(404, "No active account")
    return acc


# ---------------------------------------------------------------------------
# List follows
# ---------------------------------------------------------------------------

@router.get("/")
def list_follows(
    filter: str = Query("all", description="all | not_following_back | following_back | unfollowed"),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    account = _active_account(db)
    q = db.query(FollowRecord).filter(FollowRecord.account_id == account.id)

    if filter == "not_following_back":
        q = q.filter(FollowRecord.is_active == True, FollowRecord.followed_back != True)
    elif filter == "following_back":
        q = q.filter(FollowRecord.is_active == True, FollowRecord.followed_back == True)
    elif filter == "unfollowed":
        q = q.filter(FollowRecord.is_active == False)
    else:
        q = q.filter(FollowRecord.is_active == True)

    total = q.count()
    records = (
        q.order_by(FollowRecord.followed_at.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )

    return {
        "total": total,
        "page": page,
        "per_page": per_page,
        "records": [_serialize_record(r) for r in records],
    }


def _serialize_record(r: FollowRecord):
    return {
        "id": r.id,
        "target_user_id": r.target_user_id,
        "target_username": r.target_username,
        "target_full_name": r.target_full_name,
        "target_profile_pic": r.target_profile_pic,
        "followed_at": r.followed_at.isoformat() if r.followed_at else None,
        "unfollowed_at": r.unfollowed_at.isoformat() if r.unfollowed_at else None,
        "followed_back": r.followed_back,
        "followed_back_checked_at": r.followed_back_checked_at.isoformat() if r.followed_back_checked_at else None,
        "source": r.source,
        "is_active": r.is_active,
        "days_since_follow": (
            (datetime.datetime.utcnow() - r.followed_at).days
            if r.followed_at else None
        ),
    }


# ---------------------------------------------------------------------------
# Sync following list from Instagram
# ---------------------------------------------------------------------------

@router.post("/sync")
def sync_following(db: Session = Depends(get_db)):
    """Pull current following list from Instagram and update the DB."""
    _require_login()
    account = _active_account(db)
    my_id = ig.get_my_user_id()

    following_map = ig.get_following(my_id, amount=0)
    follower_map = ig.get_followers(my_id, amount=0)
    follower_ids = set(str(uid) for uid in follower_map.keys())

    now = datetime.datetime.utcnow()
    added = updated = 0

    for uid, user in following_map.items():
        uid_str = str(uid)
        record = db.query(FollowRecord).filter(
            FollowRecord.account_id == account.id,
            FollowRecord.target_user_id == uid_str,
        ).first()

        follows_back = uid_str in follower_ids

        if not record:
            record = FollowRecord(
                account_id=account.id,
                target_user_id=uid_str,
                target_username=user.username,
                target_full_name=user.full_name,
                target_profile_pic=str(user.profile_pic_url) if user.profile_pic_url else None,
                followed_at=now,
                followed_back=follows_back,
                followed_back_checked_at=now,
                source="sync",
                is_active=True,
            )
            db.add(record)
            added += 1
        else:
            record.target_username = user.username
            record.target_full_name = user.full_name
            if user.profile_pic_url:
                record.target_profile_pic = str(user.profile_pic_url)
            record.followed_back = follows_back
            record.followed_back_checked_at = now
            record.is_active = True
            record.unfollowed_at = None
            updated += 1

    # Mark accounts we no longer follow as inactive
    ig_following_ids = set(str(uid) for uid in following_map.keys())
    stale = db.query(FollowRecord).filter(
        FollowRecord.account_id == account.id,
        FollowRecord.is_active == True,
    ).all()
    for r in stale:
        if r.target_user_id not in ig_following_ids:
            r.is_active = False
            if not r.unfollowed_at:
                r.unfollowed_at = now

    db.commit()

    log = ActivityLog(
        account_id=account.id,
        action_type="sync",
        details=f"Sync complete: {added} added, {updated} updated",
    )
    db.add(log)
    db.commit()

    return {"added": added, "updated": updated, "total_following": len(following_map)}


# ---------------------------------------------------------------------------
# Manual unfollow
# ---------------------------------------------------------------------------

class UnfollowRequest(BaseModel):
    user_ids: list[str]


@router.post("/unfollow")
def manual_unfollow(req: UnfollowRequest, db: Session = Depends(get_db)):
    _require_login()
    account = _active_account(db)
    results = []

    for uid in req.user_ids:
        record = db.query(FollowRecord).filter(
            FollowRecord.account_id == account.id,
            FollowRecord.target_user_id == uid,
            FollowRecord.is_active == True,
        ).first()
        if not record:
            results.append({"user_id": uid, "success": False, "error": "Not found in follow list"})
            continue
        # Block unfollow if whitelisted
        wl = db.query(Whitelist).filter(
            Whitelist.account_id == account.id,
            Whitelist.target_user_id == uid,
        ).first()
        if wl:
            results.append({"user_id": uid, "success": False, "error": f"@{record.target_username} is whitelisted — remove from whitelist first"})
            continue
        try:
            ig.unfollow_user(uid, delay_min=10, delay_max=30)
            record.unfollowed_at = datetime.datetime.utcnow()
            record.is_active = False
            db.commit()
            db_log = ActivityLog(
                account_id=account.id,
                action_type="unfollow",
                target_username=record.target_username,
                target_user_id=uid,
                details="Manual unfollow",
            )
            db.add(db_log)
            db.commit()
            results.append({"user_id": uid, "success": True})
        except Exception as e:
            results.append({"user_id": uid, "success": False, "error": str(e)})

    return {"results": results}


# ---------------------------------------------------------------------------
# Manual follow
# ---------------------------------------------------------------------------

class FollowRequest(BaseModel):
    username: str


@router.post("/follow")
def manual_follow(req: FollowRequest, db: Session = Depends(get_db)):
    _require_login()
    account = _active_account(db)

    try:
        user = ig.get_user_info_by_username(req.username)
        uid_str = str(user.pk)

        # Check blacklist
        bl = db.query(Blacklist).filter(
            Blacklist.account_id == account.id,
            Blacklist.target_user_id == uid_str,
        ).first()
        if bl:
            raise HTTPException(400, f"@{req.username} is on your blacklist")

        ig.follow_user(uid_str, delay_min=2, delay_max=5)

        # Upsert follow record
        record = db.query(FollowRecord).filter(
            FollowRecord.account_id == account.id,
            FollowRecord.target_user_id == uid_str,
        ).first()
        now = datetime.datetime.utcnow()
        if record:
            record.is_active = True
            record.unfollowed_at = None
            record.followed_at = now
        else:
            record = FollowRecord(
                account_id=account.id,
                target_user_id=uid_str,
                target_username=user.username,
                target_full_name=user.full_name,
                target_profile_pic=str(user.profile_pic_url) if user.profile_pic_url else None,
                followed_at=now,
                source="manual",
                is_active=True,
            )
            db.add(record)

        log = ActivityLog(
            account_id=account.id,
            action_type="follow",
            target_username=user.username,
            target_user_id=uid_str,
            details="Manual follow",
        )
        db.add(log)
        db.commit()

        return {"success": True, "username": user.username, "user_id": uid_str}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, str(e))


# ---------------------------------------------------------------------------
# Follow queue
# ---------------------------------------------------------------------------

@router.get("/queue")
def get_queue(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    account = _active_account(db)
    q = db.query(FollowQueue).filter(FollowQueue.account_id == account.id)
    total = q.count()
    items = (
        q.order_by(FollowQueue.mutual_count.desc(), FollowQueue.added_at.asc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )
    return {
        "total": total,
        "items": [
            {
                "id": i.id,
                "target_user_id": i.target_user_id,
                "target_username": i.target_username,
                "target_full_name": i.target_full_name,
                "target_profile_pic": i.target_profile_pic,
                "mutual_count": i.mutual_count,
                "source": i.source,
                "added_at": i.added_at.isoformat() if i.added_at else None,
            }
            for i in items
        ],
    }


@router.delete("/queue/{item_id}")
def remove_from_queue(item_id: int, db: Session = Depends(get_db)):
    account = _active_account(db)
    item = db.query(FollowQueue).filter(
        FollowQueue.id == item_id,
        FollowQueue.account_id == account.id,
    ).first()
    if not item:
        raise HTTPException(404, "Queue item not found")
    db.delete(item)
    db.commit()
    return {"success": True}


@router.delete("/queue")
def clear_queue(db: Session = Depends(get_db)):
    account = _active_account(db)
    db.query(FollowQueue).filter(FollowQueue.account_id == account.id).delete()
    db.commit()
    return {"success": True}
