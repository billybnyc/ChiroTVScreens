#!/usr/bin/env python3
"""
session_keeper.py - keeps a site open in a visible browser window, quits and
relaunches the browser on a fixed cycle (default 24h), and logs back in
whenever the session has dropped.

Handles the awkward cases too:
  * frozen / unresponsive pages, via an escalating recovery ladder
  * "session expiring" modals with Continue / Log back in / Cancel buttons
  * native JS dialogs that block navigation
  * cookie banners and other overlays that sit on top of the page

Portable: same file runs on Windows, macOS and Linux. Everything
machine-specific lives in .env next to this script.

Usage:
    python session_keeper.py

Stop with Ctrl+C.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

try:
    from playwright.sync_api import (
        Error as PlaywrightError,
        TimeoutError as PlaywrightTimeout,
        sync_playwright,
    )
except ImportError:  # pragma: no cover
    sys.exit(
        "Playwright is not installed.\n"
        "Run:  pip install playwright  &&  python -m playwright install chromium"
    )

BASE_DIR = Path(__file__).resolve().parent
SCRIPT_BUILD = "v9 (2026-10-31) - verified central update URL"
LOG_DIR = BASE_DIR / "logs"
SHOT_DIR = LOG_DIR / "screenshots"


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

def load_dotenv(path: Path) -> None:
    """Minimal .env loader so there is no extra dependency. Real environment
    variables always win, which makes per-machine overrides easy."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def env_float(key: str, default: float) -> float:
    try:
        return float(env(key) or default)
    except ValueError:
        return default


def env_int(key: str, default: int) -> int:
    try:
        return int(float(env(key) or default))
    except ValueError:
        return default


def env_bool(key: str, default: bool = False) -> bool:
    value = env(key).lower()
    return default if not value else value in {"1", "true", "yes", "on"}


@dataclass
class Config:
    site_url: str
    login_url: str
    username: str
    password: str
    username_selector: str
    password_selector: str
    submit_selector: str
    logged_in_selector: str
    logged_out_selector: str
    keepalive_selector: str
    relogin_selector: str
    dismiss_selector: str
    refresh_hours: float
    check_minutes: float
    reload_on_check: bool
    post_login_clicks: list
    post_login_timeout_ms: int
    fullscreen_video: bool
    fullscreen_selector: str
    hide_cursor: bool
    final_keys: list
    final_keys_delay_ms: int
    update_manifest_url: str
    update_on_start: bool
    update_timeout_s: int
    update_allow_credentials: bool
    retry_seconds: float
    health_timeout_ms: int
    failures_before_restart: int
    screenshot_on_failure: bool
    profile_dir: Path
    browser_channel: str
    kiosk: bool
    headless: bool

    @classmethod
    def from_env(cls) -> "Config":
        site_url = env("SITE_URL")
        if not site_url:
            sys.exit("SITE_URL is not set. Copy .env.example to .env and fill it in.")

        return cls(
            site_url=site_url,
            login_url=env("LOGIN_URL") or site_url,
            username=env("SITE_USERNAME"),
            password=env("SITE_PASSWORD"),
            username_selector=env(
                "USERNAME_SELECTOR",
                'input[type="email"], input[name="email"], input[name="username"], '
                'input[id="username"], input[id="email"]',
            ),
            password_selector=env("PASSWORD_SELECTOR", 'input[type="password"]'),
            submit_selector=env("SUBMIT_SELECTOR",
                                'button[type="submit"], input[type="submit"]'),
            logged_in_selector=env("LOGGED_IN_SELECTOR"),
            logged_out_selector=env("LOGGED_OUT_SELECTOR", 'input[type="password"]'),
            keepalive_selector=env("KEEPALIVE_SELECTOR"),
            relogin_selector=env("RELOGIN_SELECTOR"),
            dismiss_selector=env("DISMISS_SELECTOR"),
            refresh_hours=env_float("REFRESH_HOURS", 24.0),
            check_minutes=env_float("CHECK_MINUTES", 15.0),
            reload_on_check=env_bool("RELOAD_ON_CHECK", True),
            post_login_clicks=[s.strip() for s in env("POST_LOGIN_CLICKS").split("|") if s.strip()],
            post_login_timeout_ms=env_int("POST_LOGIN_TIMEOUT_MS", 20_000),
            fullscreen_video=env_bool("FULLSCREEN_VIDEO", True),
            fullscreen_selector=env("FULLSCREEN_SELECTOR"),
            hide_cursor=env_bool("HIDE_CURSOR", True),
            final_keys=[s.strip() for s in env("FINAL_KEYS").split("|") if s.strip()],
            final_keys_delay_ms=env_int("FINAL_KEYS_DELAY_MS", 3_000),
            update_manifest_url=env("UPDATE_MANIFEST_URL"),
            update_on_start=env_bool("UPDATE_ON_START", True),
            update_timeout_s=env_int("UPDATE_TIMEOUT_S", 30),
            update_allow_credentials=env_bool("UPDATE_ALLOW_CREDENTIALS", False),
            retry_seconds=env_float("RETRY_SECONDS", 60.0),
            health_timeout_ms=env_int("HEALTH_TIMEOUT_MS", 10_000),
            failures_before_restart=env_int("FAILURES_BEFORE_RESTART", 3),
            screenshot_on_failure=env_bool("SCREENSHOT_ON_FAILURE", True),
            profile_dir=Path(env("PROFILE_DIR") or (BASE_DIR / "profile")).expanduser().resolve(),
            browser_channel=env("BROWSER_CHANNEL"),
            kiosk=env_bool("KIOSK", False),
            headless=env_bool("HEADLESS", False),
        )


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------

