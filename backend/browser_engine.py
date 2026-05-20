"""
Core Playwright browser engine.
Manages persistent browser contexts, stealth patches, and human-like helpers.
One browser process is shared; each account gets its own context (cookies, storage).
"""
import logging
import os
import random
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("socialreach.browser")

PROFILES_DIR = Path(os.environ.get("SOCIALREACH_PROFILES_DIR", "./browser_profiles"))
HEADLESS = os.environ.get("SOCIALREACH_HEADLESS", "true").lower() != "false"

_lock = threading.Lock()
_pw = None
_browser = None
_contexts: dict = {}   # profile_key -> BrowserContext
_pages: dict = {}      # profile_key -> Page


def _ensure_browser():
    global _pw, _browser
    if _browser is not None:
        return
    from playwright.sync_api import sync_playwright
    _pw = sync_playwright().start()
    _browser = _pw.chromium.launch(
        headless=HEADLESS,
        args=[
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--disable-extensions",
        ],
    )
    logger.info("Chromium launched (headless=%s)", HEADLESS)


def get_context(profile_key: str):
    with _lock:
        if profile_key in _contexts:
            return _contexts[profile_key]

        _ensure_browser()

        profile_dir = PROFILES_DIR / profile_key
        profile_dir.mkdir(parents=True, exist_ok=True)
        storage_file = profile_dir / "storage.json"

        ctx = _browser.new_context(
            viewport={"width": 414, "height": 896},
            user_agent=(
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                "Version/17.4 Mobile/15E148 Safari/604.1"
            ),
            locale="en-US",
            timezone_id="America/New_York",
            storage_state=str(storage_file) if storage_file.exists() else None,
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )

        # Patch common headless detection tells
        ctx.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
            window.chrome = { runtime: {} };
        """)

        _contexts[profile_key] = ctx
        return ctx


def get_page(profile_key: str):
    """Get the single persistent page for this profile, creating if needed."""
    with _lock:
        page = _pages.get(profile_key)
        if page is None or page.is_closed():
            ctx = get_context(profile_key)
            page = ctx.new_page()
            try:
                from playwright_stealth import stealth_sync
                stealth_sync(page)
            except ImportError:
                pass
            _pages[profile_key] = page
        return page


def save_context(profile_key: str):
    with _lock:
        if profile_key not in _contexts:
            return
        profile_dir = PROFILES_DIR / profile_key
        profile_dir.mkdir(parents=True, exist_ok=True)
        _contexts[profile_key].storage_state(path=str(profile_dir / "storage.json"))
        logger.debug("Saved browser context for %s", profile_key)


def inject_cookie(profile_key: str, name: str, value: str, domain: str):
    ctx = get_context(profile_key)
    ctx.add_cookies([{
        "name": name, "value": value, "domain": domain,
        "path": "/", "httpOnly": True, "secure": True,
    }])


# ---------------------------------------------------------------------------
# Human-like interaction helpers
# ---------------------------------------------------------------------------

def human_type(page, selector: str, text: str):
    el = page.locator(selector).first
    el.scroll_into_view_if_needed()
    el.click()
    el.fill("")
    for char in text:
        page.keyboard.type(char)
        time.sleep(random.uniform(0.05, 0.17))


def human_click(page, selector: str):
    el = page.locator(selector).first
    el.scroll_into_view_if_needed()
    time.sleep(random.uniform(0.15, 0.45))
    el.click()


def human_delay(min_s: float = 0.8, max_s: float = 2.5):
    time.sleep(random.uniform(min_s, max_s))


def scroll_element(page, js_selector: str, by: int = 600):
    page.evaluate(f"""
        const el = {js_selector};
        if (el) el.scrollBy(0, {by});
    """)


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

def close_all():
    global _pw, _browser, _contexts, _pages
    with _lock:
        for p in _pages.values():
            try: p.close()
            except: pass
        _pages.clear()
        for ctx in _contexts.values():
            try: ctx.close()
            except: pass
        _contexts.clear()
        if _browser:
            try: _browser.close()
            except: pass
        if _pw:
            try: _pw.stop()
            except: pass
        _pw = _browser = None
        logger.info("Browser engine shut down")
