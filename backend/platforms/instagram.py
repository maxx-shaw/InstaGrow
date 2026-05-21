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
    "detail": None,     # human-readable progress message for the UI
    "challenge_method": None,
    "username": None,
    "user_id": None,
}


def _set_detail(msg: str):
    """Update the user-facing progress message and log it."""
    login_state["detail"] = msg
    logger.info("Login progress: %s", msg)

_profile_key: Optional[str] = None
_page_lock = threading.Lock()

_totp_code: Optional[str] = None
_totp_event = threading.Event()
_challenge_code: Optional[str] = None
_challenge_event = threading.Event()


def _dismiss_overlays(page):
    """Dismiss Instagram's cookie/consent dialog via JS (bypasses overlay detection)."""
    try:
        dismissed = page.evaluate("""() => {
            const keywords = [
                'allow all', 'allow essential', 'decline optional',
                'accept all', 'accept', 'only allow essential',
                'reject all', 'decline'
            ];
            // Check all clickable elements, not just <button>
            const clickable = Array.from(document.querySelectorAll(
                'button, [role="button"], [tabindex="0"]'
            ));
            for (const el of clickable) {
                const txt = (el.innerText || el.textContent || '').trim().toLowerCase();
                if (!txt || txt.length > 60) continue;
                if (keywords.some(kw => txt.startsWith(kw))) {
                    el.click();
                    return txt.slice(0, 50);
                }
            }
            return null;
        }""")
        if dismissed:
            logger.info("Dismissed overlay via JS: %s", dismissed)
            be.human_delay(0.8, 1.2)
        else:
            logger.info("No cookie overlay detected")
    except Exception as e:
        logger.debug("Overlay dismiss (non-fatal): %s", e)


def _submit_login_form(page):
    """Submit the login form — tries multiple selectors then falls back to Enter."""
    selectors = [
        'button[type="submit"]',
        'button:has-text("Log in")',
        'div[role="button"]:has-text("Log in")',
        '[aria-label="Log in"]',
    ]
    for sel in selectors:
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=1500):
                el.click()
                logger.info("Clicked login submit via: %s", sel)
                return
        except Exception:
            continue
    logger.info("No submit button matched — pressing Enter")
    page.keyboard.press("Enter")


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

def _reset_in_progress_login():
    """Unblock any login currently waiting on a challenge code, so a new attempt can start."""
    global _totp_code, _challenge_code
    status = login_state.get("status")
    if status in ("logging_in", "totp_required", "challenge_required"):
        logger.info("Aborting previous login (status=%s)", status)
        _totp_code = None
        _challenge_code = None
        _totp_event.set()
        _challenge_event.set()
        time.sleep(0.5)  # give the old thread a moment to unwind


def login_async(username: str, password: str, session_json: Optional[str] = None):
    _reset_in_progress_login()
    # Run on the dedicated Playwright thread so all browser objects stay
    # owned by the same thread.
    future = be.submit_async(_do_login, username, password)

    def _watchdog():
        try:
            future.result(timeout=180)
        except Exception as e:
            if login_state.get("status") == "logging_in":
                login_state.update(status="error",
                                   error=f"Login failed: {e}")
                logger.error("Login watchdog: %s", e)
    threading.Thread(target=_watchdog, daemon=True).start()


