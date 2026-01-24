from __future__ import annotations

import random
from dataclasses import dataclass

from grvt_volume_boost.config import AccountConfig

SIDE_POLICY_RANDOM = "random"
SIDE_POLICY_ACCOUNT1_LONG = "account1_long"
SIDE_POLICY_ACCOUNT1_SHORT = "account1_short"


@dataclass(frozen=True)
class AccountPair:
    primary: AccountConfig
    secondary: AccountConfig


def _as_int(value: str) -> int | None:
    try:
        return int(str(value))
    except Exception:
        return None


def stable_account_pair(acc1: AccountConfig, acc2: AccountConfig) -> AccountPair:
    """Return (primary, secondary) where primary has numerically smaller sub_account_id."""
    id1 = _as_int(acc1.sub_account_id)
    id2 = _as_int(acc2.sub_account_id)
    if id1 is not None and id2 is not None:
        return AccountPair(primary=acc1, secondary=acc2) if id1 <= id2 else AccountPair(primary=acc2, secondary=acc1)
    # Fallback: stable lexical compare.
    return (
        AccountPair(primary=acc1, secondary=acc2)
        if str(acc1.sub_account_id) <= str(acc2.sub_account_id)
        else AccountPair(primary=acc2, secondary=acc1)
    )


def choose_long_short_for_open(
    pair: AccountPair,
    policy: str,
    *,
    rng: random.Random | None = None,
) -> tuple[AccountConfig, AccountConfig]:
    """Return (acc_long, acc_short) for OPENING leg in the current strategy."""
    if policy == SIDE_POLICY_ACCOUNT1_LONG:
        return pair.primary, pair.secondary
    if policy == SIDE_POLICY_ACCOUNT1_SHORT:
        return pair.secondary, pair.primary
    if policy == SIDE_POLICY_RANDOM:
        rng = rng or random
        return (pair.primary, pair.secondary) if rng.choice([True, False]) else (pair.secondary, pair.primary)
    raise ValueError(f"Unknown side policy: {policy}")

