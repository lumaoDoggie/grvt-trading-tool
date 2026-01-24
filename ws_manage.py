"""Convenience entrypoint for `python ws_manage.py ...`.

Implementation lives in `grvt_volume_boost/cli_ws_manage.py`.
"""

from grvt_volume_boost.cli_ws_manage import main


if __name__ == "__main__":
    raise SystemExit(main())