def _do_login(username: str, password: str):
    global _profile_key
    _profile_key = f"instagram_{username}"
    login_state.update(status="logging_in", error=None,
                       detail="Starting up the browser…", username=username)
    logger.info("Starting Playwright login for @%s", username)

    with _page_lock:
        try:
            _set_detail("Launching secure browser session…")
            page = be.get_page(_profile_key)
            page.set_default_timeout(30000)
            _set_detail("Opening Instagram…")
            page.goto(f"{IG}/accounts/login/", wait_until="domcontentloaded")
            be.human_delay(1.5, 2.5)

            # Already logged in from saved context?
            if "accounts/login" not in page.url:
                _set_detail("Existing session found — signing you in…")
                _finish_login(page, username)
                return

            # Dismiss cookie / consent overlay before touching any inputs
            _set_detail("Accepting cookie prompt…")
            _dismiss_overlays(page)

            _set_detail("Entering your credentials…")
            be.human_type(page, 'input[name="username"]', username)
            be.human_delay(0.4, 0.9)
            be.human_type(page, 'input[name="password"]', password)
            be.human_delay(0.6, 1.2)
            _set_detail("Submitting login…")
            _submit_login_form(page)
            _set_detail("Waiting for Instagram to respond…")

            try:
                page.wait_for_url(lambda u: "accounts/login" not in u, timeout=15000)
            except Exception:
                pass

            be.human_delay(1.5, 2.5)
            url = page.url
            logger.info("Post-submit URL: %s", url)

            page_text = ""
            try:
                page_text = page.content().lower()
            except Exception:
                pass

            # Most reliable 2FA detection: is there a code/OTP input on the page?
            two_factor_input_found = False
            for sel in [
                'input[name="verificationCode"]',
                'input[autocomplete="one-time-code"]',
                'input[inputmode="numeric"]',
                'input[name="security_code"]',
                'input[aria-label*="ode" i]',
            ]:
                try:
                    if page.locator(sel).first.is_visible(timeout=600):
                        two_factor_input_found = True
                        logger.info("2FA input detected via selector: %s", sel)
                        break
                except Exception:
                    continue

            two_factor_text_hit = any(kw in page_text for kw in [
                "two_factor", "two-factor", "security code", "verification code",
                "6-digit code", "enter the code", "we sent a", "check your",
                "login code", "confirmation code",
            ])

            two_factor_hit = two_factor_input_found or "two_factor" in url or two_factor_text_hit

            if two_factor_hit:
                logger.info("Instagram requires 2FA — waiting for code (input_found=%s, url_match=%s, text_match=%s)",
                            two_factor_input_found, "two_factor" in url, two_factor_text_hit)
                login_state.update(status="totp_required", error=None,
                                   detail="Two-factor authentication required.")
                _totp_event.clear()
                got = _totp_event.wait(timeout=300)
                if not got or not _totp_code:
                    login_state.update(status="error", error="2FA code timed out or cancelled — please try again")
                    return
                logger.info("Got 2FA code, submitting")
                _set_detail("Submitting your verification code…")
                _submit_totp(page, username)

            elif "challenge" in url or "checkpoint" in url:
                logger.info("Instagram challenge required (URL: %s)", url)
                login_state.update(status="challenge_required", challenge_method="email")
                _challenge_event.clear()
                got = _challenge_event.wait(timeout=300)
                if not got or not _challenge_code:
                    login_state.update(status="error", error="Challenge code timed out — please try again")
                    return
                _submit_challenge(page, username)

            elif "accounts/login" in url:
                # Still on login page — could be wrong credentials OR a banner we missed
                try:
                    msg = page.locator('[data-testid="login-error-message"]').inner_text(timeout=2000)
                except Exception:
                    # Log a slice of the page to help diagnose
                    logger.warning("Login fall-through. URL=%s. Page text head: %s",
                                   url, page_text[:500].replace("\n", " "))
                    msg = "Login failed — check your credentials, or Instagram showed an unexpected page"
                login_state.update(status="error", error=msg)

            else:
                _finish_login(page, username)

        except Exception as e:
            login_state.update(status="error", error=str(e))
            logger.error("Login failed: %s", e)


