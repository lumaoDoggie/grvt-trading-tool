"""Volume Boost GUI entrypoint.

Implementation lives in `grvt_volume_boost/gui_multi_market.py`.
"""

from dotenv import load_dotenv

# Load .env first so it can override persisted GUI prefs.
load_dotenv(".env")

from grvt_volume_boost.runtime import ensure_tls_trust

# Configure TLS trust early to avoid SSL_CERTIFICATE_VERIFY_FAILED in packaged builds.
ensure_tls_trust()

from grvt_volume_boost.gui_prefs import apply_startup_prefs

apply_startup_prefs()

from grvt_volume_boost.gui_multi_market import VolumeBoostGUI


if __name__ == "__main__":
    VolumeBoostGUI().run()
