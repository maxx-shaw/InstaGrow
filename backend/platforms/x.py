"""
X (Twitter) automation via Playwright.
"""
import logging
import random
import threading
import time
from typing import Optional

import browser_engine as be

logger = logging.getLogger("socialreach.x")
X = "https://x.com"

login_state: dict = {
    "status": "idle",
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
    login_state.update(status="idle", user_id=None, username=None, error=None)
    be.close_all()


# ---------------------------------------------------------------------------
# Login — password
# ---------------------------------------------------------------------------

def login_async(username: str, password: str, session_json=None):
    t = threading.Thread(target=_do_login, args=(username, password), daemon=True)
    t.start()


def _do_login(username: str, password: str):
    global _profile_key
    _profile_key = f"x_{username}"
    login_state.update(status="logging_in", error=None, username=username)

    with _page_lock:
        try:
            page = be.get_page(_profile_key)
            page.goto(f"{X}/i/flow/login", wait_until="domcontentloaded")
            be.human_delay(2, 3)

            if "home" in page.url:
                _finish_login(page, username)
                return

            # Step 1 — username / email
            be.human_type(page, 'input[autocomplete="username"]', username)
            be.human_delay(0.5, 1)
            page.locator('button:has-text("Next")').last.click()
            be.human_delay(1.5, 2.5)

            # X sometimes asks for phone/email verification before password
            if page.locator('input[data-testid="ocfEnterTextTextInput"]').is_visible():
                login_state.update(status="challenge_required", challenge_method="phone")
                _challenge_event.clear()
                got = _challenge_event.wait(timeout=300)
                if not got or not _challenge_code:
                    login_state.update(status="error", error="Identity check timed out — please try again")
                    return
                page.locator('input[data-testid="ocfEnterTextTextInput"]').first.fill(_challenge_code)
                be.human_delay(0.5, 1)
                page.locator('button[data-testid="ocfEnterTextNextButton"]').first.click()
                be.human_delay(1.5, 2)

            # Step 2 — password
            be.human_type(page, 'input[name="password"]', password)
            be.human_delay(0.5, 1)
            page.locator('button[data-testid="LoginForm_Login_Button"]').first.click()

            try:
                page.wait_for_url(lambda u: "/flow/login" not in u, timeout=15000)
            except Exception:
                pass

            be.human_delay(1.5, 2.5)
            url = page.url

            # 2FA / TOTP
            if page.locator('input[data-testid="ocfEnterTextTextInput"]').is_visible():
                login_state["status"] = "totp_required"
                _totp_event.clear()
                got = _totp_event.wait(timeout=300)
                if not got or not _totp_code:
                    login_state.update(status="error", error="2FA code timed out — please try again")
                    return
                page.locator('input[data-testid="ocfEnterTextTextInput"]').first.fill(_totp_code)
                be.human_delay(0.4, 0.8)
                page.locator('button[data-testid="ocfEnterTextNextButton"]').first.click()
                be.human_delay(1.5, 2)
                url = page.url

            if "/home" in url:
                _finish_login(page, username)
            elif "/flow/login" in url:
                login_state.update(status="error", error="Login failed — check your username and password")
            else:
                _finish_login(page, username)

        except Exception as e:
            login_state.update(status="error", error=str(e))
            logger.error("X login failed: %s", e)


# ---------------------------------------------------------------------------
# Login — session cookie (auth_token)
# ---------------------------------------------------------------------------

def login_by_sessionid_async(username: str, auth_token: str):
    t = threading.Thread(target=_do_login_sessionid, args=(username, auth_token), daemon=True)
    t.start()


def _do_login_sessionid(username: str, auth_token: str):
    global _profile_key
    _profile_key = f"x_{username}"
    login_state.update(status="logging_in", error=None, username=username)

    with _page_lock:
        try:
            be.inject_cookie(_profile_key, "auth_token", auth_token, ".x.com")
            page = be.get_page(_profile_key)
            page.goto(f"{X}/home", wait_until="domcontentloaded")
            be.human_delay(2, 3)

            if "/login" in page.url or "/flow/" in page.url:
                login_state.update(
                    status="error",
                    error="Session token rejected — copy a fresh auth_token cookie from x.com and try again.",
                )
                return

            _finish_login(page, username)

        except Exception as e:
            login_state.update(status="error", error=f"Session login failed: {e}")
            logger.error("X session login failed: %s", e)


def _finish_login(page, username: str):
    login_state.update(status="logged_in", user_id=username, username=username)
    be.save_context(_profile_key)
    logger.info("X logged in as @%s", username)


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

class _UserInfo:
    def __init__(self, pk, username, full_name="", profile_pic_url=None):
        self.pk = pk
        self.username = username
        self.full_name = full_name
        self.profile_pic_url = profile_pic_url


def follow_user_by_username(username: str, delay_min: int = 30, delay_max: int = 120) -> bool:
    with _page_lock:
        page = be.get_page(_profile_key)
        page.goto(f"{X}/{username}", wait_until="domcontentloaded")
        be.human_delay(1.5, 2.5)

        try:
            # X uses data-testid on follow buttons
            follow_btn = page.locator(
                f'button[data-testid="follow"], '
                f'button[aria-label*="Follow @{username}"]'
            ).first
            if follow_btn.is_visible(timeout=3000):
                follow_btn.click()
                be.human_delay(0.8, 1.5)
                time.sleep(random.uniform(delay_min, delay_max))
                be.save_context(_profile_key)
                return True
            logger.info("No Follow button for X @%s", username)
            return False
        except Exception as e:
            logger.error("X follow @%s: %s", username, e)
            raise


def unfollow_user_by_username(username: str, delay_min: int = 20, delay_max: int = 60) -> bool:
    with _page_lock:
        page = be.get_page(_profile_key)
        page.goto(f"{X}/{username}", wait_until="domcontentloaded")
        be.human_delay(1.5, 2.5)

        try:
            unfollow_btn = page.locator(
                f'button[data-testid="unfollow"], '
                f'button[aria-label*="Following @{username}"]'
            ).first
            if unfollow_btn.is_visible(timeout=3000):
                unfollow_btn.click()
                be.human_delay(0.5, 1)
                # Confirm dialog
                try:
                    page.locator('button[data-testid="confirmationSheetConfirm"]').first.click(timeout=3000)
                    be.human_delay(0.6, 1.2)
                except Exception:
                    pass
                time.sleep(random.uniform(delay_min, delay_max))
                be.save_context(_profile_key)
                return True
            logger.info("No Unfollow button for X @%s", username)
            return False
        except Exception as e:
            logger.error("X unfollow @%s: %s", username, e)
            raise


def _scrape_list(list_type: str, amount: int = 0) -> dict:
    results: dict = {}
    username = login_state.get("username", "")

    with _page_lock:
        page = be.get_page(_profile_key)

        def on_response(response):
            url = response.url
            if list_type.capitalize() in url or list_type in url:
                try:
                    data = response.json()
                    instructions = (
                        data.get("data", {})
                        .get("user", {})
                        .get("result", {})
                        .get("timeline", {})
                        .get("timeline", {})
                        .get("instructions", [])
                    )
                    for inst in instructions:
                        for entry in inst.get("entries", []):
                            user_result = (
                                entry.get("content", {})
                                .get("itemContent", {})
                                .get("user_results", {})
                                .get("result", {})
                            )
                            legacy = user_result.get("legacy", {})
                            sn = legacy.get("screen_name")
                            if sn:
                                results[sn] = _UserInfo(
                                    pk=sn, username=sn,
                                    full_name=legacy.get("name", ""),
                                    profile_pic_url=legacy.get("profile_image_url_https"),
                                )
                except Exception as e:
                    logger.debug("X %s parse: %s", list_type, e)

        page.on("response", on_response)
        try:
            page.goto(f"{X}/{username}/{list_type}", wait_until="domcontentloaded")
            be.human_delay(2, 3)
            last_count = -1
            stalls = 0
            while stalls < 5:
                if len(results) == last_count:
                    stalls += 1
                else:
                    stalls = 0
                    last_count = len(results)
                page.evaluate("window.scrollBy(0, 600)")
                be.human_delay(0.8, 1.5)
                if amount > 0 and len(results) >= amount:
                    break
        finally:
            page.remove_listener("response", on_response)

    return results


def get_following(user_id: str, amount: int = 0) -> dict:
    return _scrape_list("following", amount)


def get_followers(user_id: str, amount: int = 0) -> dict:
    return _scrape_list("followers", amount)


def get_user_info_by_username(username: str) -> _UserInfo:
    with _page_lock:
        page = be.get_page(_profile_key)
        holder: dict = {}

        def on_response(response):
            if "UserByScreenName" in response.url:
                try:
                    data = response.json()
                    user = data.get("data", {}).get("user", {}).get("result", {})
                    legacy = user.get("legacy", {})
                    if legacy.get("screen_name"):
                        holder["u"] = legacy
                        holder["id"] = user.get("rest_id", username)
                except Exception:
                    pass

        page.on("response", on_response)
        try:
            page.goto(f"{X}/{username}", wait_until="domcontentloaded")
            be.human_delay(1, 2)
        finally:
            page.remove_listener("response", on_response)

    if "u" in holder:
        u = holder["u"]
        return _UserInfo(
            pk=holder.get("id", username),
            username=u.get("screen_name", username),
            full_name=u.get("name", ""),
            profile_pic_url=u.get("profile_image_url_https"),
        )
    return _UserInfo(pk=username, username=username)


def get_user_info(user_id: str) -> _UserInfo:
    return _UserInfo(pk=user_id, username=user_id)


def human_delay(min_s: float = 1.5, max_s: float = 4.0):
    be.human_delay(min_s, max_s)


def action_delay(min_s: int = 30, max_s: int = 120):
    time.sleep(random.uniform(min_s, max_s))