def setup_logging() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("session_keeper")
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s", "%Y-%m-%d %H:%M:%S")

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    logger.addHandler(stream)

    rotating = RotatingFileHandler(
        LOG_DIR / "session_keeper.log", maxBytes=1_000_000, backupCount=5, encoding="utf-8"
    )
    rotating.setFormatter(fmt)
    logger.addHandler(rotating)
    return logger


log = setup_logging()


def snapshot(page, cfg: Config, tag: str) -> None:
    """Save a screenshot so you can see what the screen looked like when it
    misbehaved. Best-effort - a truly wedged renderer may refuse."""
    if not cfg.screenshot_on_failure or page is None:
        return
    try:
        SHOT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = SHOT_DIR / f"{stamp}-{tag}.png"
        page.screenshot(path=str(path), timeout=10_000)
        log.info("Screenshot saved: %s", path.name)
        shots = sorted(SHOT_DIR.glob("*.png"))
        for old in shots[:-30]:          # keep the last 30
            old.unlink(missing_ok=True)
    except Exception:
        log.debug("Could not capture a screenshot.")


# --------------------------------------------------------------------------
# Page state helpers
# --------------------------------------------------------------------------

def visible(page, selector: str, timeout: int = 2_000) -> bool:
    """Safe visibility test - never raises, never blocks for long."""
    if not selector or page is None or page.is_closed():
        return False
    try:
        return page.locator(selector).first.is_visible(timeout=timeout)
    except Exception:
        return False


def click_if_visible(page, selector: str, label: str, timeout: int = 5_000) -> bool:
    if not visible(page, selector):
        return False
    try:
        page.locator(selector).first.click(timeout=timeout)
        log.info("Clicked %s.", label)
        return True
    except Exception as exc:
        log.warning("Found %s but could not click it (%s).", label, exc)
        return False


def is_responsive(page, cfg: Config) -> bool:
    """Ask the page's own JavaScript thread to answer. If it can't within the
    health timeout, the tab is hung - a reload alone often won't save it."""
    if page is None or page.is_closed():
        return False
    try:
        page.wait_for_function("() => true", timeout=cfg.health_timeout_ms)
        return True
    except PlaywrightTimeout:
        log.warning("Page is not responding (JS thread hung).")
        return False
    except PlaywrightError as exc:
        log.warning("Health probe failed: %s", exc)
        return False


def handle_overlays(page, cfg: Config) -> str:
    """Deal with anything sitting on top of the page before judging login state.

    Returns: 'keepalive' | 'relogin' | 'dismissed' | 'none'
    """
    if page is None or page.is_closed():
        return "none"

    # 1. "Your session is about to expire - Continue?" Clicking this is the
    #    cheapest possible fix: it keeps the existing session instead of
    #    forcing a fresh login.
    if click_if_visible(page, cfg.keepalive_selector, "the keep-session-alive button"):
        try:
            page.wait_for_timeout(2_000)
        except PlaywrightError:
            pass
        return "keepalive"

    # 2. "Session expired - Log back in". The login form is behind this button.
    if click_if_visible(page, cfg.relogin_selector, "the log-back-in button"):
        try:
            page.wait_for_load_state("domcontentloaded", timeout=30_000)
        except PlaywrightError:
            pass
        return "relogin"

    # 3. Cookie banners, "we've updated our terms", and similar clutter.
    if click_if_visible(page, cfg.dismiss_selector, "a dismissable overlay"):
        return "dismissed"

    return "none"


