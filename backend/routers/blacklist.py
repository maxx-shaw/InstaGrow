import datetime
import logging
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

import instagram_client as ig
from database import get_db, Account, Blacklist, FollowQueue, ActivityLog

logger = logging.getLogger("socialreach.blacklist")
router = APIRouter(prefix="/blacklist", tags=["blacklist"])


def _active_account(db: Session) -> Account:
    acc = db.query(Account).filter(Account.is_active == True).first()
    if not acc:
        raise HTTPException(404, "No active account")
    return acc


@router.get("/")
def list_blacklist(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    account = _active_account(db)
    q = db.query(Blacklist).filter(Blacklist.account_id == account.id)
    total = q.count()
    items = (
        q.order_by(Blacklist.created_at.desc())
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
                "reason": i.reason,
                "created_at": i.created_at.isoformat() if i.created_at else None,
            }
            for i in items
        ],
    }


class AddBlacklistRequest(BaseModel):
    username: str
    reason: str = ""


@router.post("/")
def add_to_blacklist(req: AddBlacklistRequest, db: Session = Depends(get_db)):
    account = _active_account(db)

    # Try to look up user ID if logged in
    user_id = None
    if ig.is_logged_in():
        try:
            user = ig.get_user_info_by_username(req.username)
            user_id = str(user.pk)
        except Exception:
            pass

    if not user_id:
        # Use username as a placeholder ID (prefixed so we can distinguish)
        user_id = f"username:{req.username}"

    # Check if already blacklisted
    existing = db.query(Blacklist).filter(
        Blacklist.account_id == account.id,
        Blacklist.target_user_id == user_id,
    ).first()
    if existing:
        raise HTTPException(400, f"@{req.username} is already blacklisted")

    entry = Blacklist(
        account_id=account.id,
        target_user_id=user_id,
        target_username=req.username,
        reason=req.reason,
    )
    db.add(entry)

    # Also remove from follow queue if present
    db.query(FollowQueue).filter(
        FollowQueue.account_id == account.id,
        FollowQueue.target_user_id == user_id,
    ).delete()

    log = ActivityLog(
        account_id=account.id,
        action_type="blacklist",
        target_username=req.username,
        target_user_id=user_id,
        details=f"Added to blacklist: {req.reason}",
    )
    db.add(log)
    db.commit()

    return {
        "id": entry.id,
        "target_user_id": user_id,
        "target_username": req.username,
        "reason": req.reason,
    }


@router.delete("/{entry_id}")
def remove_from_blacklist(entry_id: int, db: Session = Depends(get_db)):
    account = _active_account(db)
    entry = db.query(Blacklist).filter(
        Blacklist.id == entry_id,
        Blacklist.account_id == account.id,
    ).first()
    if not entry:
        raise HTTPException(404, "Blacklist entry not found")
    db.delete(entry)
    db.commit()
    return {"success": True}


@router.delete("/username/{username}")
def remove_by_username(username: str, db: Session = Depends(get_db)):
    account = _active_account(db)
    entry = db.query(Blacklist).filter(
        Blacklist.account_id == account.id,
        Blacklist.target_username == username,
    ).first()
    if not entry:
        raise HTTPException(404, "Not found in blacklist")
    db.delete(entry)
    db.commit()
    return {"success": True}
