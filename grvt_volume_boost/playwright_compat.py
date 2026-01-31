from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, TypeVar

_T = TypeVar("_T")


def run_sync_playwright(fn: Callable[[], _T]) -> _T:
    """Run Playwright sync API code safely even if an asyncio loop is running.

    Playwright's sync API raises when called from a thread with a running asyncio loop.
    In that case, run the sync work in a fresh thread and wait for the result.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return fn()

    with ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(fn).result()

