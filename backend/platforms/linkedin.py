"""
LinkedIn automation via Playwright.
"""
import logging
import random
import threading
import time
from typing import Optional

import browser_engine as be

logger = logging.getLogger("socialreach.linkedin")
LI = "https://www.linkedin.com"

login_state: dict = {
    "status": "idle",
    "error": None,
    "challenge_method": None,
    "username": None,
    "user_id": None,
}

_profile_key: Optional[str] = None
_page_lock = threading.Lock()
_challenge_code: Optional[str] = None
_challenge_event = threading.Event()
_totp_code: Optional[str] = None
_totp_event = threading.Event()


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
    _profile_key = f"linkedin_{username}"
    login_state.update(status="logging_in", error=None, username=username)

    with _page_lock:
        try:
            page = be.get_page(_profile_key)
            page.goto(f"{LI}/login", wait_until="domcontentloaded")
            be.human_delay(1.5, 2.5)

            if "feed" in page.url or "mynetwork" in page.url:
                _finish_login(page, username)
                return

            be.human_type(page, '#username', username)
            be.human_delay(0.4, 0.9)
            be.human_type(page, '#password', password)
            be.human_delay(0.6, 1.2)
            be.human_click(page, 'button[type="submit"]')

            try:
                page.wait_for_url(lambda u: "/login" not in u, timeout=15000)
            except Exception:
                pass

            be.human_delay(1.5, 2.5)
            url = page.url

            if "checkpoint" in url or "challenge" in url or "verification" in url:
                login_state.update(status="challenge_required", challenge_method="email")
                _challenge_event.clear()
                got = _challenge_event.wait(timeout=300)
                if not got or not _challenge_code:
                    login_state.update(status="error", error="Challenge timed out — please try again")
                    return
                _submit_challenge(page, username)

            elif "/login" in url:
                try:
                    msg = page.locator('.form__label--error, [aria-live="assertive"]').first.inner_text(timeout=2000)
                except Exception:
                    msg = "Login failed — check your credentials"
                login_state.update(status="error", error=msg)

            else:
                _finish_login(page, username)

        except Exception as e:
            login_state.update(status="error", error=str(e))
            logger.error("LinkedIn login failed: %s", e)


def _submit_challenge(page, username: str):
    try:
        page.locator('input[name="pin"], input[id*="pin"], input[type="text"]').first.fill(_challenge_code)
        be.human_delay(0.4, 0.8)
        page.locator('button:has-text("Submit"), button[type="submit"]').first.click()
        page.wait_for_url(lambda u: "checkpoint" not in u and "challenge" not in u, timeout=10000)
        be.human_delay(1, 2)
        _finish_login(page, username)
    except Exception as e:
        login_state.update(status="error", error=f"Challenge failed: {e}")


# ---------------------------------------------------------------------------
# Login — session cookie (li_at)
# ---------------------------------------------------------------------------

def login_by_sessionid_async(username: str, li_at: str):
    t = threading.Thread(target=_do_login_sessionid, args=(username, li_at), daemon=True)
    t.start()


def _do_login_sessionid(username: str, li_at: str):
    global _profile_key
    _profile_key = f"linkedin_{username}"
    login_state.update(status="logging_in", error=None, username=username)

    with _page_lock:
        try:
            be.inject_cookie(_profile_key, "li_at", li_at, ".linkedin.com")
            page = be.get_page(_profile_key)
            page.goto(f"{LI}/feed/", wait_until="domcontentloaded")
            be.human_delay(2, 3)

            if "/login" in page.url or "/authwall" in page.url:
                login_state.update(
                    status="error",
                    error="Session cookie rejected — copy a fresh li_at cookie from linkedin.com and try again.",
                )
                return

            _finish_login(page, username)

        except Exception as e:
            login_state.update(status="error", error=f"Session login failed: {e}")
            logger.error("LinkedIn session login failed: %s", e)