def is_logged_in(page, cfg: Config) -> bool:
    """Prefer an explicit logged-in marker; otherwise treat a visible password
    field as proof that we've been bounced to the login screen."""
    if page is None or page.is_closed():
        return False
    if cfg.logged_in_selector:
        return visible(page, cfg.logged_in_selector, timeout=5_000)
    return not visible(page, cfg.logged_out_selector, timeout=5_000)


def find_username_field(page, cfg: Config):
    """Locate the username box. Tries the configured selector first, then falls
    back to 'the visible text input sitting just above the password field',
    which works on old hand-rolled forms whose inputs have odd names."""
    if cfg.username_selector and visible(page, cfg.username_selector, 3_000):
        return page.locator(cfg.username_selector).first

    log.info("Username selector did not match - locating the field by position.")
    pw_box = None
    try:
        pw_box = page.locator(cfg.password_selector).first.bounding_box()
    except Exception:
        pass

    candidates = page.locator(
        'input[type="text"], input[type="email"], input:not([type])'
    )
    best, best_y = None, -1.0
    try:
        total = min(candidates.count(), 25)
    except Exception:
        return None

    for i in range(total):
        field = candidates.nth(i)
        try:
            if not field.is_visible(timeout=1_000):
                continue
            box = field.bounding_box()
        except Exception:
            continue
        if not box:
            continue
        # Must sit above (or level with) the password box.
        if pw_box and box["y"] > pw_box["y"] + 5:
            continue
        if box["y"] > best_y:
            best, best_y = field, box["y"]

    return best


def page_changed(page, before_url: str, timeout_ms: int = 12_000) -> bool:
    """Wait for evidence that a submit actually did something: either the URL
    changed, or the password field stopped being visible."""
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        try:
            if page.url != before_url:
                return True
            if not page.locator('input[type="password"]').first.is_visible(timeout=500):
                return True
        except PlaywrightError:
            return True  # navigation in flight invalidates the handle
        try:
            page.wait_for_timeout(400)
        except PlaywrightError:
            return True
    return False


def dump_login_form(page, cfg: Config) -> None:
    """Write the login form's markup to the log so the real field names can be
    read off instead of guessed at. The password value is redacted."""
    try:
        html = page.evaluate(
            """() => {
                const pw = document.querySelector('input[type=password]');
                if (!pw) return 'NO PASSWORD FIELD ON PAGE';
                const el = pw.closest('form') || pw.parentElement;
                return el ? el.outerHTML.slice(0, 4000) : 'NO ENCLOSING ELEMENT';
            }"""
        )
        if cfg.password and html:
            html = html.replace(cfg.password, "[REDACTED]")
        if cfg.username and html:
            html = html.replace(cfg.username, "[USERNAME]")
        log.error("--- login form markup (paste this if asking for help) ---")
        log.error("%s", html)
        log.error("--- end of form markup ---")
    except Exception as exc:
        log.warning("Could not read the form markup: %s", exc)


def submit_login(page, cfg: Config) -> bool:
    """Submit the form. Old hand-rolled forms vary wildly, so this escalates
    through everything that can plausibly trigger a submit."""
    before = page.url

    candidates = [
        cfg.submit_selector,                 # whatever .env specifies
        'input[type="submit"]',
        'button[type="submit"]',
        'input[type="image"]',               # image buttons on older forms
        'input[value="Login" i]',
        'input[value="Log in" i]',
        'input[value="Sign in" i]',
        'input[value="Submit" i]',
        'button',                            # a bare <button> defaults to submit
    ]

    for sel in candidates:
        if not sel or not visible(page, sel, 1_200):
            continue
        try:
            page.locator(sel).first.click(timeout=10_000)
            log.info("Clicked submit control: %s", sel)
            if page_changed(page, before):
                return True
            log.info("That click did not change the page; trying the next option.")
        except PlaywrightError:
            continue

    # Enter from the password field
    try:
        page.locator(cfg.password_selector).first.press("Enter", timeout=5_000)
        log.info("Pressed Enter in the password field.")
        if page_changed(page, before):
            return True
    except PlaywrightError:
        pass

    # Last resort: submit the form element directly from JavaScript
    try:
        submitted = page.evaluate(
            """() => {
                const pw = document.querySelector('input[type=password]');
                const f = pw && pw.closest('form');
                if (!f) return false;
                if (typeof f.requestSubmit === 'function') { f.requestSubmit(); }
                else { f.submit(); }
                return true;
            }"""
        )
        if submitted:
            log.info("Submitted the form directly via JavaScript.")
            if page_changed(page, before):
                return True
    except PlaywrightError as exc:
        log.warning("JavaScript submit failed: %s", exc)

    log.error("Nothing submitted the form - the page never changed.")
    return False


