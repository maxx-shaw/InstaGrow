import datetime
import logging
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

import instagram_client as ig
from database import get_db, Account, Whitelist, ActivityLog

logger = logging.getLogger("socialreach.whitelist")
router = APIRouter(prefix="/whitelist", tags=["whitelist"])


def _active_account(db: Session) -> Account:
    acc = db.query(Account).filter(Account.is_active == True).first()
    if not acc:
        raise HTTPException(404, "No active account")
    return acc


@router.get("/")
def list_whitelist(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    account = _active_account(db)
    q = db.query(Whitelist).filter(Whitelist.account_id == account.id)
    total = q.count()
    items = (
        q.order_by(Whitelist.created_at.desc())
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


class AddWhitelistRequest(BaseModel):
    username: str
    reason: str = ""


@router.post("/")
def add_to_whitelist(req: AddWhitelistRequest, db: Session = Depends(get_db)):
    account = _active_account(db)

    user_id = None
    if ig.is_logged_in():
        try:
            user = ig.get_user_info_by_username(req.username)
            user_id = str(user.pk)
        except Exception:
            pass

    if not user_id:
        user_id = f"username:{req.username}"

    existing = db.query(Whitelist).filter(
        Whitelist.account_id == account.id,
        Whitelist.target_user_id == user_id,
    ).first()
    if existing:
        raise HTTPException(400, f"@{req.username} is already whitelisted")

    entry = Whitelist(
        account_id=account.id,
        target_user_id=user_id,
        target_username=req.username,
        reason=req.reason,
    )
    db.add(entry)

    log = ActivityLog(
        account_id=account.id,
        action_type="whitelist",
        target_username=req.username,
        target_user_id=user_id,
        details=f"Added to whitelist: {req.reason}" if req.reason else "Added to whitelist",
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
def remove_from_whitelist(entry_id: int, db: Session = Depends(get_db)):
    account = _active_account(db)
    entry = db.query(Whitelist).filter(
        Whitelist.id == entry_id,
        Whitelist.account_id == account.id,
    ).first()
    if not entry:
        raise HTTPException(404, "Whitelist entry not found")
    db.delete(entry)
    db.commit()
    return {"success": True}