def _submit_totp(page, username: str):
    try:
        # Find the verification code input — bloks UI uses different attrs
        code_selectors = [
            'input[name="verificationCode"]',
            'input[aria-label*="ode"]',
            'input[autocomplete="one-time-code"]',
            'input[inputmode="numeric"]',
            'input[type="tel"]',
        ]
        filled = False
        for sel in code_selectors:
            try:
                el = page.locator(sel).first
                if el.is_visible(timeout=1500):
                    el.fill(_totp_code)
                    filled = True
                    logger.info("Filled 2FA code into: %s", sel)
                    break
            except Exception:
                continue
        if not filled:
            raise Exception("Could not find 2FA input field")

        be.human_delay(0.4, 0.8)

        # Click confirm — try multiple selectors then fall back to Enter
        confirm_selectors = [
            'button:has-text("Confirm")',
            'div[role="button"]:has-text("Confirm")',
            'button:has-text("Continue")',
            'div[role="button"]:has-text("Continue")',
            'button[type="submit"]',
        ]
        clicked = False
        for sel in confirm_selectors:
            try:
                el = page.locator(sel).first
                if el.is_visible(timeout=1500):
                    el.click()
                    clicked = True
                    logger.info("Submitted 2FA via: %s", sel)
                    break
            except Exception:
                continue
        if not clicked:
            logger.info("No confirm button matched — pressing Enter")
            page.keyboard.press("Enter")

        _set_detail("Verifying code — approve the login in your Instagram app if it asks…")
        try:
            page.wait_for_url(lambda u: "two_factor" not in u, timeout=15000)
        except Exception:
            pass
        be.human_delay(1, 2)

        if "two_factor" in page.url:
            login_state.update(status="error",
                               error="2FA code rejected by Instagram — please try again")
            return

        _finish_login(page, username)
    except Exception as e:
        login_state.update(status="error", error=f"2FA failed: {e}")
        logger.error("2FA submission failed: %s", e)


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
    _reset_in_progress_login()
    be.submit_async(_do_login_sessionid, username, session_id)


def _do_login_sessionid(username: str, session_id: str):
    global _profile_key
    _profile_key = f"instagram_{username}"
    login_state.update(status="logging_in", error=None,
                       detail="Restoring your session…", username=username)

    with _page_lock:
        try:
            be.inject_cookie(_profile_key, "sessionid", session_id, ".instagram.com")
            page = be.get_page(_profile_key)
            _set_detail("Opening Instagram…")
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
    _set_detail("Finishing up — almost there…")
    # Dismiss prompts
    for text in ("Not now", "Not Now"):
        try:
            page.locator(f'button:has-text("{text}")').first.click(timeout=2500)
            be.human_delay(0.5, 1)
        except Exception:
            pass

    user_id = _extract_user_id(page, username)
    login_state.update(status="logged_in", detail=None,
                       user_id=user_id or username, username=username)
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
    return be.run_blocking(_follow_user_by_username, username, delay_min, delay_max)


def _follow_user_by_username(username: str, delay_min: int, delay_max: int) -> bool:
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
    return be.run_blocking(_unfollow_user_by_username, username, delay_min, delay_max)


def _unfollow_user_by_username(username: str, delay_min: int, delay_max: int) -> bool:
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


def _scrape_list(list_type: str, progress_cb=None) -> dict:
    return be.run_blocking(_scrape_list_impl, list_type, progress_cb)