def perform_login(page, cfg: Config) -> bool:
    if not cfg.username or not cfg.password:
        log.error("Login needed but SITE_USERNAME / SITE_PASSWORD are not set.")
        return False

    log.info("Logging in...")

    # The login form may be behind a "log back in" button on a timeout modal.
    if not visible(page, cfg.password_selector, timeout=3_000):
        handle_overlays(page, cfg)

    if not visible(page, cfg.password_selector, timeout=3_000):
        try:
            page.goto(cfg.login_url, wait_until="domcontentloaded", timeout=60_000)
        except PlaywrightError as exc:
            log.error("Could not reach the login page: %s", exc)
            return False

    user_field = find_username_field(page, cfg)
    if user_field is None:
        log.error("Could not find the username field on the login page.")
        snapshot(page, cfg, "no-username-field")
        return False

    try:
        user_field.fill(cfg.username, timeout=30_000)
        page.locator(cfg.password_selector).first.fill(cfg.password, timeout=30_000)
    except PlaywrightError as exc:
        log.error("Could not fill the login form: %s", exc)
        snapshot(page, cfg, "login-form-failed")
        return False

    if not submit_login(page, cfg):
        snapshot(page, cfg, "login-submit-failed")
        dump_login_form(page, cfg)
        return False

    try:
        page.wait_for_load_state("networkidle", timeout=45_000)
    except PlaywrightTimeout:
        pass  # Some apps hold long-poll connections open; not an error.

    handle_overlays(page, cfg)

    landed = page.url
    log.info("After submit, landed on: %s", landed)

    # Only navigate onward when SITE_URL is somewhere other than the login
    # page. When the two are the same, going "back to SITE_URL" would return
    # us to the login form and make a successful login look like a failure.
    login_is_home = cfg.site_url.rstrip("/") == cfg.login_url.rstrip("/")
    if not login_is_home and landed.rstrip("/") != cfg.site_url.rstrip("/"):
        try:
            page.goto(cfg.site_url, wait_until="domcontentloaded", timeout=60_000)
        except PlaywrightError:
            pass

    if is_logged_in(page, cfg):
        log.info("Login succeeded.")
        # If SITE_URL was just the login page and the site redirected us
        # somewhere real, adopt that for the rest of this run so the browser
        # sits on the actual content instead of the login screen.
        if login_is_home and landed.rstrip("/") != cfg.login_url.rstrip("/"):
            cfg.site_url = landed
            log.info("Adopting %s as the page to hold open.", landed)
            log.info("Tip: put SITE_URL=%s in .env to make that permanent.", landed)
        return True

    log.error("Login submitted but the page still looks logged out.")
    log.error("Current URL: %s", page.url)
    snapshot(page, cfg, "login-rejected")
    dump_login_form(page, cfg)
    return False


def selectors_for_label(label: str) -> list:
    """Turn a plain button label from .env into selectors worth trying.
    A value starting with "css:" is used verbatim instead."""
    if label.lower().startswith("css:"):
        return [label[4:].strip()]
    esc = label.replace('"', '\\"')
    return [
        'a:text-is("%s")' % esc,          # exact text, most precise first
        'button:text-is("%s")' % esc,
        'input[value="%s" i]' % esc,
        '[title="%s" i]' % esc,
        'img[alt="%s" i]' % esc,          # image buttons on older sites
        'a:has-text("%s")' % esc,         # substring fallbacks
        'button:has-text("%s")' % esc,
        '*:text-is("%s")' % esc,
    ]


def click_label(page, label: str, timeout_ms: int):
    """Wait for a labelled control to appear and click it. Returns the
    selector that worked, or None."""
    sels = selectors_for_label(label)
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        for sel in sels:
            try:
                loc = page.locator(sel).first
                if loc.is_visible(timeout=400):
                    loc.click(timeout=10_000)
                    return sel
            except PlaywrightError:
                continue
        try:
            page.wait_for_timeout(500)
        except PlaywrightError:
            break
    return None


