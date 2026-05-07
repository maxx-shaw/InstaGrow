import logging
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import Optional

from database import get_db, Account, AutomationSettings
import scheduler as sched
import instagram_client as ig

logger = logging.getLogger("instagrow.automation")
router = APIRouter(prefix="/automation", tags=["automation"])


def _active_account(db: Session) -> Account:
    acc = db.query(Account).filter(Account.is_active == True).first()
    if not acc:
        raise HTTPException(404, "No active account")
    return acc


def _get_or_create_settings(db: Session, account_id: int) -> AutomationSettings:
    s = db.query(AutomationSettings).filter(
        AutomationSettings.account_id == account_id
    ).first()
    if not s:
        s = AutomationSettings(account_id=account_id)
        db.add(s)
        db.commit()
        db.refresh(s)
    return s


def _serialize_settings(s: AutomationSettings) -> dict:
    return {
        "auto_unfollow_enabled": s.auto_unfollow_enabled,
        "unfollow_after_days": s.unfollow_after_days,
        "unfollow_per_day": s.unfollow_per_day,
        "unfollow_only_non_followers": s.unfollow_only_non_followers,
        "auto_follow_enabled": s.auto_follow_enabled,
        "follow_per_day": s.follow_per_day,
        "follow_delay_min": s.follow_delay_min,
        "follow_delay_max": s.follow_delay_max,
        "mutual_follows_threshold": s.mutual_follows_threshold,
        "active_hours_start": s.active_hours_start,
        "active_hours_end": s.active_hours_end,
        "scan_depth": s.scan_depth,
        "last_unfollow_run": s.last_unfollow_run.isoformat() if s.last_unfollow_run else None,
        "last_follow_run": s.last_follow_run.isoformat() if s.last_follow_run else None,
        "last_scan_run": s.last_scan_run.isoformat() if s.last_scan_run else None,
        "follows_today": s.follows_today,
        "unfollows_today": s.unfollows_today,
        "follow_per_day_remaining": max(0, s.follow_per_day - s.follows_today),
        "unfollow_per_day_remaining": max(0, s.unfollow_per_day - s.unfollows_today),
    }


@router.get("/settings")
def get_settings(db: Session = Depends(get_db)):
    account = _active_account(db)
    s = _get_or_create_settings(db, account.id)
    return _serialize_settings(s)


class SettingsUpdate(BaseModel):
    auto_unfollow_enabled: Optional[bool] = None
    unfollow_after_days: Optional[int] = None
    unfollow_per_day: Optional[int] = None
    unfollow_only_non_followers: Optional[bool] = None
    auto_follow_enabled: Optional[bool] = None
    follow_per_day: Optional[int] = None
    follow_delay_min: Optional[int] = None
    follow_delay_max: Optional[int] = None
    mutual_follows_threshold: Optional[int] = None
    active_hours_start: Optional[int] = None
    active_hours_end: Optional[int] = None
    scan_depth: Optional[int] = None


@router.patch("/settings")
def update_settings(req: SettingsUpdate, db: Session = Depends(get_db)):
    account = _active_account(db)
    s = _get_or_create_settings(db, account.id)

    for field, value in req.model_dump(exclude_none=True).items():
        setattr(s, field, value)

    # Validate delay range
    if s.follow_delay_min >= s.follow_delay_max:
        raise HTTPException(400, "follow_delay_min must be less than follow_delay_max")
    if s.active_hours_start >= s.active_hours_end:
        raise HTTPException(400, "active_hours_start must be less than active_hours_end")

    db.commit()
    return _serialize_settings(s)


@router.post("/scan")
def trigger_scan(db: Session = Depends(get_db)):
    """Manually trigger a followers-of-following scan."""
    if not ig.is_logged_in():
        raise HTTPException(401, "Not logged in to Instagram")
    _active_account(db)
    sched.trigger_scan_now()
    return {"status": "scan_triggered"}


@router.post("/run-unfollow")
def trigger_unfollow(db: Session = Depends(get_db)):
    """Manually trigger the auto-unfollow job."""
    if not ig.is_logged_in():
        raise HTTPException(401, "Not logged in to Instagram")
    _active_account(db)
    sched.trigger_unfollow_now()
    return {"status": "unfollow_triggered"}


@router.post("/check-follow-back")
def trigger_check_follow_back(db: Session = Depends(get_db)):
    """Manually trigger follow-back status check."""
    if not ig.is_logged_in():
        raise HTTPException(401, "Not logged in to Instagram")
    _active_account(db)
    sched.trigger_check_follow_back_now()
    return {"status": "check_triggered"}
