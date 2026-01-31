"""QR code-based login for GRVT."""
from __future__ import annotations

import base64
import json
import time
import re
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlparse

import cv2
import numpy as np

from grvt_volume_boost.auth.cookies import save_cookie_cache
from grvt_volume_boost.playwright_compat import run_sync_playwright
from grvt_volume_boost.runtime import ensure_playwright_browsers_path
from grvt_volume_boost.settings import EDGE_URL, ORIGIN, SESSION_DIR


def _launch_chromium(p, *, headless: bool, args: list[str] | None = None, channel: str | None = None):
    """Launch Chromium with an optional system channel, falling back to bundled Chromium.

    - `channel="chrome"` uses the locally installed Chrome browser (often better vs automation checks).
    - If that channel isn't available (common on fresh machines / packaged EXEs), fall back to
      Playwright's bundled Chromium so the app still works one-click.
    """
    launch_kwargs: dict = {"headless": bool(headless)}
    if args:
        launch_kwargs["args"] = list(args)
    if channel:
        try:
            return p.chromium.launch(channel=channel, **launch_kwargs)
        except Exception:
            # Fallback to bundled Chromium.
            pass
    return p.chromium.launch(**launch_kwargs)


def decode_qr_image(image_path: str | Path) -> str | None:
    """Decode QR code from image file. Returns URL or None."""
    img = cv2.imread(str(image_path))
    if img is None:
        return None
    detector = cv2.QRCodeDetector()
    data, _, _ = detector.detectAndDecode(img)
    if data:
        return data

    # OpenCV can fail on some QR versions/densities; fall back to PIL-based decoding.
    try:
        from PIL import Image
        pil_img = Image.open(str(image_path))
        return decode_qr_from_pil(pil_img)
    except Exception:
        return None