def dump_clickables(page, cfg: Config) -> None:
    """List the visible clickable things on the page, so a missing step can be
    matched against what is actually there."""
    try:
        items = page.evaluate(
            """() => {
                const out = [];
                const sel = 'a, button, input[type=submit], input[type=button], input[type=image], [role=button]';
                document.querySelectorAll(sel).forEach(el => {
                    const r = el.getBoundingClientRect();
                    if (r.width < 1 || r.height < 1) return;
                    const label = (el.innerText || el.value || el.alt || el.title || '').trim();
                    if (label) out.push(el.tagName.toLowerCase() + ': ' + label.slice(0, 80));
                });
                return out.slice(0, 40);
            }"""
        )
        log.error("--- visible clickable items on this page ---")
        for i in items:
            log.error("    %s", i)
        log.error("--- end of list ---")
    except Exception as exc:
        log.warning("Could not list clickable items: %s", exc)


def press_keys(page, keys: list, delay_ms: int = 0) -> None:
    """Send one or more keyboard keys to the page, in order.
    Names are Playwright key names: Escape, Enter, Space, F11, ArrowRight..."""
    if not keys:
        return
    if delay_ms:
        try:
            page.wait_for_timeout(delay_ms)
        except PlaywrightError:
            pass
    for key in keys:
        try:
            page.keyboard.press(key)
            log.info("Pressed key: %s", key)
        except PlaywrightError as exc:
            log.warning("Could not press %s: %s", key, exc)
        try:
            page.wait_for_timeout(600)
        except PlaywrightError:
            pass


def run_post_login_steps(page, cfg: Config, required: bool = True) -> None:
    """Click through whatever stands between the login and the actual content
    (for ChiroTV: Play ChiroTV, then play the newest version).

    required=False is used on routine re-checks, where the steps have usually
    already been done and the controls are simply not on screen any more.
    """
    if not cfg.post_login_clicks:
        return

    for label in cfg.post_login_clicks:
        # A "key:NAME" entry presses a keyboard key instead of clicking.
        if label.lower().startswith("key:"):
            press_keys(page, [label[4:].strip()])
            continue
        # A "wait:1500" entry pauses for that many milliseconds.
        if label.lower().startswith("wait:"):
            try:
                page.wait_for_timeout(int(label[5:].strip()))
                log.info("Waited %s ms", label[5:].strip())
            except (PlaywrightError, ValueError):
                pass
            continue
        timeout = cfg.post_login_timeout_ms if required else 2_500
        sel = click_label(page, label, timeout)
        if sel:
            log.info("Post-login step: clicked %s  (matched %s)", label, sel)
            try:
                page.wait_for_load_state("domcontentloaded", timeout=30_000)
            except PlaywrightError:
                pass
            try:
                page.wait_for_timeout(2_000)
            except PlaywrightError:
                pass
        elif required:
            log.warning("Post-login step not found: %s", label)
            snapshot(page, cfg, "post-login-step-missing")
            dump_clickables(page, cfg)


def go_fullscreen(page, cfg: Config) -> None:
    """Make the content itself fill the screen, on top of the window already
    being fullscreen. Tries a named control first, then the Fullscreen API on
    whatever element is actually playing."""
    if not cfg.fullscreen_video:
        return

    if cfg.fullscreen_selector:
        sel = click_label(page, cfg.fullscreen_selector, 5_000)
        if sel:
            log.info("Fullscreen: clicked %s (matched %s)", cfg.fullscreen_selector, sel)
        else:
            log.info("Fullscreen control not found: %s", cfg.fullscreen_selector)

    # The Fullscreen API needs a recent user gesture. The post-login clicks
    # usually provide one; if the browser refuses, the window is still
    # fullscreen from --kiosk, so this is an enhancement, not a requirement.
    try:
        result = page.evaluate(
            """() => {
                const pick = document.querySelector('video')
                          || document.querySelector('iframe')
                          || document.querySelector('embed, object')
                          || document.documentElement;
                if (!pick) return 'no element found';
                const fn = pick.requestFullscreen
                        || pick.webkitRequestFullscreen
                        || pick.mozRequestFullScreen
                        || pick.msRequestFullscreen;
                if (!fn) return 'no fullscreen API on ' + pick.tagName.toLowerCase();
                try { fn.call(pick); } catch (e) { return 'refused: ' + e.message; }
                return 'requested on <' + pick.tagName.toLowerCase() + '>';
            }"""
        )
        log.info("Fullscreen: %s", result)
    except PlaywrightError as exc:
        log.warning("Fullscreen request failed: %s", exc)

    # A video that is playing should also fill its own box rather than letterbox.
    try:
        page.evaluate(
            """() => {
                const v = document.querySelector('video');
                if (v) { v.style.width = '100%'; v.style.height = '100%'; }
            }"""
        )
    except PlaywrightError:
        pass

    if cfg.hide_cursor:
        try:
            page.add_style_tag(content="* { cursor: none !important; }")
            log.info("Mouse pointer hidden.")
        except PlaywrightError:
            pass


