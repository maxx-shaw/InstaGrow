"""
Instagram automation via Playwright.
Replaces instagrapi — all actions go through a real Chromium browser.
"""
import logging
import random
import threading
import time
from typing import Optional

import browser_engine as be

logger = logging.getLogger("socialreach.instagram")
IG = "https://www.instagram.com"

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

login_state: dict = {
    "status": "idle",   # idle | logging_in | challenge_required | totp_required | logged_in | error
    "error": None,
    "challenge_method": None,
    "username": None,
    "user_id": None,
}

_profile_key: Optional[str] = None
_page_lock = threading.Lock()

_totp_code: Optional[str] = None
_totp_event = threading.Event()
_challenge_code: Optional[str] = None
_challenge_event = threading.Event()


def is_logged_in() -> bool:
    return login_state["status"] == "logged_in"


def get_my_user_id() -> Optional[str]:
    return login_state.get("user_id")


def get_session_json() -> Optional[str]:
    # v2 uses file-based persistent context; no JSON session blob needed
    return None


def submit_totp_code(code: str):
    global _totp_code
    _totp_code = code
    _totp_event.set()


def submit_challenge_code(code: str):
    global _challenge_code
    _challenge_code = code
    _challenge_event.set()


def logout():
    global _page_lock
    login_state["status"] = "idle"
    login_state["user_id"] = None
    login_state["username"] = None
    login_state["error"] = None
    be.close_all()


# ---------------------------------------------------------------------------
# Login — password
# ---------------------------------------------------------------------------

def login_async(username: str, password: str, session_json: Optional[str] = None):
    t = threading.Thread(target=_do_login, args=(username, password), daemon=True)
    t.start()


def _do_login(username: str, password: str):
    global _profile_key
    _profile_key = f"instagram_{username}"
    login_state.update(status="logging_in", error=None, username=username)

    with _page_lock:
        try:
            page = be.get_page(_profile_key)
            page.goto(f"{IG}/accounts/login/", wait_until="domcontentloaded")
            be.human_delay(1.5, 2.5)

            # Already logged in from saved context?
            if "accounts/login" not in page.url:
                _finish_login(page, username)
                return

            # Dismiss cookie notice
            try:
                page.locator('button:has-text("Allow essential")').click(timeout=2500)
                be.human_delay(0.5, 1)
            except Exception:
                pass

            be.human_type(page, 'input[name="username"]', username)
            be.human_delay(0.4, 0.9)
            be.human_type(page, 'input[name="password"]', password)
            be.human_delay(0.6, 1.2)
            be.human_click(page, 'button[type="submit"]')

            try:
                page.wait_for_url(lambda u: "accounts/login" not in u, timeout=15000)
            except Exception:
                pass

            be.human_delay(1.5, 2.5)
            url = page.url

            if "two_factor" in url or "two_factor" in page.content().lower():
                login_state["status"] = "totp_required"
                _totp_event.clear()
                got = _totp_event.wait(timeout=300)
                if not got or not _totp_code:
                    login_state.update(status="error", error="Authenticator code timed out — please try again")
                    return
                _submit_totp(page, username)

            elif "challenge" in url:
                login_state.update(status="challenge_required", challenge_method="email")
                _challenge_event.clear()
                got = _challenge_event.wait(timeout=300)
                if not got or not _challenge_code:
                    login_state.update(status="error", error="Challenge code timed out — please try again")
                    return
                _submit_challenge(page, username)

            elif "accounts/login" in url:
                try:
                    msg = page.locator('[data-testid="login-error-message"]').inner_text(timeout=2000)
                except Exception:
                    msg = "Login failed — check your credentials"
                login_state.update(status="error", error=msg)

            else:
                _finish_login(page, username)

        except Exception as e:
            login_state.update(status="error", error=str(e))
            logger.error("Login failed: %s", e)


def _submit_totp(page, username: str):
    try:
        sel = 'input[name="verificationCode"], input[aria-label*="ode"]'
        page.locator(sel).first.fill(_totp_code)
        be.human_delay(0.4, 0.8)
        page.locator('button:has-text("Confirm")').first.click()
        page.wait_for_url(lambda u: "two_factor" not in u, timeout=10000)
        be.human_delay(1, 2)
        _finish_login(page, username)
    except Exception as e:
        login_state.update(status="error", error=f"2FA failed: {e}")


