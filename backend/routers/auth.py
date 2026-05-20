import datetime
import logging
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

import instagram_client as ig
from database import get_db, Account, AutomationSettings, ActivityLog

logger = logging.getLogger("instagrow.auth")
router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str
    platform: str = "instagram"


class ChallengeRequest(BaseModel):
    code: str


class SessionLoginRequest(BaseModel):
    username: str
    session_id: str
    platform: str = "instagram"


@router.post("/login")
def login(req: LoginRequest, db: Session = Depends(get_db)):
    """Start login (may require challenge/2FA — poll /auth/status)."""
    ig.set_platform(req.platform)
    if ig.login_state["status"] == "logged_in":
        return {"status": "already_logged_in"}

    account = db.query(Account).filter(Account.username == req.username).first()
    session_json = account.session_data if account else None

    ig.login_async(req.username, req.password, session_json)
    return {"status": "logging_in"}


@router.post("/login-session")
def login_session(req: SessionLoginRequest, db: Session = Depends(get_db)):
    """Login using a browser session cookie — avoids password-based login blocks."""
    ig.set_platform(req.platform)
    if ig.login_state["status"] == "logged_in":
        return {"status": "already_logged_in"}
    ig.login_by_sessionid_async(req.username, req.session_id)
    return {"status": "logging_in"}


@router.get("/platform")
def current_platform():
    return {"platform": ig.get_platform()}


@router.get("/status")
def status(db: Session = Depends(get_db)):
    state = ig.login_state.copy()
    state["platform"] = ig.get_platform()

    if state["status"] == "logged_in":
        my_id = ig.get_my_user_id()
        username = state.get("username") or ""

        # Persist / update account record
        account = db.query(Account).filter(Account.username == username).first()
        if not account:
            account = Account(username=username, user_id=my_id, platform=ig.get_platform())
            db.add(account)
        else:
            account.user_id = my_id
            account.platform = ig.get_platform()

        session_json = ig.get_session_json()
        if session_json:
            account.session_data = session_json

        db.commit()

        # Ensure automation settings exist
        if account.id:
            existing = db.query(AutomationSettings).filter(
                AutomationSettings.account_id == account.id
            ).first()
            if not existing:
                db.add(AutomationSettings(account_id=account.id))
                db.commit()

        state["username"] = username
        state["user_id"] = my_id

    return state


@router.post("/challenge")
def submit_challenge(req: ChallengeRequest):
    if ig.login_state["status"] not in ("challenge_required",):
        raise HTTPException(400, "No challenge pending")
    ig.submit_challenge_code(req.code)
    return {"status": "code_submitted"}


@router.post("/totp")
def submit_totp(req: ChallengeRequest):
    if ig.login_state["status"] != "totp_required":
        raise HTTPException(400, "No TOTP challenge pending")
    ig.submit_totp_code(req.code)
    return {"status": "code_submitted"}


@router.post("/logout")
def logout(db: Session = Depends(get_db)):
    username = ig.login_state.get("username")
    if username:
        # Save final session before logout
        session_json = ig.get_session_json()
        if session_json:
            account = db.query(Account).filter(Account.username == username).first()
            if account:
                account.session_data = session_json
                db.commit()

        log = ActivityLog(
            account_id=0,
            action_type="logout",
            details=f"User {username} logged out",
        )
        db.add(log)
        db.commit()

    ig.logout()
    return {"status": "logged_out"}