def ensure_logged_in(page, cfg: Config) -> bool:
    handle_overlays(page, cfg)
    if is_logged_in(page, cfg):
        return True
    return perform_login(page, cfg)


# --------------------------------------------------------------------------
# Recovery ladder
# --------------------------------------------------------------------------

def recover(context, page, cfg: Config):
    """Escalate through progressively heavier fixes for a stuck page.

    Returns a usable page, or None if the browser itself needs restarting.
    """
    snapshot(page, cfg, "stuck")

    # Step 1 - soft reload.
    log.info("Recovery 1/3: reloading the page.")
    try:
        page.reload(wait_until="domcontentloaded", timeout=45_000)
        if is_responsive(page, cfg):
            log.info("Reload recovered the page.")
            return page
    except PlaywrightError as exc:
        log.warning("Reload failed: %s", exc)

    # Step 2 - throw away the current document and navigate fresh.
    log.info("Recovery 2/3: navigating to the site URL from scratch.")
    try:
        page.goto("about:blank", timeout=15_000)
        page.goto(cfg.site_url, wait_until="domcontentloaded", timeout=60_000)
        if is_responsive(page, cfg):
            log.info("Fresh navigation recovered the page.")
            return page
    except PlaywrightError as exc:
        log.warning("Navigation failed: %s", exc)

    # Step 3 - discard the tab entirely and open a new one. This kills a hung
    # renderer process, which a reload cannot do.
    log.info("Recovery 3/3: closing the tab and opening a new one.")
    try:
        try:
            page.close()
        except PlaywrightError:
            pass
        fresh = context.new_page()
        attach_dialog_handler(fresh, cfg)
        fresh.goto(cfg.site_url, wait_until="domcontentloaded", timeout=60_000)
        if is_responsive(fresh, cfg):
            log.info("New tab recovered the session.")
            return fresh
    except PlaywrightError as exc:
        log.warning("New tab failed: %s", exc)

    log.error("Recovery exhausted - the browser will be restarted.")
    return None


def attach_dialog_handler(page, cfg: Config) -> None:
    """Native alert()/confirm() dialogs block navigation until answered, which
    looks exactly like a frozen screen. Dismiss them automatically."""
    def on_dialog(dialog):
        log.warning("Dismissing a browser dialog (%s): %s", dialog.type, dialog.message[:120])
        try:
            dialog.dismiss()
        except PlaywrightError:
            pass
    page.on("dialog", on_dialog)
    page.on("crash", lambda _: log.error("The page crashed."))


# --------------------------------------------------------------------------
# Session cycle
# --------------------------------------------------------------------------

def browser_args(cfg: Config) -> list:
    args = [
        "--disable-session-crashed-bubble",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-infobars",
    ]
    if cfg.kiosk:
        # Kiosk: fullscreen with no address bar, tabs or window chrome.
        args.append("--kiosk")
    else:
        args += ["--start-maximized", "--start-fullscreen"]
    return args


