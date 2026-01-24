from __future__ import annotations

import json
from pathlib import Path


def _parse_jsonish_value(raw: str) -> str:
    """Parse values stored via JSON.stringify, falling back to the raw string."""
    s = str(raw)
    try:
        v = json.loads(s)
        return str(v)
    except Exception:
        return s


def extract_selected_sub_account_id(state_path: Path, *, origin: str) -> str | None:
    """Extract the selected sub-account ID (e.g. 'SUB:...') from localStorage."""
    if not state_path.exists():
        return None

    with open(state_path, "r", encoding="utf-8") as f:
        state = json.load(f)

    local_storage = _get_local_storage(state, origin)
    raw = local_storage.get("grvt:sub_account_id", "")
    if not raw:
        return None
    try:
        return _parse_jsonish_value(raw)
    except Exception:
        return None


def extract_chain_sub_account_id(state_path: Path, *, origin: str) -> str | None:
    """Extract the numeric chain sub-account ID (uint64) from localStorage.

    We store this in localStorage as `grvt:chain_sub_account_id` after QR login by querying
    the GRVT edge GraphQL endpoint from within a real browser context (Cloudflare blocks Python TLS).
    """
    if not state_path.exists():
        return None

    with open(state_path, "r", encoding="utf-8") as f:
        state = json.load(f)

    local_storage = _get_local_storage(state, origin)
    raw = local_storage.get("grvt:chain_sub_account_id", "")
    if not raw:
        return None
    val = _parse_jsonish_value(raw).strip()
    return val if val.isdigit() else None


def extract_account_id(state_path: Path, *, origin: str) -> str | None:
    """Extract the main account ID used for `X-Grvt-Account-Id` from localStorage."""
    if not state_path.exists():
        return None

    with open(state_path, "r", encoding="utf-8") as f:
        state = json.load(f)

    local_storage = _get_local_storage(state, origin)
    raw = local_storage.get("grvt:account_id", "")
    if not raw:
        return None
    val = _parse_jsonish_value(raw).strip()
    return val or None


def ensure_account_ids(state_path: Path, *, origin: str) -> tuple[str | None, str | None]:
    """Ensure `grvt:account_id` and `grvt:chain_sub_account_id` exist in storage-state.

    Returns (account_id, chain_sub_account_id). If both are already present, no browser is started.
    Otherwise, we launch a real browser with the provided storage-state, query the edge GraphQL
    endpoint, and persist the discovered IDs back into the state file via localStorage.
    """
    acc_id = extract_account_id(state_path, origin=origin)
    chain_sa = extract_chain_sub_account_id(state_path, origin=origin)
    if acc_id and chain_sa:
        return acc_id, chain_sa

    selected_sub = extract_selected_sub_account_id(state_path, origin=origin)
    if not selected_sub:
        return acc_id, chain_sa

    # Cloudflare blocks direct Python requests; do the GraphQL query via a real browser context.
    from playwright.sync_api import sync_playwright
    from playwright_stealth import Stealth
    from grvt_volume_boost.settings import EDGE_URL
    from grvt_volume_boost.runtime import ensure_playwright_browsers_path

    # NOTE: The query string must contain real newlines. If it contains literal "\n"
    # sequences (backslash + n), the GraphQL parser will reject it.
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
        ensure_playwright_browsers_path()
        with sync_playwright() as p:
            stealth = Stealth()
            stealth.hook_playwright_context(p)
            browser = p.chromium.launch(
                headless=True,
                args=["--headless=new", "--disable-blink-features=AutomationControlled"],
                channel="chrome",
            )
            context = browser.new_context(storage_state=str(state_path), viewport={"width": 1280, "height": 720}, locale="en-US")
            page = context.new_page()
            page.goto(origin, wait_until="domcontentloaded", timeout=60000)

            result = page.evaluate(
                """async ({query, selectedSub, edgeUrl}) => {
                    const subRaw = window.localStorage.getItem('grvt:sub_account_id');
                    let selected = selectedSub;
                    if (!selected && subRaw) {
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
                    if (!match) return { ok:false, status: resp.status, selected, text: text.slice(0,200) };
                    const chainSub = match.subAccount.chainSubAccountID;
                    const accountID = (match.subAccount.accountID || '').replace('ACC:', '');
                    if (chainSub) window.localStorage.setItem('grvt:chain_sub_account_id', String(chainSub));
                    if (accountID) window.localStorage.setItem('grvt:account_id', accountID);
                    return { ok:true, chainSubAccountID: String(chainSub), accountID };
                }""",
                 {"query": query, "selectedSub": selected_sub, "edgeUrl": EDGE_URL},
             )

            context.storage_state(path=str(state_path))
            browser.close()

        if isinstance(result, dict) and result.get("ok"):
            acc_id = str(result.get("accountID") or "") or acc_id
            chain_sa = str(result.get("chainSubAccountID") or "") or chain_sa
    except Exception:
        # Best-effort: if we can't derive IDs, callers will surface a clear re-login error.
        pass

    return acc_id, chain_sa


