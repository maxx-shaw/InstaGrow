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

# TOTP 2FA state — keeps the pending client + identifier alive between the
# TwoFactorRequired exception and the user submitting their authenticator code.
_totp_state: dict = {
    "code": None,
    "identifier": None,
    "client": None,
    "username": None,
    "event": threading.Event(),
}


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
    _totp_state["code"] = code
    _totp_state["event"].set()


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
    """Start password-based login in a background thread."""
    t = threading.Thread(
        target=_do_login,
        args=(username, password, session_json),
        daemon=True,
    )
    t.start()


def login_by_sessionid_async(username: str, session_id: str):
    """Start session-ID login in a background thread."""
    t = threading.Thread(
        target=_do_login_sessionid,
        args=(username, session_id),
        daemon=True,
    )
    t.start()


def _do_login_sessionid(username: str, session_id: str):
    global _client
    login_state["status"] = "logging_in"
    login_state["error"] = None
    login_state["username"] = username

    with _client_lock:
        try:
            cl = _make_client()
            cl.login_by_sessionid(session_id)
            # Verify it worked and populate user_id / username
            user = cl.user_info(cl.user_id)
            cl.username = user.username
            _client = cl
            login_state["status"] = "logged_in"
            logger.info("Session ID login successful for %s (user_id=%s)", username, cl.user_id)
        except Exception as e:
            login_state["status"] = "error"
            login_state["error"] = f"Session ID login failed: {e}. Make sure you copied the full sessionid cookie value."
            logger.error("Session ID login failed: %s", e)


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
            # Extract the identifier Instagram needs to verify the TOTP code
            two_factor_info = cl.last_json.get("two_factor_info", {})
            identifier = two_factor_info.get("two_factor_identifier", "")
            _totp_state["identifier"] = identifier
            _totp_state["client"] = cl
            _totp_state["username"] = username
            _totp_state["code"] = None
            _totp_state["event"].clear()
            login_state["status"] = "totp_required"
            login_state["error"] = None

            # Block this thread until the user submits their authenticator code
            got_code = _totp_state["event"].wait(timeout=300)
            code = _totp_state["code"]

            if not got_code or not code:
                login_state["status"] = "error"
                login_state["error"] = "Authenticator code timed out — please try again"
                return

            try:
                cl.two_factor_login(
                    verification_code=code,
                    two_factor_identifier=identifier,
                    username=username,
                    verification_method="3",  # 3 = TOTP authenticator app
                )
                _client = cl
                login_state["status"] = "logged_in"
                logger.info("TOTP 2FA login successful for %s", username)
            except Exception as e:
                login_state["status"] = "error"
                login_state["error"] = f"2FA verification failed: {e}"
                logger.error("TOTP verification failed: %s", e)

        except BadPassword:
            login_state["status"] = "error"
            login_state["error"] = "Incorrect username or password"

        except Exception as e:
            login_state["status"] = "error"
            login_state["error"] = str(e)
            logger.error("Login failed: %s", e)


def _do_login_call(cl: Client, username: str, password: str):
    """
    Call cl.login() and recover gracefully from the TypeError that instagrapi
    raises when Instagram's response contains a None where an int is expected
    (a common API response format change). Tries multiple strategies to recover
    a valid user_id before giving up.
    """
    try:
        cl.login(username, password)
        return  # clean success
    except (TypeError, AttributeError) as e:
        logger.warning("cl.login() raised %s — attempting recovery", e)

    # Strategy 1: user_id was already set before the error occurred
    if cl.user_id is not None and cl.user_id != 0:
        logger.info("Recovery S1: user_id=%s already set", cl.user_id)
        return

    # Strategy 2: pull user_id from the raw last_json response
    try:
        last = cl.last_json or {}
        user = (
            last.get("logged_in_user")
            or last.get("user")
            or {}
        )
        pk = user.get("pk") or user.get("pk_id") or user.get("id")
        if pk:
            cl.user_id = int(str(pk).split(".")[0])
            cl.username = username
            logger.info("Recovery S2: extracted user_id=%s from last_json", cl.user_id)
            return
    except Exception as ex:
        logger.warning("Recovery S2 failed: %s", ex)

    # Strategy 3: verify session is valid regardless (Instagram may have logged
    # us in even though response parsing failed)
    try:
        feed = cl.get_timeline_feed()
        if feed and cl.user_id:
            logger.info("Recovery S3: session valid, user_id=%s", cl.user_id)
            return
    except Exception as ex:
        logger.warning("Recovery S3 failed: %s", ex)

    # All recovery strategies failed — credentials or account issue
    raise Exception(
        "Could not complete Instagram login. "
        "Please double-check your username and password, then try again. "
        "If correct, Instagram may be temporarily blocking automated access — "
        "wait a few minutes and retry."
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