def run_one_session(cfg: Config) -> None:
    """Launch the browser, hold the site open for refresh_hours, then quit.
    Returns early if the page becomes unrecoverable."""
    cfg.profile_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        launch_kwargs = dict(
            user_data_dir=str(cfg.profile_dir),
            headless=cfg.headless,
            args=browser_args(cfg),
            no_viewport=True,
            ignore_default_args=["--enable-automation"],
        )
        if cfg.browser_channel:
            launch_kwargs["channel"] = cfg.browser_channel

        log.info("Launching browser (profile: %s)", cfg.profile_dir)
        context = pw.chromium.launch_persistent_context(**launch_kwargs)

        try:
            page = context.pages[0] if context.pages else context.new_page()
            attach_dialog_handler(page, cfg)
            page.goto(cfg.site_url, wait_until="domcontentloaded", timeout=60_000)
            ensure_logged_in(page, cfg)
            run_post_login_steps(page, cfg, required=True)
            go_fullscreen(page, cfg)
            press_keys(page, cfg.final_keys, cfg.final_keys_delay_ms)

            deadline = time.monotonic() + cfg.refresh_hours * 3600
            interval = max(cfg.check_minutes * 60, 30)
            failures = 0

            log.info(
                "Session open. Full browser restart in %.1f hour(s); "
                "health check every %.0f minute(s).",
                cfg.refresh_hours, interval / 60,
            )

            while time.monotonic() < deadline:
                time.sleep(min(interval, max(deadline - time.monotonic(), 0)))
                if time.monotonic() >= deadline:
                    break

                try:
                    # 1. Is the page alive at all?
                    if not is_responsive(page, cfg):
                        page = recover(context, page, cfg)
                        if page is None:
                            log.warning("Restarting the browser early.")
                            return
                        failures = 0
                        ensure_logged_in(page, cfg)
                        run_post_login_steps(page, cfg, required=True)
                        go_fullscreen(page, cfg)
                        press_keys(page, cfg.final_keys, cfg.final_keys_delay_ms)

                    # 2. Clear anything sitting on top of it. A keep-alive
                    #    click means the session is fine - don't reload over it.
                    action = handle_overlays(page, cfg)

                    # 3. Routine refresh.
                    if cfg.reload_on_check and action != "keepalive":
                        page.reload(wait_until="domcontentloaded", timeout=60_000)

                    # 4. Are we still authenticated?
                    if ensure_logged_in(page, cfg):
                        failures = 0
                        # Cheap no-op when the stream is already playing.
                        run_post_login_steps(page, cfg, required=False)
                    else:
                        failures += 1
                        log.warning("Not logged in after check (%d in a row).", failures)

                except PlaywrightError as exc:
                    failures += 1
                    log.warning("Check failed (%d in a row): %s", failures, exc)
                    snapshot(page, cfg, "check-failed")

                if failures >= cfg.failures_before_restart:
                    log.error(
                        "%d consecutive failed checks - restarting the browser early.",
                        failures,
                    )
                    return

            log.info("Cycle complete - quitting the browser.")
        finally:
            try:
                context.close()
            except PlaywrightError as exc:
                log.warning("Browser did not close cleanly: %s", exc)



# --------------------------------------------------------------------------
# Central updates
#
# The keeper can pull its own script and its non-secret settings from a URL
# you control, so a change is published once instead of being redeployed to
# every screen. Safety rules, in order of importance:
#
#   * a downloaded script must COMPILE before it replaces the running one
#   * the previous script is kept as .bak and restored if the new one fails
#   * a sha256 in the manifest is verified when present
#   * credentials are ignored from the manifest unless explicitly allowed
#
# Whoever controls the manifest URL can run code on these machines. Use a
# host only you can write to, and always https.
# --------------------------------------------------------------------------

RESTART_EXIT_CODE = 86           # the launcher relaunches on this code
STATE_FILE = BASE_DIR / "update_state.json"
CENTRAL_ENV = BASE_DIR / "central.env"

CREDENTIAL_KEYS = {"SITE_USERNAME", "SITE_PASSWORD"}
# Settings that must stay machine-local - pushing these centrally would point
# every screen at one profile directory or break a single machine's paths.
LOCAL_ONLY_KEYS = {"PROFILE_DIR", "UPDATE_MANIFEST_URL"}