def _submit_challenge(page, username: str):
    try:
        page.locator('input[name="security_code"], input[type="text"]').first.fill(_challenge_code)
        be.human_delay(0.4, 0.8)
        page.locator('button:has-text("Submit"), button[type="submit"]').first.click()
        page.wait_for_url(lambda u: "challenge" not in u, timeout=10000)
        be.human_delay(1, 2)
        _finish_login(page, username)
    except Exception as e:
        login_state.update(status="error", error=f"Challenge failed: {e}")


# ---------------------------------------------------------------------------
# Login — session ID
# ---------------------------------------------------------------------------

def login_by_sessionid_async(username: str, session_id: str):
    t = threading.Thread(target=_do_login_sessionid, args=(username, session_id), daemon=True)
    t.start()


def _do_login_sessionid(username: str, session_id: str):
    global _profile_key
    _profile_key = f"instagram_{username}"
    login_state.update(status="logging_in", error=None, username=username)

    with _page_lock:
        try:
            be.inject_cookie(_profile_key, "sessionid", session_id, ".instagram.com")
            page = be.get_page(_profile_key)
            page.goto(f"{IG}/", wait_until="domcontentloaded")
            be.human_delay(2, 3)

            if "accounts/login" in page.url:
                login_state.update(
                    status="error",
                    error="Session ID rejected — it may be expired. Copy a fresh sessionid cookie from your browser and try again.",
                )
                return

            _finish_login(page, username)

        except Exception as e:
            login_state.update(status="error", error=f"Session login failed: {e}")
            logger.error("Session ID login failed: %s", e)


# ---------------------------------------------------------------------------
# Post-login setup
# ---------------------------------------------------------------------------

def _finish_login(page, username: str):
    # Dismiss prompts
    for text in ("Not now", "Not Now"):
        try:
            page.locator(f'button:has-text("{text}")').first.click(timeout=2500)
            be.human_delay(0.5, 1)
        except Exception:
            pass

    user_id = _extract_user_id(page, username)
    login_state.update(status="logged_in", user_id=user_id or username, username=username)
    be.save_context(_profile_key)
    logger.info("Logged in as %s (user_id=%s)", username, user_id)


def _extract_user_id(page, username: str) -> Optional[str]:
    holder: dict = {}

    def on_response(response):
        if "web_profile_info" in response.url:
            try:
                data = response.json()
                uid = data.get("data", {}).get("user", {}).get("id")
                if uid:
                    holder["id"] = str(uid)
            except Exception:
                pass

    page.on("response", on_response)
    try:
        page.goto(f"{IG}/{username}/", wait_until="domcontentloaded")
        be.human_delay(1, 2)
    finally:
        page.remove_listener("response", on_response)

    return holder.get("id")


# ---------------------------------------------------------------------------
# Follow / Unfollow
# ---------------------------------------------------------------------------

def follow_user_by_username(username: str, delay_min: int = 45, delay_max: int = 180) -> bool:
    with _page_lock:
        page = be.get_page(_profile_key)
        page.goto(f"{IG}/{username}/", wait_until="domcontentloaded")
        be.human_delay(1.2, 2.5)

        try:
            btn = page.locator('button:has-text("Follow")').filter(
                has_not=page.locator(':has-text("Following"), :has-text("Requested")')
            ).first
            if not btn.is_visible(timeout=3000):
                logger.info("No Follow button for @%s — may already follow", username)
                return False
            btn.click()
            be.human_delay(0.8, 1.5)
        except Exception:
            # Try simpler selector
            try:
                page.locator('header button').first.click()
                be.human_delay(0.8, 1.5)
            except Exception as e:
                logger.error("follow @%s: %s", username, e)
                raise

        time.sleep(random.uniform(delay_min, delay_max))
        be.save_context(_profile_key)
        return True


