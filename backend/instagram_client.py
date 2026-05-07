"""
Singleton Instagram client manager using instagrapi.
Handles session persistence, challenge (2FA), and human-like rate limiting.
"""
import json
import logging
import random
import threading
import time
from typing import Optional

from instagrapi import Client
from instagrapi.exceptions import (
    ChallengeRequired,
    LoginRequired,
    TwoFactorRequired,
    BadPassword,
    UserNotFound,
    ClientError,
)

logger = logging.getLogger("instagrow.client")


# ---------------------------------------------------------------------------
# Global state (single-user app)
# ---------------------------------------------------------------------------

_client: Optional[Client] = None
_client_lock = threading.Lock()

# Login / challenge state
login_state = {
    "status": "idle",           # idle | logging_in | challenge_required | totp_required | logged_in | error
    "error": None,
    "challenge_method": None,   # email | sms
    "username": None,
}

_challenge_code: Optional[str] = None
_challenge_event = threading.Event()
_totp_code: Optional[str] = None
_totp_event = threading.Event()


# ---------------------------------------------------------------------------
# Challenge / TOTP handlers
# ---------------------------------------------------------------------------

def _challenge_code_handler(username: str, choice: int) -> str:
    """Called by instagrapi when Instagram requires a verification code."""
    global _challenge_code
    method = "email" if choice == 0 else "sms"
    logger.info("Challenge required for %s via %s", username, method)
    login_state["status"] = "challenge_required"
    login_state["challenge_method"] = method
    _challenge_event.clear()
    _challenge_event.wait(timeout=300)   # wait up to 5 min for user input
    code = _challenge_code
    _challenge_code = None
    return code or ""


def _totp_2fa_handler(username: str, totp_code_callback) -> str:
    """Called by instagrapi when Instagram requires a TOTP 2FA code."""
    global _totp_code
    logger.info("TOTP 2FA required for %s", username)
    login_state["status"] = "totp_required"
    _totp_event.clear()
    _totp_event.wait(timeout=300)
    code = _totp_code
    _totp_code = None
    return code or ""


def submit_challenge_code(code: str):
    global _challenge_code
    _challenge_code = code
    _challenge_event.set()


def submit_totp_code(code: str):
    global _totp_code
    _totp_code = code
    _totp_event.set()


# ---------------------------------------------------------------------------
# Client factory
# ---------------------------------------------------------------------------

def _make_client() -> Client:
    cl = Client()
    cl.challenge_code_handler = _challenge_code_handler
    cl.logger = logger
    return cl


def get_client() -> Optional[Client]:
    return _client


def is_logged_in() -> bool:
    return _client is not None and login_state["status"] == "logged_in"


# ---------------------------------------------------------------------------
# Login / session management
# ---------------------------------------------------------------------------

def login_async(username: str, password: str, session_json: Optional[str] = None):
    """Start login in a background thread. Check login_state for progress."""
    t = threading.Thread(
        target=_do_login,
        args=(username, password, session_json),
        daemon=True,
    )
    t.start()


def _do_login(username: str, password: str, session_json: Optional[str]):
    global _client
    login_state["status"] = "logging_in"
    login_state["error"] = None
    login_state["username"] = username

    with _client_lock:
        try:
            cl = _make_client()

            # Try to reuse existing session first
            if session_json:
                try:
                    settings = json.loads(session_json)
                    cl.set_settings(settings)
                    _do_login_call(cl, username, password)
                    logger.info("Logged in via saved session for %s", username)
                    _client = cl
                    login_state["status"] = "logged_in"
                    return
                except Exception as e:
                    logger.warning("Session reuse failed, trying fresh login: %s", e)
                    cl = _make_client()

            # Fresh login
            _do_login_call(cl, username, password)
            _client = cl
            login_state["status"] = "logged_in"
            logger.info("Fresh login successful for %s (user_id=%s)", username, cl.user_id)

        except ChallengeRequired:
            logger.info("Challenge required — waiting for code submission")
            if _client is None:
                login_state["status"] = "error"
                login_state["error"] = "Challenge timed out or failed"
            else:
                login_state["status"] = "logged_in"

        except TwoFactorRequired:
            login_state["status"] = "totp_required"
            login_state["error"] = "Two-factor authentication required"

        except BadPassword:
            login_state["status"] = "error"
            login_state["error"] = "Incorrect username or password"

        except Exception as e:
            login_state["status"] = "error"
            login_state["error"] = str(e)
            logger.error("Login failed: %s", e)


