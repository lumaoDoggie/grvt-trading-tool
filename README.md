# GRVT Volume Boost Tool

Self-trade volume boost tool for GRVT perpetuals. It uses two accounts to place opposing trades (hedged) to generate volume while aiming to stay market-neutral.

This repo contains both a GUI and CLI. Authentication is via QR-login sessions (no API keys in `.env`).

## Disclaimer

Trading is risky. Self-trading / wash trading may violate exchange rules and/or local regulations. Use at your own risk.

## Quickstart

### 1) Install

Python 3.10+ recommended.

```bash
pip install -r requirements.txt
python -m playwright install
```

### 2) Run the GUI

```bash
python volume_boost_gui.py
```

## Windows EXE (one-click)

For Windows users, download the latest `GRVTVolumeBoost-windows-x64.zip` from GitHub Releases, unzip it, then run:

- `GRVTVolumeBoost.exe`

The release build bundles Playwright Chromium, so QR login / cookie refresh works without extra setup.

## 中文说明

See `README_zh.md`.

### 3) Configure accounts (QR)

- Click `Setup Account`
- For Account 1 and 2, use `Capture QR` (or `Select Image...`) and then `Login`
- If GRVT asks for email verification, the app will prompt you to enter the code

Sessions are saved locally under:
- PROD: `session/`
- TESTNET: `session_testnet/`

These are ignored by git.

## Security Notes

- Do not share `session/`, `session_testnet/`, or `grvt_cookie_cache*.json` (they contain authentication material).
- If you publish logs/screenshots, redact any cookies/session identifiers first.

## Environments (PROD / TESTNET)

The GUI has an `Env` switch (top bar). Switching env restarts the app and uses separate session directories.

Defaults:
- PROD: `trades.grvt.io`, `market-data.grvt.io`, `edge.grvt.io`
- TESTNET: `trades.testnet.grvt.io`, `market-data.testnet.grvt.io`, `edge.testnet.grvt.io`

## Configuration

Copy `.env.example` to `.env` (optional). Most users can run with defaults.

## Notes

- Cookies are refreshed automatically from the stored browser state.
- Orders are signed with an EIP-712 session key stored in `localStorage['grvt_ss_on_chain']` after successful login.