def unfollow_user_by_username(username: str, delay_min: int = 30, delay_max: int = 90) -> bool:
    with _page_lock:
        page = be.get_page(_profile_key)
        page.goto(f"{IG}/{username}/", wait_until="domcontentloaded")
        be.human_delay(1.2, 2.5)

        try:
            # Click the Following button to open unfollow sheet
            page.locator('button:has-text("Following")').first.click()
            be.human_delay(0.6, 1.2)

            # Confirm in the dialog
            page.locator('[role="dialog"] button:has-text("Unfollow")').first.click()
            be.human_delay(0.6, 1.2)
        except Exception as e:
            logger.error("unfollow @%s: %s", username, e)
            raise

        time.sleep(random.uniform(delay_min, delay_max))
        be.save_context(_profile_key)
        return True


# ---------------------------------------------------------------------------
# Scrape following / followers (intercepts Instagram's own XHR responses)
# ---------------------------------------------------------------------------

class _UserInfo:
    """Minimal user object matching the interface expected by existing routers."""
    def __init__(self, pk, username, full_name="", profile_pic_url=None):
        self.pk = pk
        self.username = username
        self.full_name = full_name
        self.profile_pic_url = profile_pic_url


def _scrape_list(list_type: str) -> dict:
    """Scrape following or followers by intercepting XHR calls."""
    username = login_state.get("username", "")
    if not username:
        raise Exception("Not logged in")

    results: dict = {}

    with _page_lock:
        page = be.get_page(_profile_key)

        def on_response(response):
            url = response.url
            if "friendships" in url and list_type in url:
                try:
                    data = response.json()
                    for user in data.get("users", []):
                        pk = str(user.get("pk", ""))
                        uname = user.get("username", "")
                        if pk and uname:
                            results[pk] = _UserInfo(
                                pk=pk,
                                username=uname,
                                full_name=user.get("full_name", ""),
                                profile_pic_url=user.get("profile_pic_url"),
                            )
                except Exception:
                    pass

        page.on("response", on_response)
        try:
            page.goto(f"{IG}/{username}/{list_type}/", wait_until="domcontentloaded")
            be.human_delay(1.5, 2.5)

            if "accounts/login" in page.url:
                raise Exception("Session expired — please log in again")

            # Scroll the modal to trigger paginated XHR calls
            try:
                page.wait_for_selector('[role="dialog"]', timeout=5000)
            except Exception:
                pass

            last_count = -1
            stalls = 0
            while stalls < 5:
                if len(results) == last_count:
                    stalls += 1
                else:
                    stalls = 0
                    last_count = len(results)

                be.scroll_element(
                    page,
                    "document.querySelector('[role=\"dialog\"] [style*=\"overflow\"]') || "
                    "document.querySelector('[role=\"dialog\"] ul')",
                )
                be.human_delay(0.7, 1.4)

            try:
                page.keyboard.press("Escape")
                be.human_delay(0.4, 0.8)
            except Exception:
                pass

        finally:
            page.remove_listener("response", on_response)

    return results


def get_following(user_id: str, amount: int = 0) -> dict:
    return _scrape_list("following")


def get_followers(user_id: str, amount: int = 0) -> dict:
    return _scrape_list("followers")


def get_user_info_by_username(username: str) -> _UserInfo:
    with _page_lock:
        page = be.get_page(_profile_key)
        holder: dict = {}

        def on_response(response):
            if "web_profile_info" in response.url:
                try:
                    data = response.json()
                    user = data.get("data", {}).get("user", {})
                    if user:
                        holder["u"] = user
                except Exception:
                    pass

        page.on("response", on_response)
        try:
            page.goto(f"{IG}/{username}/", wait_until="domcontentloaded")
            be.human_delay(1, 2)
        finally:
            page.remove_listener("response", on_response)

    if "u" in holder:
        u = holder["u"]
        return _UserInfo(
            pk=str(u.get("id", username)),
            username=u.get("username", username),
            full_name=u.get("full_name", ""),
            profile_pic_url=u.get("profile_pic_url"),
        )
    return _UserInfo(pk=username, username=username)


def get_user_info(user_id: str) -> _UserInfo:
    return _UserInfo(pk=user_id, username=user_id)


def human_delay(min_s: float = 1.5, max_s: float = 4.0):
    be.human_delay(min_s, max_s)


def action_delay(min_s: int = 45, max_s: int = 180):
    time.sleep(random.uniform(min_s, max_s))
