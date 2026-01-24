# GRVT Volume Boost Tool

## Overview

Self-trade volume boost tool for GRVT perpetuals exchange. Uses two accounts to create opposing hedged positions, generating trading volume while remaining market-neutral.

## Architecture

```
volume_boost_gui.py              # GUI entry point
grvt_volume_boost/
├── gui_multi_market.py          # Main GUI components (~2800 lines)
├── strategy.py                  # Trading strategy + external fill detection
├── sizing.py                    # Position sizing (USD notional → contracts)
├── direction.py                 # Trade direction helpers
├── price_monitor.py             # WebSocket price buffer for stability checks
├── ws.py                        # WebSocket order sync utilities
├── config.py                    # Account config loading
├── settings.py                  # Environment settings (URLs, paths)
├── auth/
│   ├── cookies.py               # Cookie refresh (Playwright headless)
│   ├── qr_login.py              # QR code login flow
│   └── session_state.py         # Session state management
├── clients/
│   ├── market_data.py           # Public market data API (with HTTP pooling)
│   └── trades.py                # Authenticated trades API (with HTTP pooling)
└── services/
    ├── orders.py                # Order placement + account queries
    └── signing.py               # EIP-712 signing
```

## Trading Modes

| Mode | Description |
|------|-------------|
| **Open & Instant Close** | Open hedge, close immediately with delay |
| **Build, Hold & Close** | Build position, hold N minutes, then close |
| **Build & Hold** | Build position and hold indefinitely |
| **Close Existing** | Close existing positions only |

## Order Execution Flow (Maker-Taker Pattern)

1. **Price stability check** - WS buffer or 2-sample observation
2. **Maker order** (Account A) - Post-only limit at bid/ask
3. **WS sync wait** - Confirm maker on orderbook
4. **Taker IOC** (Account B) - Match against maker
5. **Cancel residual** - Clean up unfilled maker
6. **Fill verification** - Trade-matching external fill detection

## External Fill Detection (Trade-Matching)

**New approach (Jan 2026):** Compare taker IOC filled size vs expected:
```python
taker_filled = extract_filled_size(taker_result)  # From r.s1.bs[0]
if abs(taker_filled - size) > min_size:
    # External fill detected → close imbalance
```

## Performance Optimizations

| Optimization | Description |
|--------------|-------------|
| HTTP connection pooling | `requests.Session` with keep-alive |
| WS ticker cache | Use `TickerMonitor.buffer` instead of REST in trading loop |
| Lazy cookie validation | Skip `ping_auth` on fresh cache hits |
| Parallel cookie refresh | Fetch both cookies concurrently |
| Concurrent ticker fetches | Monitor window uses `ThreadPoolExecutor` |
| Log trimming | Keep last 3000 lines in status panel |
| Market catalog diff | Only update dropdowns if list changed |

## Key Components

### CookieManager
- TTL-based caching (default 3 min)
- Lazy validation - only validate on stale/miss
- Parallel fetch for account pairs

### PositionMonitorWindow
- 4 second auto-refresh
- Concurrent ticker fetches
- Hedge status indicator (Hedge/Unbalanced/Same Side)

### MarketRunPanel
- Market search with autocomplete
- Size type toggle (contracts / USD notional)
- External fill warning dialog with continue/stop choice

## Session Files

```
session/ (prod)
├── grvt_browser_state_1.json
└── grvt_browser_state_2.json

session_testnet/ (testnet)
├── grvt_browser_state_1.json
└── grvt_browser_state_2.json
```

## Known Limitations

- Cookie expires ~25 min → automatic background refresh
- Session key expires ~37 days → needs QR re-login
- Less liquid markets may trigger "Price unstable" more often
- Very small orders may hit precision edge cases