def decode_qr_from_pil(pil_image) -> str | None:
    """Decode QR code from PIL Image. Returns URL or None."""
    img_array = np.array(pil_image.convert("RGB"))
    img_bgr = cv2.cvtColor(img_array, cv2.COLOR_RGB2BGR)

    def _try_cv2(mat) -> str | None:
        try:
            detector = cv2.QRCodeDetector()
            data, _, _ = detector.detectAndDecode(mat)
            return data if data else None
        except Exception:
            return None

    # First attempt: raw image.
    data = _try_cv2(img_bgr)
    if data:
        return data

    # Robust attempts: try multiple scales and binarization strategies.
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    for scale in (1.25, 1.5, 2.0, 3.0, 4.0):
        try:
            resized = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST)
        except Exception:
            resized = gray

        for mat in (resized, cv2.GaussianBlur(resized, (3, 3), 0)):
            # Otsu threshold
            try:
                _, th = cv2.threshold(mat, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
                data = _try_cv2(th)
                if data:
                    return data
                data = _try_cv2(255 - th)
                if data:
                    return data
            except Exception:
                pass

            # Adaptive threshold
            try:
                th = cv2.adaptiveThreshold(mat, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 2)
                data = _try_cv2(th)
                if data:
                    return data
                data = _try_cv2(255 - th)
                if data:
                    return data
            except Exception:
                pass

    # Fallback to pyzbar if cv2 fails
    try:
        from pyzbar.pyzbar import decode
        results = decode(pil_image)
        if results:
            return results[0].data.decode()
    except ImportError:
        pass
    return None


def _dismiss_popups(page) -> None:
    """Best-effort dismissal for blocking modals/popups (e.g. trading competition)."""
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass

    # Try a few common close button patterns.
    candidates = [
        "button[aria-label='Close']",
        "button[aria-label='close']",
        "[role='dialog'] button[aria-label='Close']",
        "[role='dialog'] button:has-text('Close')",
    ]
    for sel in candidates:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                loc.click(timeout=800)
                return
        except Exception:
            pass

    # Some popups use an 'X' or '×' label.
    try:
        btn = page.get_by_role("button", name=re.compile(r"^(x|×|close)$", re.I)).first
        if btn.count() > 0:
            btn.click(timeout=800)
            return
    except Exception:
        pass


def _is_cloudflare_blocked(page) -> bool:
    """Best-effort detection of Cloudflare bot/challenge blocks."""
    try:
        url = (page.url or "").lower()
        if "cdn-cgi" in url or "cloudflare" in url:
            return True
    except Exception:
        pass
    try:
        title = (page.title() or "").lower()
        if "just a moment" in title or "attention required" in title:
            return True
    except Exception:
        pass
    try:
        body = (page.content() or "").lower()
        needles = [
            "you have been blocked",
            "attention required",
            "checking your browser",
            "cf-challenge",
            "/cdn-cgi/",
        ]
        return any(n in body for n in needles)
    except Exception:
        return False


def _wait_for_session_key(
    page,
    *,
    timeout_sec: float,
    get_email_code: Callable[[], str | None] | None,
    on_event: Callable[[str], None] | None,
) -> tuple[str | None, bool]:
    """Wait for grvt_ss_on_chain to appear. Returns (value, verification_seen)."""
    deadline = time.time() + timeout_sec
    sk = None
    verification_seen = False
    prompted = False
    while time.time() < deadline:
        _dismiss_popups(page)
        try:
            sk = page.evaluate("() => window.localStorage.getItem('grvt_ss_on_chain')")  # type: ignore[arg-type]
        except Exception:
            sk = None
        if sk:
            return str(sk), verification_seen

        if not verification_seen:
            # Heuristic: some verification UIs don't expose obvious OTP inputs immediately.
            try:
                body = (page.content() or "").lower()
                if "verification" in body and "code" in body:
                    verification_seen = True
            except Exception:
                pass

        if not prompted:
            did = _try_submit_email_code(page, get_email_code=get_email_code, on_event=on_event)
            if did:
                verification_seen = True
                prompted = True
        time.sleep(0.5)

    return None, verification_seen


def _manual_verification_headed(
    *,
    state_path: Path,
    origin: str,
    timeout_sec: float,
) -> str | None:
    """Open a headed browser with the current state to allow manual verification."""
    def _run() -> str | None:
        from playwright.sync_api import sync_playwright
        from playwright_stealth import Stealth

        ensure_playwright_browsers_path()
        with sync_playwright() as p:
            stealth = Stealth()
            stealth.hook_playwright_context(p)
            browser = _launch_chromium(
                p,
                headless=False,
                args=["--disable-blink-features=AutomationControlled"],
                channel="chrome",
            )
            context = browser.new_context(storage_state=str(state_path), viewport={"width": 1280, "height": 720}, locale="en-US")
            page = context.new_page()
            # Going to the exchange page tends to surface verification UI if needed.
            try:
                page.goto(f"{origin}/exchange/perpetual/BTC-USDT", wait_until="domcontentloaded", timeout=60000)
            except Exception:
                page.goto(origin, wait_until="domcontentloaded", timeout=60000)

            sk, _ = _wait_for_session_key(page, timeout_sec=timeout_sec, get_email_code=None, on_event=None)
            context.storage_state(path=str(state_path))
            browser.close()
            return sk

    return run_sync_playwright(_run)


def _wait_for_local_storage_key(page, key: str, *, timeout_sec: float) -> str | None:
    """Poll localStorage for a key to appear, while dismissing popups."""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        _dismiss_popups(page)
        try:
            val = page.evaluate("(k) => window.localStorage.getItem(k)", key)
            if val:
                return str(val)
        except Exception:
            pass
        time.sleep(0.5)
    return None


def _notify(on_event: Callable[[str], None] | None, msg: str) -> None:
    try:
        if on_event:
            on_event(msg)
    except Exception:
        pass


def _try_submit_email_code(
    page,
    *,
    get_email_code: Callable[[], str | None] | None,
    on_event: Callable[[str], None] | None,
) -> bool:
    """Best-effort handler for email verification/OTP UI.

    Returns True if a code was entered/submitted (not a guarantee of success).
    """
    # Heuristic: OTP forms usually have numeric inputs, or an "one-time-code" field.
    otp_inputs = page.locator(
        "input[autocomplete='one-time-code'], input[inputmode='numeric'], input[type='tel'], input[name*='code' i]"
    )
    try:
        count = otp_inputs.count()
    except Exception:
        count = 0
    if count <= 0:
        return False

    # Only proceed if something is visible.
    try:
        if not otp_inputs.first.is_visible():
            return False
    except Exception:
        pass

    # Some flows require clicking "Send code"/"Resend" before inputs become usable.
    for label in ("Send code", "Resend", "Get code", "Send", "Request code"):
        try:
            btn = page.get_by_role("button", name=re.compile(label, re.I)).first
            if btn.count() > 0 and btn.is_enabled():
                btn.click(timeout=1000)
                break
        except Exception:
            pass

    _notify(on_event, "Email verification detected. Waiting for code...")
    if not get_email_code:
        _notify(on_event, "Email verification required, but no code provider configured.")
        return True

    code = (get_email_code() or "").strip()
    if not code:
        _notify(on_event, "No code entered; cannot complete verification.")
        return True

    # Fill code into either a single input or multiple digit inputs.
    try:
        if count == 1:
            otp_inputs.first.click(timeout=1000)
            otp_inputs.first.fill(code, timeout=3000)
        else:
            # Many OTP UIs use 6 separate inputs. Fill sequentially.
            digits = [c for c in code if c.isdigit()]
            if len(digits) < count:
                # Still try: fill what we have.
                _notify(on_event, f"Code length ({len(digits)}) shorter than expected ({count}). Trying anyway...")
            for i in range(min(count, len(digits))):
                otp_inputs.nth(i).click(timeout=800)
                otp_inputs.nth(i).fill(digits[i], timeout=1200)
    except Exception:
        # Some UIs block fill(); fallback to type.
        try:
            otp_inputs.first.click(timeout=800)
            page.keyboard.type(code, delay=40)
        except Exception:
            return False

    # Try common submit buttons.
    for label in ("Verify", "Continue", "Confirm", "Submit", "Next"):
        try:
            btn = page.get_by_role("button", name=re.compile(label, re.I)).first
            if btn.count() > 0 and btn.is_enabled():
                btn.click(timeout=1500)
                _notify(on_event, "Submitted email verification code.")
                return True
        except Exception:
            pass

    # Fallback: press Enter.
    try:
        page.keyboard.press("Enter")
        _notify(on_event, "Submitted email verification code (Enter).")
        return True
    except Exception:
        return True


def _populate_account_ids(page) -> None:
    """Populate localStorage with IDs needed for authenticated trading.

    GRVT's REST/WS APIs require:
    - `X-Grvt-Account-Id`: account_id (base64-like, no `ACC:` prefix)
    - `sub_account_id`: numeric `chainSubAccountID` (uint64)

    Cloudflare blocks direct Python TLS calls to `edge.grvt.io`, so we query it from inside the
    browser context and persist into localStorage for later reuse.
    """
    # NOTE: Must contain real newlines (not literal "\\n" sequences).
    query = """query UserSubAccountsQuery {
  userSubAccounts {
    data {
      subAccounts {
        subAccount {
          id
          chainSubAccountID
          accountID
        }
      }
    }
  }
}
"""
    try:
        page.evaluate(
            """async ({query, edgeUrl}) => {
                const subRaw = window.localStorage.getItem('grvt:sub_account_id');
                let selected = null;
                if (subRaw) {
                  try { selected = JSON.parse(subRaw); } catch(e) { selected = subRaw; }
                }
                const cidRaw = window.localStorage.getItem('grvt:client_id');
                let cid = null;
                if (cidRaw) {
                  try { cid = JSON.parse(cidRaw); } catch(e) { cid = cidRaw; }
                }
                const headers = { 'content-type': 'application/json', 'x-api-source': 'WEB' };
                if (cid) headers['x-client-session-id'] = String(cid);
                try { headers['x-trace-id'] = crypto.randomUUID(); } catch(e) {}
                headers['x-device-fingerprint'] = `UserAgent=${navigator.userAgent}`;

                const resp = await fetch(edgeUrl + '/query', {
                  method: 'POST',
                  headers,
                  credentials: 'include',
                  body: JSON.stringify({ query })
                });
                const text = await resp.text();
                let data = null;
                try { data = JSON.parse(text); } catch(e) {}
                const subs = data?.data?.userSubAccounts?.data?.subAccounts || [];
                let match = null;
                if (selected) match = subs.find(x => x?.subAccount?.id === selected) || null;
                if (!match && subs.length) match = subs[0];
                if (!match) return;
                const chainSub = match.subAccount.chainSubAccountID;
                const accountID = (match.subAccount.accountID || '').replace('ACC:', '');
                if (chainSub) window.localStorage.setItem('grvt:chain_sub_account_id', String(chainSub));
                if (accountID) window.localStorage.setItem('grvt:account_id', accountID);
            }""",
            {"query": query, "edgeUrl": EDGE_URL},
        )
    except Exception:
        # Best-effort. If this fails, we fall back to deriving IDs later (or prompt re-login).
        return


def parse_qr_url(url: str) -> dict | None:
    """Parse and validate QR login URL. Returns payload dict or None."""
    parsed = urlparse(url)
    if parsed.path != "/qr-login":
        return None
    params = parse_qs(parsed.query)
    data_b64 = params.get("data", [None])[0]
    if not data_b64:
        return None
    try:
        payload = json.loads(base64.b64decode(data_b64))
        if "token" in payload:
            return payload
    except (json.JSONDecodeError, ValueError):
        pass
    return None


def qr_login_from_url(
    url: str,
    state_path: Path,
    *,
    origin: str = ORIGIN,
    timeout_sec: float = 60.0,
    require_session_key: bool = True,
    session_key_timeout_sec: float = 180.0,
    headless: bool = False,
    channel: str | None = None,
    get_email_code: Callable[[], str | None] | None = None,
    on_event: Callable[[str], None] | None = None,
    _allow_headed_fallback: bool = True,
) -> tuple[str | None, str]:
    """Login via QR URL. Returns (gravity_cookie, status_message).

    Note: Orders require `localStorage['grvt_ss_on_chain']` which is only set after completing
    the email verification step. When `require_session_key` is True, this will wait until that
    key exists before saving storage state.
    """
    def _run() -> tuple[str | None, str]:
        from playwright.sync_api import sync_playwright
        from playwright_stealth import Stealth

        payload = parse_qr_url(url)
        if not payload:
            return None, "Invalid QR code URL format"

        SESSION_DIR.mkdir(exist_ok=True)

        try:
            ensure_playwright_browsers_path()
            with sync_playwright() as p:
                stealth = Stealth()
                stealth.hook_playwright_context(p)
                args = ["--disable-blink-features=AutomationControlled"]
                if headless:
                    args = ["--headless=new", *args]
                # Prefer a real Chrome channel to reduce automation friction (also in headless).
                launch_channel = channel if channel is not None else "chrome"
                browser = _launch_chromium(p, headless=headless, args=args, channel=launch_channel)
                context = browser.new_context(
                    viewport={"width": 412, "height": 915},
                    device_scale_factor=2.625,
                    is_mobile=True,
                    has_touch=True,
                    user_agent="Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Mobile Safari/537.36",
                    locale="en-US",
                )
                page = context.new_page()

                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                _dismiss_popups(page)

                # Detect Cloudflare blocks early (best-effort) and auto-fallback to headed if requested.
                if _is_cloudflare_blocked(page):
                    browser.close()
                    if headless and _allow_headed_fallback:
                        _notify(on_event, "Cloudflare block detected in headless; retrying headed...")
                        return qr_login_from_url(
                            url,
                            state_path,
                            origin=origin,
                            timeout_sec=timeout_sec,
                            require_session_key=require_session_key,
                            session_key_timeout_sec=session_key_timeout_sec,
                            headless=False,
                            channel=channel,
                            get_email_code=get_email_code,
                            on_event=on_event,
                            _allow_headed_fallback=False,
                        )
                    return None, "Cloudflare blocked this browser session. Capture a fresh QR and try again."

                # Wait for gravity cookie
                start = time.time()
                gravity = None
                while time.time() - start < timeout_sec:
                    cookies = context.cookies()
                    gravity = next((c["value"] for c in cookies if c.get("name") == "gravity"), None)
                    if gravity:
                        break
                    time.sleep(1)

                if not gravity:
                    browser.close()
                    return None, "QR code expired or invalid. Generate a new one."

                if require_session_key:
                    sk, verification_seen = _wait_for_session_key(
                        page, timeout_sec=session_key_timeout_sec, get_email_code=get_email_code, on_event=on_event
                    )
                    if not sk:
                        # If verification was detected but we couldn't complete it headless, fall back to headed
                        # using the *current* session state (no need for a fresh QR).
                        if headless and verification_seen and _allow_headed_fallback:
                            _notify(on_event, "Email verification needs manual completion; opening headed browser...")
                            context.storage_state(path=str(state_path))
                            browser.close()
                            remaining = max(30.0, float(session_key_timeout_sec))
                            sk2 = _manual_verification_headed(state_path=state_path, origin=origin, timeout_sec=remaining)
                            if not sk2:
                                return None, "Login incomplete: email verification not completed in time."
                            sk = sk2
                        else:
                            browser.close()
                            return None, "Login incomplete: grvt_ss_on_chain not found (finish email verification and try again)."

                # Populate account/subaccount IDs for API usage.
                _populate_account_ids(page)

                context.storage_state(path=str(state_path))
                browser.close()

            save_cookie_cache(gravity)
            return gravity, f"Login successful. Cookie: {gravity[:12]}..."

        except Exception as e:
            return None, f"Login failed: {e}"

    return run_sync_playwright(_run)


def qr_login(
    image_path: str | Path,
    state_path: Path,
    *,
    origin: str = ORIGIN,
    headless: bool = False,
    timeout_sec: float = 60.0,
    require_session_key: bool = True,
    session_key_timeout_sec: float = 180.0,
    channel: str | None = None,
) -> str | None:
    """Login via QR code image. Returns gravity cookie or None."""
    import time

    ensure_playwright_browsers_path()
    # Decode QR
    url = decode_qr_image(image_path)
    if not url:
        print("Error: Could not decode QR code from image")
        return None

    # Validate URL
    payload = parse_qr_url(url)
    if not payload:
        print("Error: Invalid QR code URL format")
        return None

    SESSION_DIR.mkdir(exist_ok=True)

    print("QR code decoded. Navigating to login URL...")
    if not headless:
        print("(Browser will be visible. Wait for login to complete...)")

    def _run() -> tuple[str | None, bool]:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            # Prefer a real Chrome channel if available, but fall back to bundled Chromium.
            launch_channel = channel if channel is not None else "chrome"
            browser = _launch_chromium(p, headless=headless, channel=launch_channel)
            context = browser.new_context(viewport={"width": 1280, "height": 800})
            page = context.new_page()

            page.goto(url, timeout=60000)
            _dismiss_popups(page)

            # Wait for redirect/navigation after QR processing
            try:
                page.wait_for_url("**/trade**", timeout=timeout_sec * 1000)
            except Exception:
                pass  # May not redirect to trade page

            # Wait for gravity cookie
            start = time.time()
            gravity = None
            while time.time() - start < timeout_sec:
                try:
                    cookies = context.cookies()
                    gravity = next((c["value"] for c in cookies if c.get("name") == "gravity"), None)
                    if gravity:
                        break
                    time.sleep(0.5)
                except Exception:
                    break

            if not gravity:
                browser.close()
                return None, False

            if require_session_key:
                deadline = time.time() + session_key_timeout_sec
                sk = None
                prompted = False
                while time.time() < deadline:
                    _dismiss_popups(page)
                    try:
                        sk = page.evaluate("() => window.localStorage.getItem('grvt_ss_on_chain')")  # type: ignore[arg-type]
                    except Exception:
                        sk = None
                    if sk:
                        break
                    if not prompted:
                        did = _try_submit_email_code(
                            page,
                            get_email_code=lambda: input("Enter GRVT email verification code: ").strip(),
                            on_event=lambda m: print(m),
                        )
                        if did:
                            prompted = True
                    time.sleep(0.5)

                if not sk:
                    browser.close()
                    return None, False

            # Populate account/subaccount IDs for API usage.
            _populate_account_ids(page)

            # Save state
            context.storage_state(path=str(state_path))

            # Validate session key in localStorage
            try:
                local_storage = page.evaluate("() => Object.keys(localStorage)")
                has_session_key = any("privateKey" in k.lower() or "session" in k.lower() for k in local_storage)
            except Exception:
                has_session_key = False

            browser.close()
            return gravity, has_session_key

    gravity, has_session_key = run_sync_playwright(_run)

    if not has_session_key:
        print("Warning: Session key not found in localStorage - auth may be incomplete")

    save_cookie_cache(gravity)
    print(f"Login successful. State saved to: {state_path}")
    print(f"Gravity cookie: {gravity[:12]}...")
    return gravity