def _get_local_storage(state: dict, origin: str) -> dict:
    """Extract localStorage dict from state (supports both formats)."""
    if "grvt_ss_on_chain" in state or "grvt:sub_account_id" in state:
        return state  # Raw localStorage format

    # Playwright format
    for entry in state.get("origins", []):
        if entry.get("origin") == origin:
            return {item["name"]: item["value"] for item in entry.get("localStorage", []) if "name" in item}
    return {}


def extract_account_from_browser_state(state_path: Path, *, origin: str) -> tuple[str, str]:
    """Extract (user_id, session_private_key) from a browser state file.

    Supports both raw localStorage format (flat key-value) and Playwright format.
    """
    if not state_path.exists():
        raise FileNotFoundError(f"Browser state not found: {state_path}")

    with open(state_path, "r", encoding="utf-8") as f:
        state = json.load(f)

    # Determine format: raw localStorage (has grvt_ss_on_chain at top level) vs Playwright (has origins)
    if "grvt_ss_on_chain" in state:
        # Raw localStorage format
        local_storage = state
    else:
        # Playwright format
        local_storage = {}
        for entry in state.get("origins", []):
            if entry.get("origin") == origin:
                local_storage = {item["name"]: item["value"] for item in entry.get("localStorage", []) if "name" in item}
                break

    sk_raw = local_storage.get("grvt_ss_on_chain", "") or ""
    if not sk_raw:
        raise ValueError(f"Could not extract account info from {state_path}")

    # Stored as a JSON string, may contain unicode escapes
    sk_str = sk_raw.strip('"').encode().decode("unicode_escape")
    sk = json.loads(sk_str)

    for user_id, data in sk.items():
        session_private_key = data.get("privateKey")
        return str(user_id), session_private_key

    raise ValueError(f"Could not extract account info from {state_path}")


def set_local_storage_values(state_path: Path, *, origin: str, updates: dict[str, str]) -> None:
    """Persist localStorage updates into a Playwright storage-state file."""
    if not state_path.exists():
        raise FileNotFoundError(state_path)

    with open(state_path, "r", encoding="utf-8") as f:
        state = json.load(f)

    if "origins" not in state:
        raise ValueError("State file is not Playwright storage-state format")

    origins = state.get("origins", []) or []
    entry = None
    for o in origins:
        if o.get("origin") == origin:
            entry = o
            break
    if entry is None:
        entry = {"origin": origin, "localStorage": []}
        origins.append(entry)
        state["origins"] = origins

    ls = entry.get("localStorage", []) or []
    by_name = {i.get("name"): i for i in ls if isinstance(i, dict) and "name" in i}
    for k, v in updates.items():
        if k in by_name:
            by_name[k]["value"] = str(v)
        else:
            ls.append({"name": k, "value": str(v)})
    entry["localStorage"] = ls

    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def fetch_subaccounts(state_path: Path, *, origin: str) -> list[dict]:
    """Fetch subaccount list via the edge GraphQL endpoint inside a browser context.

    Returns a list of dicts with keys:
    - id (e.g. SUB:...)
    - name
    - chainSubAccountID (numeric string)
    - accountID (e.g. ACC:...)
    """
    if not state_path.exists():
        raise FileNotFoundError(state_path)

    from playwright.sync_api import sync_playwright
    from playwright_stealth import Stealth
    from grvt_volume_boost.settings import EDGE_URL
    from grvt_volume_boost.runtime import ensure_playwright_browsers_path

    query = """query UserSubAccountsQuery {
  userSubAccounts {
    data {
      subAccounts {
        subAccount {
          id
          name
          chainSubAccountID
          accountID
        }
      }
    }
  }
}
"""

    ensure_playwright_browsers_path()
    with sync_playwright() as p:
        stealth = Stealth()
        stealth.hook_playwright_context(p)
        browser = p.chromium.launch(
            headless=True,
            args=["--headless=new", "--disable-blink-features=AutomationControlled"],
            channel="chrome",
        )
        context = browser.new_context(storage_state=str(state_path), viewport={"width": 1280, "height": 720}, locale="en-US")
        page = context.new_page()
        page.goto(origin, wait_until="domcontentloaded", timeout=60000)

        data = page.evaluate(
            """async ({query, edgeUrl}) => {
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
                try { return JSON.parse(text); } catch(e) { return { errors:[{message:'non-json'}], _text: text.slice(0,200), _status: resp.status }; }
            }""",
             {"query": query, "edgeUrl": EDGE_URL},
         )

        context.storage_state(path=str(state_path))
        browser.close()

    subs = (((data or {}).get("data") or {}).get("userSubAccounts") or {}).get("data") or {}
    out = []
    for entry in subs.get("subAccounts", []) or []:
        sa = (entry or {}).get("subAccount") or {}
        if not sa:
            continue
        out.append(
            {
                "id": sa.get("id"),
                "name": sa.get("name"),
                "chainSubAccountID": str(sa.get("chainSubAccountID")) if sa.get("chainSubAccountID") is not None else None,
                "accountID": sa.get("accountID"),
            }
        )
    return out
