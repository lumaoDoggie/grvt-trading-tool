from __future__ import annotations

import os
import sys
from pathlib import Path


def _is_frozen() -> bool:
    # PyInstaller sets sys.frozen and sys._MEIPASS for onefile. For onedir builds,
    # sys.frozen is still True.
    return bool(getattr(sys, "frozen", False))


def _exe_dir() -> Path:
    if _is_frozen():
        return Path(sys.executable).resolve().parent
    # Dev mode: repo root is one level above package.
    return Path(__file__).resolve().parent.parent


def ensure_playwright_browsers_path() -> None:
    """Point Playwright to a bundled browsers folder when running as an EXE.

    For a true "one-click" Windows experience we ship a Chromium build next to the EXE
    under `playwright-browsers/`. When this folder exists, set
    PLAYWRIGHT_BROWSERS_PATH so Playwright can find it without downloading.
    """
    # Respect an explicit user override.
    if os.getenv("PLAYWRIGHT_BROWSERS_PATH"):
        return

    base = _exe_dir()
    bundled = base / "playwright-browsers"
    if bundled.exists():
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(bundled)


def ensure_tls_trust() -> None:
    """Best-effort TLS trust configuration for packaged Windows builds.

    Symptoms: SSLCertVerificationError "unable to get local issuer certificate".
    Causes:
    - Python/OpenSSL can't find a CA bundle (common in embedded/packaged envs), or
    - a corporate proxy performs TLS interception and the org root cert is only in OS trust store.

    Strategy:
    - Prefer `truststore` (uses OS trust store on Windows/macOS).
    - Fallback to `certifi` and set SSL_CERT_FILE / REQUESTS_CA_BUNDLE.
    """
    # Respect explicit overrides.
    if os.getenv("SSL_CERT_FILE") or os.getenv("REQUESTS_CA_BUNDLE"):
        return

    # 1) Use OS trust store if available.
    try:
        import truststore  # type: ignore

        truststore.inject_into_ssl()
        return
    except Exception:
        pass

    # 2) Fallback to certifi bundle.
    try:
        import certifi  # type: ignore

        ca = certifi.where()
        os.environ.setdefault("SSL_CERT_FILE", ca)
        os.environ.setdefault("REQUESTS_CA_BUNDLE", ca)
    except Exception:
        pass