def read_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_state(state: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception as exc:
        log.warning("Could not write update state: %s", exc)


def fetch(url: str, timeout: int) -> bytes:
    req = Request(url, headers={"User-Agent": "session-keeper", "Cache-Control": "no-cache"})
    with urlopen(req, timeout=timeout) as resp:
        return resp.read()


def apply_central_settings(settings: dict, cfg: Config) -> bool:
    """Write pushed settings to central.env. Returns True if they changed."""
    if not isinstance(settings, dict):
        return False

    clean = {}
    for key, value in settings.items():
        key = str(key).strip().upper()
        if key in LOCAL_ONLY_KEYS:
            log.info("Ignoring machine-local setting from manifest: %s", key)
            continue
        if key in CREDENTIAL_KEYS and not cfg.update_allow_credentials:
            log.info("Ignoring credential from manifest: %s "
                     "(set UPDATE_ALLOW_CREDENTIALS=true to permit)", key)
            continue
        clean[key] = str(value)

    if not clean:
        return False

    lines = ["# Written by the central update. Do not edit - edit the manifest.",
             "# Local .env still supplies anything not listed here.", ""]
    lines += ["%s=%s" % (k, v) for k, v in sorted(clean.items())]
    body = "\n".join(lines) + "\n"

    previous = CENTRAL_ENV.read_text(encoding="utf-8") if CENTRAL_ENV.is_file() else ""
    if body == previous:
        return False

    CENTRAL_ENV.write_text(body, encoding="utf-8")
    log.info("Central settings updated (%d key(s)): %s", len(clean), ", ".join(sorted(clean)))
    return True


def apply_script_update(url: str, expected_sha: str, cfg: Config) -> bool:
    """Download, validate and install a new keeper script.
    Returns True only when the running file was actually replaced."""
    try:
        payload = fetch(url, cfg.update_timeout_s)
    except (URLError, OSError) as exc:
        log.warning("Could not download the new script: %s", exc)
        return False

    if expected_sha:
        actual = hashlib.sha256(payload).hexdigest()
        if actual.lower() != expected_sha.strip().lower():
            log.error("Script checksum mismatch - refusing the update.")
            log.error("  expected %s", expected_sha)
            log.error("  received %s", actual)
            return False

    me = Path(__file__).resolve()
    candidate = me.with_suffix(".py.new")
    try:
        candidate.write_bytes(payload)
    except OSError as exc:
        log.warning("Could not write the downloaded script: %s", exc)
        return False

    # A new script that does not compile would take every screen down, so it
    # never gets installed.
    import py_compile
    try:
        py_compile.compile(str(candidate), doraise=True)
    except Exception as exc:
        log.error("Downloaded script does not compile - update refused: %s", exc)
        candidate.unlink(missing_ok=True)
        return False

    backup = me.with_suffix(".py.bak")
    try:
        backup.unlink(missing_ok=True)
        me.replace(backup)
        candidate.replace(me)
    except OSError as exc:
        log.error("Could not install the new script: %s", exc)
        if backup.is_file() and not me.is_file():
            backup.replace(me)          # put the old one back
        return False

    log.info("New keeper script installed (previous kept as %s).", backup.name)
    return True


def check_for_update(cfg: Config) -> bool:
    """Look for a newer script or settings. Returns True if the process
    should restart to pick them up."""
    if not cfg.update_manifest_url:
        return False

    log.info("Checking for updates: %s", cfg.update_manifest_url)
    try:
        raw = fetch(cfg.update_manifest_url, cfg.update_timeout_s)
        manifest = json.loads(raw.decode("utf-8"))
    except (URLError, OSError) as exc:
        log.warning("Update check failed (continuing with what is installed): %s", exc)
        return False
    except ValueError as exc:
        log.warning("Update manifest is not valid JSON: %s", exc)
        return False

    state = read_state()
    restart = False

    settings_changed = apply_central_settings(manifest.get("settings"), cfg)

    version = str(manifest.get("version", "")).strip()
    script = manifest.get("script") or {}
    script_url = str(script.get("url", "")).strip()

    if version and script_url and version != str(state.get("script_version", "")):
        log.info("Manifest offers script version %s (running %s).",
                 version, state.get("script_version") or SCRIPT_BUILD)
        if apply_script_update(script_url, str(script.get("sha256", "")), cfg):
            state["script_version"] = version
            write_state(state)
            restart = True
    elif version and script_url:
        log.info("Script is current (version %s).", version)
    elif version:
        log.info("Manifest %s carries settings only - no script update.", version)

    if settings_changed and not restart:
        restart = True
        log.info("Settings changed - restarting to apply them.")

    note = manifest.get("note")
    if note:
        log.info("Manifest note: %s", str(note)[:300])

    return restart


def main() -> int:
    # .env wins where both exist; config.env is the visible-named fallback,
    # since Windows Explorer hides dotfiles and some copy/zip tools drop them.
    # Precedence: real environment variables, then centrally pushed settings,
    # then the local .env, then the shipped defaults. First one loaded wins.
    load_dotenv(CENTRAL_ENV)
    load_dotenv(BASE_DIR / ".env")
    load_dotenv(BASE_DIR / "config.env")
    cfg = Config.from_env()

    log.info("=" * 60)
    log.info("session_keeper BUILD: %s", SCRIPT_BUILD)
    if cfg.update_on_start and check_for_update(cfg):
        log.info("Restarting to run the update.")
        return RESTART_EXIT_CODE
    log.info("session_keeper starting - target: %s", cfg.site_url)

    while True:
        try:
            run_one_session(cfg)
            # The 24-hour restart is the natural moment to pick up changes.
            if check_for_update(cfg):
                log.info("Restarting to run the update.")
                return RESTART_EXIT_CODE
        except KeyboardInterrupt:
            log.info("Stopped by user.")
            return 0
        except Exception as exc:  # keep the loop alive across crashes
            log.exception("Session failed: %s", exc)
            log.info("Retrying in %.0f second(s).", cfg.retry_seconds)
            try:
                time.sleep(cfg.retry_seconds)
            except KeyboardInterrupt:
                log.info("Stopped by user.")
                return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