def _scrape_list_impl(list_type: str, progress_cb=None) -> dict:
    """Scrape following or followers by intercepting XHR calls.

    progress_cb(list_type, count) is invoked as new users are scraped so
    callers can surface live progress to the UI.
    """
    username = login_state.get("username", "")
    if not username:
        raise Exception("Not logged in")

    results: dict = {}
    end_of_list = threading.Event()

    with _page_lock:
        page = be.get_page(_profile_key)

        def on_response(response):
            url = response.url
            # Instagram API v1: /api/v1/friendships/{id}/following/ or /followers/
            if not (("friendships" in url and list_type in url) or
                    ("graphql" in url and list_type in url)):
                return
            try:
                data = response.json()
            except Exception:
                return

            # v1 API format: {"users": [...], "next_max_id": "..."}
            users = data.get("users", [])
            # GraphQL edge format fallback
            if not users:
                gql_key = "edge_following" if list_type == "following" else "edge_followed_by"
                edges = (data.get("data", {})
                             .get(gql_key, {})
                             .get("edges", []))
                users = [e.get("node", {}) for e in edges]

            before = len(results)
            for user in users:
                pk = str(user.get("pk", "") or user.get("id", ""))
                uname = user.get("username", "")
                if pk and uname:
                    results[pk] = _UserInfo(
                        pk=pk,
                        username=uname,
                        full_name=user.get("full_name", ""),
                        profile_pic_url=user.get("profile_pic_url"),
                    )

            next_id = data.get("next_max_id")
            logger.info("%s XHR from %s: +%d users (total %d), next_max_id=%r",
                        list_type, url.split("?")[0], len(results) - before, len(results), next_id)

            # No next_max_id means we've hit the end of the list
            if "next_max_id" in data and not data["next_max_id"]:
                end_of_list.set()
                logger.info("End of %s list (next_max_id empty)", list_type)

        page.on("response", on_response)
        try:
            page.goto(f"{IG}/{username}/{list_type}/", wait_until="domcontentloaded")
            be.human_delay(2, 3)

            if "accounts/login" in page.url:
                raise Exception("Session expired — please log in again")

            # Wait for the modal dialog
            try:
                page.wait_for_selector('[role="dialog"]', timeout=8000)
                logger.info("Dialog found for %s/%s", username, list_type)
            except Exception:
                logger.warning("No dialog found for %s/%s — will try scrolling anyway", username, list_type)

            # The following modal has its own scroll container inside the dialog;
            # scrolling the window does nothing. We detect the right container by
            # actually TRYING to scroll each element and checking if scrollTop
            # changed — this works regardless of computed overflow style.
            _SCROLL_JS = """() => {
                const dialog = document.querySelector('[role="dialog"]');
                if (dialog) {
                    // Try every div/ul from deepest to shallowest
                    const els = [dialog, ...dialog.querySelectorAll('div, ul')];
                    for (let i = els.length - 1; i >= 0; i--) {
                        const el = els[i];
                        const before = el.scrollTop;
                        el.scrollTop += 1200;
                        if (el.scrollTop > before) return 'dialog-inner';
                    }
                    return 'dialog-no-scroll';
                }
                window.scrollBy(0, 1200);
                return 'window';
            }"""

            last_count = -1
            stalls = 0
            max_stalls = 10  # 10 × ~1.2s = ~12s of no new data before giving up

            while stalls < max_stalls and not end_of_list.is_set():
                scrolled_to = page.evaluate(_SCROLL_JS)
                be.human_delay(1.0, 1.5)

                current = len(results)
                if current == last_count:
                    stalls += 1
                    logger.debug("%s stall %d/%d at %d results (scroll target: %s)",
                                 list_type, stalls, max_stalls, current, scrolled_to)
                else:
                    stalls = 0
                    last_count = current
                    logger.info("Scraped %d %s so far (scroll target: %s)",
                                current, list_type, scrolled_to)
                    if progress_cb:
                        try:
                            progress_cb(list_type, current)
                        except Exception:
                            pass

            logger.info("Scrape complete: %d %s", len(results), list_type)

            try:
                page.keyboard.press("Escape")
                be.human_delay(0.4, 0.8)
            except Exception:
                pass

        finally:
            page.remove_listener("response", on_response)

    return results


def get_following(user_id: str, amount: int = 0, progress_cb=None) -> dict:
    return _scrape_list("following", progress_cb=progress_cb)


def get_followers(user_id: str, amount: int = 0, progress_cb=None) -> dict:
    return _scrape_list("followers", progress_cb=progress_cb)


def get_user_info_by_username(username: str) -> _UserInfo:
    return be.run_blocking(_get_user_info_by_username_impl, username)


def _get_user_info_by_username_impl(username: str) -> "_UserInfo":
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
