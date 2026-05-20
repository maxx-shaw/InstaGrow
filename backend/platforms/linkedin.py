"""
LinkedIn automation via Playwright — coming soon.
"""
import logging

logger = logging.getLogger("socialreach.linkedin")

login_state: dict = {
    "status": "idle",
    "error": "LinkedIn support coming soon",
    "username": None,
    "user_id": None,
}


def is_logged_in() -> bool:
    return False


def login_async(username: str, password: str, session_json=None):
    raise NotImplementedError("LinkedIn support is coming in a future update")


def login_by_sessionid_async(username: str, session_id: str):
    raise NotImplementedError("LinkedIn support is coming in a future update")