def _do_login_call(cl: Client, username: str, password: str):
    """
    Call cl.login() and tolerate the TypeError / AttributeError that instagrapi
    sometimes raises when parsing Instagram's response (e.g. user_id comes back
    as None from the API). If the call raises but cl.user_id is still populated
    we consider it a success.
    """
    try:
        cl.login(username, password)
    except (TypeError, AttributeError) as e:
        # instagrapi called int() / accessed an attr on None in the response.
        # Check whether we're actually logged in despite the internal error.
        if cl.user_id:
            logger.warning(
                "Login raised %s internally but user_id=%s — treating as success",
                e, cl.user_id,
            )
            return
        raise Exception(
            "Instagram returned an unexpected response during login. "
            "Make sure your username/password are correct and try again. "
            f"(internal: {e})"
        )


def get_session_json() -> Optional[str]:
    if _client is None:
        return None
    try:
        return json.dumps(_client.get_settings())
    except Exception:
        return None


def logout():
    global _client
    with _client_lock:
        if _client:
            try:
                _client.logout()
            except Exception:
                pass
        _client = None
        login_state["status"] = "idle"
        login_state["error"] = None


# ---------------------------------------------------------------------------
# Human-like helpers
# ---------------------------------------------------------------------------

def human_delay(min_s: float = 1.5, max_s: float = 4.0):
    time.sleep(random.uniform(min_s, max_s))


def action_delay(min_s: int = 45, max_s: int = 180):
    """Longer delay between follow/unfollow actions."""
    jitter = random.uniform(0, 15)
    time.sleep(random.uniform(min_s, max_s) + jitter)


# ---------------------------------------------------------------------------
# Follow / unfollow wrappers
# ---------------------------------------------------------------------------

def follow_user(user_id: str, delay_min: int = 45, delay_max: int = 180) -> bool:
    if not is_logged_in():
        raise LoginRequired("Not logged in")
    try:
        result = _client.user_follow(int(user_id))
        logger.info("Followed user %s → %s", user_id, result)
        action_delay(delay_min, delay_max)
        return bool(result)
    except Exception as e:
        logger.error("Failed to follow %s: %s", user_id, e)
        raise


def unfollow_user(user_id: str, delay_min: int = 30, delay_max: int = 90) -> bool:
    if not is_logged_in():
        raise LoginRequired("Not logged in")
    try:
        result = _client.user_unfollow(int(user_id))
        logger.info("Unfollowed user %s → %s", user_id, result)
        action_delay(delay_min, delay_max)
        return bool(result)
    except Exception as e:
        logger.error("Failed to unfollow %s: %s", user_id, e)
        raise


def get_following(user_id: str, amount: int = 0) -> dict:
    if not is_logged_in():
        raise LoginRequired("Not logged in")
    human_delay(2, 5)
    return _client.user_following(int(user_id), amount=amount)


def get_followers(user_id: str, amount: int = 0) -> dict:
    if not is_logged_in():
        raise LoginRequired("Not logged in")
    human_delay(2, 5)
    return _client.user_followers(int(user_id), amount=amount)


def get_my_user_id() -> Optional[str]:
    if _client is None:
        return None
    return str(_client.user_id)


def get_user_info(user_id: str):
    if not is_logged_in():
        raise LoginRequired("Not logged in")
    human_delay(1, 3)
    return _client.user_info(int(user_id))


def get_user_info_by_username(username: str):
    if not is_logged_in():
        raise LoginRequired("Not logged in")
    human_delay(1, 3)
    return _client.user_info_by_username(username)
