from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from grvt_volume_boost.auth.session_state import (
    ensure_account_ids,
    extract_account_from_browser_state,
    extract_account_id,
    extract_chain_sub_account_id,
)
from grvt_volume_boost.settings import CHAIN_ID, MARKET_DATA_URL, ORIGIN, SESSION_DIR, TRADES_URL


@dataclass(frozen=True)
class AccountConfig:
    name: str
    sub_account_id: str
    main_account_id: str
    session_private_key: str
    browser_state_path: Path


def _state_file_for_account(num: int) -> Path:
    return SESSION_DIR / f"grvt_browser_state_{num}.json"


def get_account(num: int) -> AccountConfig:
    """Load account info from the saved browser state (QR login).

    We do NOT use API keys/secrets; orders are signed by `grvt_ss_on_chain` session key
    and authenticated by the `gravity` cookie.
    """
    state_path = _state_file_for_account(num)
    name = f"Account{num}"

    # Session signing key lives in grvt_ss_on_chain. (Keyed by USR:* in newer sessions.)
    _, session_key = extract_account_from_browser_state(state_path, origin=ORIGIN)

    # Account IDs required for authenticated REST/WS:
    # - X-Grvt-Account-Id => account_id (base64-like, no ACC: prefix)
    # - sub_account_id    => chainSubAccountID (numeric uint64)
    main_id = extract_account_id(state_path, origin=ORIGIN)
    sub_id = extract_chain_sub_account_id(state_path, origin=ORIGIN)
    if not main_id or not sub_id:
        # Back-compat: older sessions didn't store these; derive once via browser and persist to state file.
        main_id, sub_id = ensure_account_ids(state_path, origin=ORIGIN)
    if not main_id:
        raise ValueError(f"Could not extract account_id from {state_path.name}")
    if not sub_id:
        raise ValueError(f"Could not extract chain_sub_account_id from {state_path.name}")

    return AccountConfig(
        name=name,
        sub_account_id=sub_id,
        main_account_id=main_id,
        session_private_key=session_key,
        browser_state_path=state_path,
    )


def get_all_accounts() -> tuple[list[AccountConfig], list[str]]:
    """Return all configured accounts and any errors encountered.

    Returns (accounts, errors) where errors contains human-readable messages.
    """
    SESSION_DIR.mkdir(exist_ok=True)
    accounts: list[AccountConfig] = []
    errors: list[str] = []

    for num in (1, 2):
        state_path = _state_file_for_account(num)

        # Check session file
        if not state_path.exists():
            errors.append(f"Account {num}: Session file not found: {state_path.name}")
            continue

        try:
            accounts.append(get_account(num))
        except ValueError as e:
            msg = str(e)
            if "grvt_ss_on_chain" in msg or "account info" in msg:
                errors.append(f"Account {num}: Session file missing grvt_ss_on_chain (re-login via QR)")
            elif "account_id" in msg or "chain_sub_account_id" in msg:
                errors.append(f"Account {num}: {msg} (re-login via QR if it persists)")
            else:
                errors.append(f"Account {num}: {msg}")
        except Exception as e:
            errors.append(f"Account {num}: {e}")

    return accounts, errors