def _finish_login(page, username: str):
    try:
        page.goto(f"{LI}/me/", wait_until="domcontentloaded")
        be.human_delay(1, 2)
        url = page.url
        if "/in/" in url:
            slug = url.split("/in/")[1].strip("/").split("?")[0]
            login_state["user_id"] = slug
        else:
            login_state["user_id"] = username
    except Exception:
        login_state["user_id"] = username

    for text in ("Dismiss", "Skip"):
        try:
            page.locator(f'button:has-text("{text}")').first.click(timeout=2000)
        except Exception:
            pass

    login_state.update(status="logged_in", username=username)
    be.save_context(_profile_key)
    logger.info("LinkedIn logged in as %s", username)


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
        page.goto(f"{LI}/in/{username}/", wait_until="domcontentloaded")
        be.human_delay(1.5, 2.5)

        try:
            # Try Connect first
            connect_btn = page.locator('button:has-text("Connect")').first
            if connect_btn.is_visible(timeout=3000):
                connect_btn.click()
                be.human_delay(0.5, 1)
                try:
                    page.locator('button:has-text("Send without a note")').first.click(timeout=2500)
                except Exception:
                    pass
                time.sleep(random.uniform(delay_min, delay_max))
                be.save_context(_profile_key)
                return True

            # Fall back to Follow
            follow_btn = page.locator('button:has-text("Follow"):not(:has-text("Following"))').first
            if follow_btn.is_visible(timeout=2000):
                follow_btn.click()
                be.human_delay(0.8, 1.5)
                time.sleep(random.uniform(delay_min, delay_max))
                be.save_context(_profile_key)
                return True

            logger.info("No Connect/Follow button for LinkedIn %s", username)
            return False

        except Exception as e:
            logger.error("LinkedIn follow %s: %s", username, e)
            raise


def unfollow_user_by_username(username: str, delay_min: int = 20, delay_max: int = 60) -> bool:
    with _page_lock:
        page = be.get_page(_profile_key)
        page.goto(f"{LI}/in/{username}/", wait_until="domcontentloaded")
        be.human_delay(1.5, 2.5)

        try:
            following_btn = page.locator('button:has-text("Following")').first
            if following_btn.is_visible(timeout=3000):
                following_btn.click()
                be.human_delay(0.5, 1)
                page.locator('span:has-text("Unfollow"), button:has-text("Unfollow")').first.click()
                be.human_delay(0.8, 1.5)
                time.sleep(random.uniform(delay_min, delay_max))
                be.save_context(_profile_key)
                return True

            logger.info("No Following button for LinkedIn %s", username)
            return False

        except Exception as e:
            logger.error("LinkedIn unfollow %s: %s", username, e)
            raise


def get_following(user_id: str, amount: int = 0) -> dict:
    results: dict = {}
    with _page_lock:
        page = be.get_page(_profile_key)

        def on_response(response):
            if "voyager/api/relationships" in response.url:
                try:
                    data = response.json()
                    for el in data.get("elements", []):
                        profile = (el.get("connectedMember")
                                   or el.get("miniProfile")
                                   or el.get("targetMember", {}).get("miniProfile", {}))
                        if not profile:
                            continue
                        uname = profile.get("publicIdentifier", "")
                        first = profile.get("firstName", "")
                        last = profile.get("lastName", "")
                        entity = profile.get("entityUrn", uname)
                        pk = entity.split("::")[-1] if "::" in entity else uname
                        if uname:
                            results[pk] = _UserInfo(
                                pk=pk, username=uname,
                                full_name=f"{first} {last}".strip(),
                            )
                except Exception as e:
                    logger.debug("LinkedIn connections parse: %s", e)

        page.on("response", on_response)
        try:
            page.goto(f"{LI}/mynetwork/invite-connect/connections/", wait_until="domcontentloaded")
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


def get_followers(user_id: str, amount: int = 0) -> dict:
    return get_following(user_id, amount)


def get_user_info_by_username(username: str) -> _UserInfo:
    return _UserInfo(pk=username, username=username)


def get_user_info(user_id: str) -> _UserInfo:
    return _UserInfo(pk=user_id, username=user_id)


def human_delay(min_s: float = 1.5, max_s: float = 4.0):
    be.human_delay(min_s, max_s)


def action_delay(min_s: int = 30, max_s: int = 120):
    time.sleep(random.uniform(min_s, max_s))
