"""Multi-market concurrent GUI (Tkinter) for GRVT Volume Boost."""

from __future__ import annotations

import os
import queue
import random
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, simpledialog, ttk
from typing import Callable

from dotenv import load_dotenv

from grvt_config import get_all_accounts
from grvt_public_api import get_all_instruments
from grvt_trader import get_position_size, place_market_order
from grvt_volume_boost.gui_prefs import save_env as _save_gui_env, save_lang as _save_gui_lang
from grvt_volume_boost.i18n import get_lang as _get_lang, tr as _, tr_log_line as _tr_log_line
from grvt_volume_boost.services.orders import (
    ensure_initial_leverage,
    get_all_initial_leverage,
    get_all_positions_map,
    ping_auth,
)
from grvt_volume_boost.auth.cookies import (
    DEFAULT_COOKIE_REFRESH_INTERVAL_SEC,
    get_fresh_cookie,
    validate_state_file,
    validate_state_file_ext,
)
from grvt_volume_boost.auth.qr_login import decode_qr_from_pil, qr_login_from_url
from grvt_volume_boost.auth.session_state import (
    extract_account_id,
    extract_chain_sub_account_id,
    fetch_subaccounts,
    set_local_storage_values,
)
from grvt_volume_boost.settings import ORIGIN, SESSION_DIR
from grvt_volume_boost.direction import (
    SIDE_POLICY_ACCOUNT1_LONG,
    SIDE_POLICY_ACCOUNT1_SHORT,
    SIDE_POLICY_RANDOM,
    AccountPair,
    choose_long_short_for_open,
)
from grvt_volume_boost.sizing import compute_size_from_usd_notional, normalize_size
from volume_boost import get_instrument, get_margin_ratio, get_ticker, place_order_pair_with_retry
from grvt_volume_boost.price_monitor import TickerMonitor
from grvt_volume_boost.ws_monitor import PositionWSManager
from grvt_volume_boost.ws import OrderStreamClient

load_dotenv(".env")

EXCLUDED_BASES: set[str] = set()  # No exclusions - include all markets

SIZE_TYPE_CONTRACTS = "Contracts"
SIZE_TYPE_USD_NOTIONAL = "USD Notional"

DIRECTION_LABEL_TO_POLICY = {
    "Random": SIDE_POLICY_RANDOM,
    "Account 1 long": SIDE_POLICY_ACCOUNT1_LONG,
    "Account 1 short": SIDE_POLICY_ACCOUNT1_SHORT,
}


def _enable_windows_dpi_awareness() -> None:
    """Avoid Win32 DPI virtualization issues (Tk vs screen capture coordinate mismatch).

    Tk commonly reports logical pixels on Windows (e.g., 1536x864 at 125% scaling),
    while screen capture APIs often use physical pixels (e.g., 1920x1080). Making the
    process DPI-aware aligns coordinate systems and makes region capture reliable.
    """

    if os.name != "nt":
        return
    try:
        import ctypes

        # Best-effort; safe even if already DPI-aware.
        # Prefer Per-Monitor v2 on modern Windows, then fall back.
        try:
            set_ctx = getattr(ctypes.windll.user32, "SetProcessDpiAwarenessContext", None)
            if set_ctx is not None:
                # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = (HANDLE)-4
                if bool(set_ctx(ctypes.c_void_p(-4))):
                    return
        except Exception:
            pass

        try:
            set_awareness = getattr(ctypes.windll.shcore, "SetProcessDpiAwareness", None)
            if set_awareness is not None:
                # PROCESS_PER_MONITOR_DPI_AWARE = 2
                _ = set_awareness(2)
                return
        except Exception:
            pass

        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


_enable_windows_dpi_awareness()

def _ui_scale(widget: tk.Misc) -> float:
    """Best-effort UI scale factor (1.0 == 100% / 96 DPI).

    This app uses fixed pixel widths in a few ttk.Treeview tables. On Windows, when
    Display Scale is >100%, text scales up but those fixed widths do not, which can
    clip headers/cell text. Use window DPI when available, otherwise fall back to
    Tk's pixels-per-inch.
    """

    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            # Per-window DPI (best for per-monitor DPI); may return 96 before the window is mapped.
            get_dpi_for_window = getattr(ctypes.windll.user32, "GetDpiForWindow", None)
            if get_dpi_for_window is not None:
                get_dpi_for_window.argtypes = [wintypes.HWND]
                get_dpi_for_window.restype = ctypes.c_uint
                dpi = int(get_dpi_for_window(widget.winfo_id()))
                if dpi > 0 and dpi != 96:
                    return max(0.75, min(dpi / 96.0, 4.0))

            # System DPI fallback (more reliable at startup).
            get_dpi_for_system = getattr(ctypes.windll.user32, "GetDpiForSystem", None)
            if get_dpi_for_system is not None:
                get_dpi_for_system.argtypes = []
                get_dpi_for_system.restype = ctypes.c_uint
                dpi = int(get_dpi_for_system())
                if dpi > 0:
                    return max(0.75, min(dpi / 96.0, 4.0))

            # Older fallback: query primary screen device DPI via GDI.
            LOGPIXELSX = 88
            get_dc = ctypes.windll.user32.GetDC
            release_dc = ctypes.windll.user32.ReleaseDC
            get_device_caps = ctypes.windll.gdi32.GetDeviceCaps
            get_dc.argtypes = [wintypes.HWND]
            get_dc.restype = wintypes.HDC
            release_dc.argtypes = [wintypes.HWND, wintypes.HDC]
            release_dc.restype = ctypes.c_int
            get_device_caps.argtypes = [wintypes.HDC, ctypes.c_int]
            get_device_caps.restype = ctypes.c_int

            hdc = get_dc(0)
            try:
                dpi = int(get_device_caps(hdc, LOGPIXELSX))
                if dpi > 0:
                    return max(0.75, min(dpi / 96.0, 4.0))
            finally:
                try:
                    release_dc(0, hdc)
                except Exception:
                    pass
        except Exception:
            pass

    try:
        dpi = float(widget.winfo_fpixels("1i"))
        if dpi > 0:
            return max(0.75, min(dpi / 96.0, 4.0))
    except Exception:
        pass

    return 1.0


def _px(widget: tk.Misc, base_px: int) -> int:
    """Scale a baseline pixel value by the current UI scale."""
    try:
        return int(round(base_px * _ui_scale(widget)))
    except Exception:
        return int(base_px)


def _set_scaled_geometry(
    widget: tk.Misc,
    base_w: int,
    base_h: int,
    *,
    max_w_frac: float = 0.95,
    max_h_frac: float = 0.9,
) -> None:
    """Set DPI-aware geometry, capped to screen size so windows don't open off-screen."""
    w = _px(widget, base_w)
    h = _px(widget, base_h)
    try:
        sw = int(widget.winfo_screenwidth())
        sh = int(widget.winfo_screenheight())
        if sw > 0 and max_w_frac > 0:
            w = min(w, int(sw * max_w_frac))
        if sh > 0 and max_h_frac > 0:
            h = min(h, int(sh * max_h_frac))
    except Exception:
        pass
    try:
        widget.geometry(f"{w}x{h}")
    except Exception:
        pass


def _ttk_font(widget: tk.Misc, style_name: str, fallback: str = "TkDefaultFont") -> tkfont.Font:
    """Resolve ttk style font to a tk Font for measuring."""
    try:
        style = ttk.Style(widget)
        f = style.lookup(style_name, "font")
        if not f:
            f = fallback
        return tkfont.Font(widget, font=f)
    except Exception:
        try:
            return tkfont.nametofont(fallback)
        except Exception:
            return tkfont.Font(widget)


def _fit_treeview_headings(tree: ttk.Treeview, headings: dict[str, str], *, base_widths: dict[str, int] | None = None) -> None:
    """Ensure columns are wide enough for localized header text (DPI/font-safe)."""
    hfont = _ttk_font(tree, "Treeview.Heading")
    pad = _px(tree, 22)
    for col, text in headings.items():
        want = int(hfont.measure(text)) + pad
        if base_widths and col in base_widths:
            want = max(want, _px(tree, int(base_widths[col])))
        try:
            # `stretch=False` prevents ttk from squeezing columns smaller than the measured header width
            # when the widget is narrower than the sum of requested widths (use xscroll instead).
            tree.column(col, width=want, minwidth=want, stretch=False)
        except Exception:
            pass


def _configure_treeview_rowheight(tree: ttk.Treeview) -> None:
    """Fix clipped rows at high Windows display scaling by adjusting rowheight."""
    try:
        style = ttk.Style(tree)
        font = _ttk_font(tree, "Treeview")
        linespace = int(font.metrics("linespace") or 0)
        want = max(linespace + _px(tree, 8), _px(tree, 20))

        cur = style.lookup("Treeview", "rowheight")
        try:
            cur_i = int(cur) if cur not in (None, "") else 0
        except Exception:
            cur_i = 0

        style.configure("Treeview", rowheight=max(cur_i, want))
    except Exception:
        pass


class SetupWindow(tk.Toplevel):
    """Account setup window focused on QR login (no manual keys)."""

    def __init__(self, parent: tk.Tk, *, app: "VolumeBoostGUI"):
        super().__init__(parent)
        self.app = app
        self.title(_("setup.title"))
        _set_scaled_geometry(self, 560, 320)
        # QR codes are one-time use; store the decoded URL so we don't depend on re-decoding the image later.
        self._qr_payloads: dict[int, dict] = {}  # account_num -> {"url": str, "image": PIL.Image | None}
        self._login_in_progress: set[int] = set()
        self._account_buttons: dict[int, list[tk.Widget]] = {}
        self._build_ui()

    def _build_ui(self) -> None:
        outer = ttk.Frame(self, padding=10)
        outer.pack(fill=tk.BOTH, expand=True)

        # Account 1 / Account 2 blocks
        self.acc1_session_status = tk.StringVar(value=_("setup.not_checked"))
        self.acc1_qr_status = tk.StringVar(value="")
        self._build_account_block(outer, 1, self.acc1_session_status, self.acc1_qr_status)

        self.acc2_session_status = tk.StringVar(value=_("setup.not_checked"))
        self.acc2_qr_status = tk.StringVar(value="")
        self._build_account_block(outer, 2, self.acc2_session_status, self.acc2_qr_status)

        ttk.Button(outer, text=_("setup.close"), command=self.destroy).pack(anchor="e", pady=(10, 0))
        self._check_sessions()

    def _maybe_select_subaccount_async(self, account_num: int) -> None:
        """If multiple subaccounts exist, prompt the user to choose one."""
        state_path = SESSION_DIR / f"grvt_browser_state_{account_num}.json"

        def worker():
            try:
                subs = fetch_subaccounts(state_path, origin=ORIGIN)
            except Exception:
                subs = []
            try:
                self.after(0, lambda: self._maybe_select_subaccount_ui(account_num, subs))
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True).start()

    def _maybe_select_subaccount_ui(self, account_num: int, subs: list[dict]) -> None:
        if len(subs) <= 1:
            return

        dlg = SubaccountSelectDialog(self, subs=subs, account_num=account_num)
        self.wait_window(dlg)
        selected = dlg.selected
        if selected is None:
            return

        sa = subs[selected]
        chain_id = (sa.get("chainSubAccountID") or "").strip()
        account_id = str(sa.get("accountID") or "").replace("ACC:", "").strip()
        sub_id = str(sa.get("id") or "").strip()
        if not chain_id.isdigit() or not account_id:
            messagebox.showerror("Error", _("setup.invalid_subaccount"))
            return

        # Persist the chosen IDs into storage-state so the rest of the app uses the right subaccount.
        import json as _json

        state_path = SESSION_DIR / f"grvt_browser_state_{account_num}.json"
        set_local_storage_values(
            state_path,
            origin=ORIGIN,
            updates={
                "grvt:chain_sub_account_id": chain_id,
                "grvt:account_id": account_id,
                # Keep UI selection in sync with backend selection; stored as a JSON string in GRVT.
                "grvt:sub_account_id": _json.dumps(sub_id) if sub_id else _json.dumps(""),
            },
        )
        messagebox.showinfo(_("setup.sub_selected_title"), _("setup.sub_selected_body", n=account_num, chain_id=chain_id))
        self._check_sessions()
        try:
            self.app.reload_accounts()
        except Exception:
            pass

    def _build_account_block(self, parent: tk.Widget, account_num: int, session_var: tk.StringVar, status_var: tk.StringVar) -> None:
        frame = ttk.LabelFrame(parent, text=_("setup.account", n=account_num), padding=10)
        frame.pack(fill=tk.X, padx=5, pady=6)

        ttk.Label(frame, text=_("setup.session")).grid(row=0, column=0, sticky=tk.W)
        ttk.Label(frame, textvariable=session_var).grid(row=0, column=1, sticky=tk.W, padx=6)

        btns = ttk.Frame(frame)
        btns.grid(row=1, column=0, columnspan=2, sticky=tk.W, pady=(8, 2))

        b_capture = ttk.Button(btns, text=_("setup.capture"), command=lambda: self._capture_qr(account_num))
        b_select = ttk.Button(btns, text=_("setup.select_image"), command=lambda: self._select_qr_image(account_num))
        b_login = ttk.Button(btns, text=_("setup.login"), command=lambda: self._validate_qr(account_num))
        b_remove = ttk.Button(btns, text=_("setup.remove_session"), command=lambda: self._remove_session(account_num))

        for b in (b_capture, b_select, b_login, b_remove):
            b.pack(side=tk.LEFT, padx=2)

        ttk.Label(frame, textvariable=status_var, wraplength=520).grid(row=2, column=0, columnspan=2, sticky=tk.W, pady=(6, 0))

        self._account_buttons[account_num] = [b_capture, b_select, b_login, b_remove]

    def _set_account_buttons_enabled(self, account_num: int, enabled: bool) -> None:
        for w in self._account_buttons.get(account_num, []):
            try:
                w.configure(state=tk.NORMAL if enabled else tk.DISABLED)
            except Exception:
                pass

    def _capture_qr(self, account_num: int) -> None:
        """Capture screen region for QR code."""
        try:
            import PIL  # noqa: F401
        except Exception:
            messagebox.showerror("Error", _("setup.pillow_missing"))
            return

        status_var = self.acc1_qr_status if account_num == 1 else self.acc2_qr_status
        status_var.set(_("setup.select_region"))

        # Hide window temporarily
        self.withdraw()
        self.update()
        time.sleep(0.3)

        # Create selection overlay
        selector = QRRegionSelector(self, account_num, self._on_qr_captured)
        selector.grab_set()

    def _on_qr_captured(self, account_num: int, image: "Image.Image | None") -> None:
        """Callback when QR region is captured."""
        self.deiconify()
        status_var = self.acc1_qr_status if account_num == 1 else self.acc2_qr_status

        if image is None:
            status_var.set(_("setup.capture_cancelled"))
            return

        # Save capture for debugging and to help users confirm selection.
        try:
            SESSION_DIR.mkdir(exist_ok=True)
            capture_path = SESSION_DIR / f"qr_capture_account_{account_num}.png"
            image.save(capture_path)
        except Exception:
            capture_path = None

        # Try to decode QR
        url = decode_qr_from_pil(image)
        if not url:
            extra = f" (saved: {capture_path.name})" if capture_path else ""
            status_var.set(_("setup.qr_decode_failed", extra=extra))
            return

        if "qr-login" not in url:
            status_var.set(_("setup.qr_not_grvt"))
            return

        self._qr_payloads[account_num] = {"url": url, "image": image}
        status_var.set(_("setup.qr_decoded"))

    def _select_qr_image(self, account_num: int) -> None:
        """Select a QR image from disk as a fallback for capture issues."""
        default_dir = str((Path(__file__).resolve().parent / "qr").resolve())
        path = filedialog.askopenfilename(
            title=f"Select QR image for Account {account_num}",
            initialdir=default_dir if os.path.isdir(default_dir) else None,
            filetypes=[("Images", "*.png;*.jpg;*.jpeg;*.bmp"), ("All files", "*.*")],
        )
        if not path:
            return

        try:
            from PIL import Image
            img = Image.open(path)
        except Exception as e:
            messagebox.showerror("Error", f"Failed to open image: {e}")
            return

        url = decode_qr_from_pil(img)
        status_var = self.acc1_qr_status if account_num == 1 else self.acc2_qr_status
        if not url:
            status_var.set(_("setup.qr_decode_failed", extra=""))
            return
        if "qr-login" not in url:
            status_var.set(_("setup.qr_not_grvt"))
            return

        self._qr_payloads[account_num] = {"url": url, "image": img}
        status_var.set(_("setup.qr_decoded"))

    def _validate_qr(self, account_num: int) -> None:
        """Validate captured QR and perform login."""
        status_var = self.acc1_qr_status if account_num == 1 else self.acc2_qr_status
        if account_num in self._login_in_progress:
            status_var.set("Login already in progress...")
            return

        payload = self._qr_payloads.get(account_num) or {}
        url = payload.get("url")
        if not url:
            # If the user captured but decode failed, try decoding the last saved capture once more.
            try:
                from PIL import Image
                capture_path = SESSION_DIR / f"qr_capture_account_{account_num}.png"
                if capture_path.exists():
                    img = Image.open(capture_path)
                    url2 = decode_qr_from_pil(img)
                    if url2 and "qr-login" in url2:
                        url = url2
                        self._qr_payloads[account_num] = {"url": url, "image": img}
            except Exception:
                pass
            if not url:
                status_var.set(_("setup.no_qr_yet"))
                return

        # One-time use: once we attempt login, force the user to capture a fresh QR for any retry.
        self._qr_payloads.pop(account_num, None)
        self._login_in_progress.add(account_num)
        self._set_account_buttons_enabled(account_num, False)

        status_var.set(_("setup.logging_in"))
        self.update()

        state_path = SESSION_DIR / f"grvt_browser_state_{account_num}.json"

        def do_login():
            # If email verification is required, prompt the user for the code (browser stays headless).
            def get_email_code() -> str | None:
                ev = threading.Event()
                box: dict[str, str | None] = {"v": None}

                def ask():
                    try:
                        box["v"] = simpledialog.askstring(
                            _("email_verify.title"),
                            _("email_verify.body"),
                            parent=self,
                        )
                    finally:
                        ev.set()

                try:
                    self.after(0, ask)
                except Exception:
                    ask()
                ev.wait()
                return box["v"]

            def on_event(msg: str) -> None:
                try:
                    self.after(0, lambda m=msg: status_var.set(m[:80]))
                except Exception:
                    pass

            # QR login requires completing email verification; keep the browser visible and
            # wait until `localStorage['grvt_ss_on_chain']` exists before saving state.
            cookie, msg = qr_login_from_url(
                url,
                state_path,
                headless=True,
                require_session_key=True,
                session_key_timeout_sec=600.0,
                get_email_code=get_email_code,
                on_event=on_event,
            )
            self.after(0, lambda: self._on_login_complete(account_num, cookie, msg))

        threading.Thread(target=do_login, daemon=True).start()

    def _on_login_complete(self, account_num: int, cookie: str | None, msg: str) -> None:
        """Callback when login completes."""
        status_var = self.acc1_qr_status if account_num == 1 else self.acc2_qr_status
        self._login_in_progress.discard(account_num)
        self._set_account_buttons_enabled(account_num, True)

        status_var.set(msg[:80])

        if cookie:
            # QR is one-time use; we already cleared it when the login started.
            messagebox.showinfo(_("setup.login_ok"), _("setup.login_ok_body", n=account_num))
            # If the user has multiple subaccounts, prompt for which to use.
            self._maybe_select_subaccount_async(account_num)
        else:
            messagebox.showerror(_("setup.login_failed"), msg)
            status_var.set(_("setup.login_failed_hint"))

        self._check_sessions()
        try:
            self.app.reload_accounts()
        except Exception:
            pass

    def _remove_session(self, account_num: int) -> None:
        """Remove session file with confirmation."""
        state_path = SESSION_DIR / f"grvt_browser_state_{account_num}.json"

        if not state_path.exists():
            messagebox.showinfo("Info", _("setup.no_session", n=account_num))
            return

        if not messagebox.askyesno(
            _("setup.remove_confirm_title"),
            _("setup.remove_confirm_body", n=account_num),
        ):
            return

        try:
            state_path.unlink()
            self._qr_payloads.pop(account_num, None)
            status_var = self.acc1_qr_status if account_num == 1 else self.acc2_qr_status
            status_var.set(_("setup.session_removed"))
            messagebox.showinfo(_("setup.removed_title"), _("setup.removed_body", n=account_num))
        except Exception as e:
            messagebox.showerror("Error", _("setup.remove_failed", err=str(e)))

        self._check_sessions()

    def _check_sessions(self) -> None:
        for num, status_var in [(1, self.acc1_session_status), (2, self.acc2_session_status)]:
            path = SESSION_DIR / f"grvt_browser_state_{num}.json"
            # Setup requires signer key to place orders.
            valid, err = validate_state_file_ext(path, require_session_key=True)
            if not valid:
                status_var.set(err)
                continue
            # IDs are needed for authenticated REST/WS. If missing, we'll derive them on demand,
            # but show a hint here to avoid confusion.
            acc_id = extract_account_id(path, origin=ORIGIN)
            chain_sa = extract_chain_sub_account_id(path, origin=ORIGIN)
            if not acc_id or not chain_sa:
                status_var.set(_("setup.session_ok_missing_ids"))
            else:
                status_var.set(_("setup.session_ok"))
        try:
            self.app.reload_accounts()
        except Exception:
            pass


class SubaccountSelectDialog(tk.Toplevel):
    """Modal dialog to choose one subaccount when multiple exist."""

    def __init__(self, parent: tk.Tk | tk.Toplevel, *, subs: list[dict], account_num: int):
        super().__init__(parent)
        self.title(_("setup.sub_select_title", n=account_num))
        _set_scaled_geometry(self, 520, 320)
        self.resizable(False, False)
        self.selected: int | None = None
        self._subs = subs

        outer = ttk.Frame(self, padding=10)
        outer.pack(fill=tk.BOTH, expand=True)

        ttk.Label(
            outer,
            text=_("setup.sub_select_body"),
            justify=tk.LEFT,
        ).pack(anchor="w")

        self._list = tk.Listbox(outer, height=10)
        self._list.pack(fill=tk.BOTH, expand=True, pady=10)

        for i, sa in enumerate(subs):
            name = sa.get("name") or ""
            sid = sa.get("chainSubAccountID") or ""
            sub_id = sa.get("id") or ""
            label = f"{i+1}. {name}  |  chainSubAccountID={sid}  |  {sub_id}"
            self._list.insert(tk.END, label)

        if subs:
            self._list.selection_set(0)

        btns = ttk.Frame(outer)
        btns.pack(fill=tk.X)

        ttk.Button(btns, text=_("setup.sub_cancel"), command=self._cancel).pack(side=tk.RIGHT, padx=4)
        ttk.Button(btns, text=_("setup.sub_use"), command=self._ok).pack(side=tk.RIGHT)

        self._list.bind("<Double-Button-1>", lambda _e: self._ok())
        self.bind("<Escape>", lambda _e: self._cancel())

        self.transient(parent)
        self.grab_set()

    def _ok(self) -> None:
        try:
            sel = self._list.curselection()
            if not sel:
                return
            self.selected = int(sel[0])
        except Exception:
            self.selected = None
        self.destroy()

    def _cancel(self) -> None:
        self.selected = None
        self.destroy()


class QRRegionSelector(tk.Toplevel):
    """Fullscreen overlay for selecting QR code region."""

    def __init__(self, parent, account_num: int, callback):
        super().__init__(parent)
        self._parent = parent
        self.account_num = account_num
        self.callback = callback
        self.start_x = self.start_y = 0
        self.rect_id = None

        # Fullscreen transparent overlay
        self.attributes("-fullscreen", True)
        self.attributes("-alpha", 0.3)
        self.configure(bg="gray")

        self.canvas = tk.Canvas(self, cursor="cross", bg="gray", highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)

        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Escape>", self._on_cancel)

        # Instructions
        self.canvas.create_text(
            self.winfo_screenwidth() // 2, 50,
            text=_("setup.region_instructions"),
            fill="white", font=("TkDefaultFont", 14, "bold")
        )

    def _on_press(self, event):
        self.start_x, self.start_y = event.x, event.y
        if self.rect_id:
            self.canvas.delete(self.rect_id)
        self.rect_id = self.canvas.create_rectangle(
            self.start_x, self.start_y, self.start_x, self.start_y,
            outline="red", width=2
        )

    def _on_drag(self, event):
        if self.rect_id:
            self.canvas.coords(self.rect_id, self.start_x, self.start_y, event.x, event.y)

    def _on_release(self, event):
        x1, y1 = min(self.start_x, event.x), min(self.start_y, event.y)
        x2, y2 = max(self.start_x, event.x), max(self.start_y, event.y)

        if x2 - x1 < 20 or y2 - y1 < 20:
            self._on_cancel(None)
            return

        # Hide first, then capture asynchronously. Some Windows/Pillow combinations intermittently
        # throw "bad window path" if we grab immediately while tearing down the fullscreen overlay.
        self.withdraw()
        try:
            self.update_idletasks()
        except Exception:
            pass

        def do_capture(attempt: int = 1) -> None:
            try:
                from PIL import Image, ImageGrab
                import ctypes
                from ctypes import wintypes

                # Give the compositor time to fully remove the overlay.
                time.sleep(0.25 + (attempt - 1) * 0.15)

                tk_w = max(1, int(self.winfo_screenwidth()))
                tk_h = max(1, int(self.winfo_screenheight()))

                user32 = ctypes.WinDLL("user32", use_last_error=True)
                gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

                # Fix ctypes prototypes (avoid 64-bit overflow / "bad window path"-like failures).
                user32.GetDC.argtypes = [wintypes.HWND]
                user32.GetDC.restype = wintypes.HDC
                user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
                user32.ReleaseDC.restype = ctypes.c_int

                gdi32.GetDeviceCaps.argtypes = [wintypes.HDC, ctypes.c_int]
                gdi32.GetDeviceCaps.restype = ctypes.c_int

                # Determine coordinate system sizes for each capture backend:
                # - GDI uses the DC logical resolution (HORZRES/VERTRES) unless DPI awareness is enabled.
                # - Pillow ImageGrab uses physical pixels.
                HORZRES = 8
                VERTRES = 10
                DESKTOPHORZRES = 118
                DESKTOPVERTRES = 117

                hdc_screen = user32.GetDC(None)
                if not hdc_screen:
                    raise OSError("GetDC failed")
                try:
                    gdi_w = int(gdi32.GetDeviceCaps(hdc_screen, HORZRES) or 0)
                    gdi_h = int(gdi32.GetDeviceCaps(hdc_screen, VERTRES) or 0)
                    phys_w = int(gdi32.GetDeviceCaps(hdc_screen, DESKTOPHORZRES) or 0)
                    phys_h = int(gdi32.GetDeviceCaps(hdc_screen, DESKTOPVERTRES) or 0)
                finally:
                    try:
                        user32.ReleaseDC(None, hdc_screen)
                    except Exception:
                        pass

                if gdi_w <= 0 or gdi_h <= 0:
                    gdi_w, gdi_h = tk_w, tk_h

                grab_w, grab_h = phys_w, phys_h
                if grab_w <= 0 or grab_h <= 0:
                    # Last resort: attempt a full screenshot (can fail on some systems).
                    try:
                        grab_w, grab_h = ImageGrab.grab().size
                    except Exception:
                        grab_w, grab_h = gdi_w, gdi_h

                def _expand_and_clamp_bbox(
                    left: int, top: int, right: int, bottom: int, limit_w: int, limit_h: int, pad_px: int
                ) -> tuple[int, int, int, int]:
                    # Expand by padding, then clamp by shifting the bbox back onto the screen if needed.
                    l = left - pad_px
                    t = top - pad_px
                    r = right + pad_px
                    b = bottom + pad_px

                    if l < 0:
                        r -= l
                        l = 0
                    if t < 0:
                        b -= t
                        t = 0
                    if r > limit_w:
                        overflow = r - limit_w
                        l -= overflow
                        r = limit_w
                        if l < 0:
                            l = 0
                    if b > limit_h:
                        overflow = b - limit_h
                        t -= overflow
                        b = limit_h
                        if t < 0:
                            t = 0

                    l = max(0, min(l, limit_w - 1))
                    t = max(0, min(t, limit_h - 1))
                    r = max(l + 1, min(r, limit_w))
                    b = max(t + 1, min(b, limit_h))
                    return int(l), int(t), int(r), int(b)

                # Convert Tk logical coords to backend coords, then add generous padding.
                scale_gdi_x = gdi_w / tk_w
                scale_gdi_y = gdi_h / tk_h
                scale_grab_x = grab_w / tk_w
                scale_grab_y = grab_h / tk_h

                sel_w_gdi = max(1, int((x2 - x1) * scale_gdi_x))
                sel_h_gdi = max(1, int((y2 - y1) * scale_gdi_y))
                pad_gdi = max(48, int(min(sel_w_gdi, sel_h_gdi) * 0.35))

                gdi_l = int(x1 * scale_gdi_x)
                gdi_t = int(y1 * scale_gdi_y)
                gdi_r = int(x2 * scale_gdi_x)
                gdi_b = int(y2 * scale_gdi_y)
                gx1, gy1, gx2, gy2 = _expand_and_clamp_bbox(gdi_l, gdi_t, gdi_r, gdi_b, gdi_w, gdi_h, pad_gdi)

                sel_w_grab = max(1, int((x2 - x1) * scale_grab_x))
                sel_h_grab = max(1, int((y2 - y1) * scale_grab_y))
                pad_grab = max(48, int(min(sel_w_grab, sel_h_grab) * 0.35))

                grab_l = int(x1 * scale_grab_x)
                grab_t = int(y1 * scale_grab_y)
                grab_r = int(x2 * scale_grab_x)
                grab_b = int(y2 * scale_grab_y)
                bx1, by1, bx2, by2 = _expand_and_clamp_bbox(grab_l, grab_t, grab_r, grab_b, grab_w, grab_h, pad_grab)

                def _gdi_grab(x: int, y: int, w: int, h: int) -> Image.Image:
                    """Win32 GDI capture of a screen region. Avoids Pillow's intermittent 'bad window path'."""
                    SRCCOPY = 0x00CC0020
                    DIB_RGB_COLORS = 0

                    class BITMAPINFOHEADER(ctypes.Structure):
                        _fields_ = [
                            ("biSize", wintypes.DWORD),
                            ("biWidth", wintypes.LONG),
                            ("biHeight", wintypes.LONG),
                            ("biPlanes", wintypes.WORD),
                            ("biBitCount", wintypes.WORD),
                            ("biCompression", wintypes.DWORD),
                            ("biSizeImage", wintypes.DWORD),
                            ("biXPelsPerMeter", wintypes.LONG),
                            ("biYPelsPerMeter", wintypes.LONG),
                            ("biClrUsed", wintypes.DWORD),
                            ("biClrImportant", wintypes.DWORD),
                        ]

                    class BITMAPINFO(ctypes.Structure):
                        _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]

                    user32 = ctypes.WinDLL("user32", use_last_error=True)
                    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

                    user32.GetDC.argtypes = [wintypes.HWND]
                    user32.GetDC.restype = wintypes.HDC
                    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
                    user32.ReleaseDC.restype = ctypes.c_int

                    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
                    gdi32.CreateCompatibleDC.restype = wintypes.HDC
                    gdi32.DeleteDC.argtypes = [wintypes.HDC]
                    gdi32.DeleteDC.restype = ctypes.c_int

                    gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
                    gdi32.CreateCompatibleBitmap.restype = wintypes.HANDLE

                    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HANDLE]
                    gdi32.SelectObject.restype = wintypes.HANDLE
                    gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
                    gdi32.DeleteObject.restype = ctypes.c_int

                    gdi32.BitBlt.argtypes = [
                        wintypes.HDC,
                        ctypes.c_int,
                        ctypes.c_int,
                        ctypes.c_int,
                        ctypes.c_int,
                        wintypes.HDC,
                        ctypes.c_int,
                        ctypes.c_int,
                        wintypes.DWORD,
                    ]
                    gdi32.BitBlt.restype = wintypes.BOOL

                    gdi32.GetDIBits.argtypes = [
                        wintypes.HDC,
                        wintypes.HANDLE,
                        wintypes.UINT,
                        wintypes.UINT,
                        ctypes.c_void_p,
                        ctypes.c_void_p,
                        wintypes.UINT,
                    ]
                    gdi32.GetDIBits.restype = ctypes.c_int

                    hdc = user32.GetDC(None)
                    if not hdc:
                        raise OSError("GetDC failed")
                    mdc = gdi32.CreateCompatibleDC(hdc)
                    if not mdc:
                        user32.ReleaseDC(None, hdc)
                        raise OSError("CreateCompatibleDC failed")
                    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
                    if not bmp:
                        gdi32.DeleteDC(mdc)
                        user32.ReleaseDC(None, hdc)
                        raise OSError("CreateCompatibleBitmap failed")

                    old = gdi32.SelectObject(mdc, bmp)
                    try:
                        if not gdi32.BitBlt(mdc, 0, 0, w, h, hdc, x, y, SRCCOPY):
                            raise OSError("BitBlt failed")

                        bmi = BITMAPINFO()
                        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
                        bmi.bmiHeader.biWidth = w
                        bmi.bmiHeader.biHeight = -h  # top-down
                        bmi.bmiHeader.biPlanes = 1
                        bmi.bmiHeader.biBitCount = 32
                        bmi.bmiHeader.biCompression = 0  # BI_RGB
                        buf = ctypes.create_string_buffer(w * h * 4)

                        lines = gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bmi), DIB_RGB_COLORS)
                        if lines != h:
                            raise OSError("GetDIBits failed")

                        return Image.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1)
                    finally:
                        try:
                            gdi32.SelectObject(mdc, old)
                        except Exception:
                            pass
                        gdi32.DeleteObject(bmp)
                        gdi32.DeleteDC(mdc)
                        try:
                            user32.ReleaseDC(None, hdc)
                        except Exception:
                            pass

                gw = max(1, gx2 - gx1)
                gh = max(1, gy2 - gy1)

                # Prefer GDI capture; fall back to Pillow if needed.
                image = None
                last_err: Exception | None = None
                method = None
                try:
                    image = _gdi_grab(gx1, gy1, gw, gh)
                    method = "gdi"
                except Exception as e:
                    last_err = e
                    try:
                        image = ImageGrab.grab(bbox=(bx1, by1, bx2, by2))
                        method = "imagegrab"
                    except Exception as e2:
                        last_err = e2
                        raise last_err

                # Save capture metadata for debugging.
                try:
                    SESSION_DIR.mkdir(exist_ok=True)
                    meta_path = SESSION_DIR / f"qr_capture_account_{self.account_num}.meta.json"
                    meta_path.write_text(
                        __import__("json").dumps(
                            {
                                "tk_screen": [tk_w, tk_h],
                                "gdi_screen": [gdi_w, gdi_h],
                                "grab_screen": [grab_w, grab_h],
                                "scale_gdi": [scale_gdi_x, scale_gdi_y],
                                "scale_grab": [scale_grab_x, scale_grab_y],
                                "tk_bbox": [x1, y1, x2, y2],
                                "gdi_bbox": [gx1, gy1, gx2, gy2],
                                "grab_bbox": [bx1, by1, bx2, by2],
                                "method": method,
                            },
                            indent=2,
                        ),
                        encoding="utf-8",
                    )
                except Exception:
                    pass

                self.destroy()
                self.callback(self.account_num, image)
            except Exception as e:
                # Persist debug info for capture issues (common on Windows).
                try:
                    import traceback
                    SESSION_DIR.mkdir(exist_ok=True)
                    log_path = SESSION_DIR / f"qr_capture_account_{self.account_num}.error.log"
                    with open(log_path, "a", encoding="utf-8") as f:
                        f.write(f"\n=== capture attempt {attempt} ===\n")
                        f.write(f"{type(e).__name__}: {e}\n")
                        f.write(traceback.format_exc())
                except Exception:
                    pass

                # Retry a few times for transient "bad window path" capture failures.
                if attempt < 6:
                    try:
                        self._parent.after(10, lambda: do_capture(attempt + 1))
                        return
                    except Exception:
                        pass
                try:
                    self.destroy()
                except Exception:
                    pass
                messagebox.showerror("Capture Error", str(e))
                self.callback(self.account_num, None)

        # Schedule capture after returning control to the Tk loop.
        try:
            self._parent.after(10, do_capture)
        except Exception:
            do_capture()

    def _on_cancel(self, _event):
        self.destroy()
        self.callback(self.account_num, None)


class RecommendMarketsWindow(tk.Toplevel):
    """Popup window showing markets ranked by spread (in ticks).
    
    Markets are filtered to show only those with at least 4 ticks
    spread between bid and ask, ranked from largest spread to smallest.
    """

    MIN_SPREAD_TICKS = 4

    def __init__(self, parent: tk.Tk, markets: list[str], market_meta: dict[str, dict[str, str]]):
        super().__init__(parent)
        self.title(_("reco.title"))
        _set_scaled_geometry(self, 500, 400)
        self.transient(parent)

        self._markets = markets
        self._meta = market_meta
        self._results: list[tuple[str, int, Decimal, Decimal]] = []  # (instrument, spread_ticks, bid, ask)
        self._stop_event = threading.Event()

        self._build_ui()
        self._start_analysis()

    def _build_ui(self) -> None:
        ttk.Label(self, text=_("reco.body", min_ticks=self.MIN_SPREAD_TICKS)).pack(anchor=tk.W, padx=10, pady=5)

        self._progress_var = tk.StringVar(value=_("reco.analyzing"))
        self._progress_label = ttk.Label(self, textvariable=self._progress_var)
        self._progress_label.pack(anchor=tk.W, padx=10)

        list_frame = ttk.Frame(self)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        columns = ("market", "spread_ticks", "bid", "ask")
        self._tree = ttk.Treeview(list_frame, columns=columns, show="headings", height=15)
        _configure_treeview_rowheight(self._tree)
        headings = {
            "market": _("reco.col.market"),
            "spread_ticks": _("reco.col.spread"),
            "bid": _("reco.col.bid"),
            "ask": _("reco.col.ask"),
        }
        self._tree.heading("market", text=headings["market"])
        self._tree.heading("spread_ticks", text=headings["spread_ticks"])
        self._tree.heading("bid", text=headings["bid"])
        self._tree.heading("ask", text=headings["ask"])
        self._tree.column("market", anchor=tk.W)
        self._tree.column("spread_ticks", anchor=tk.E)
        self._tree.column("bid", anchor=tk.E)
        self._tree.column("ask", anchor=tk.E)
        _fit_treeview_headings(
            self._tree,
            headings,
            base_widths={"market": 180, "spread_ticks": 100, "bid": 100, "ask": 100},
        )

        scrollbar = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self._tree.yview)
        xscroll = ttk.Scrollbar(list_frame, orient=tk.HORIZONTAL, command=self._tree.xview)
        self._tree.configure(yscrollcommand=scrollbar.set, xscrollcommand=xscroll.set)
        self._tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        xscroll.pack(side=tk.BOTTOM, fill=tk.X)

        btn_frame = ttk.Frame(self)
        btn_frame.pack(fill=tk.X, padx=10, pady=5)
        ttk.Button(btn_frame, text=_("reco.refresh"), command=self._refresh).pack(side=tk.LEFT)
        ttk.Button(btn_frame, text=_("reco.close"), command=self.destroy).pack(side=tk.RIGHT)

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_close(self) -> None:
        self._stop_event.set()
        self.destroy()

    def _refresh(self) -> None:
        self._stop_event.clear()
        for item in self._tree.get_children():
            self._tree.delete(item)
        self._results.clear()
        self._start_analysis()

    def _start_analysis(self) -> None:
        self._progress_var.set("Analyzing markets...")
        threading.Thread(target=self._analyze_markets, daemon=True).start()

    def _analyze_markets(self) -> None:
        """Fetch tickers in parallel and compute spreads."""
        results = []
        total = len(self._markets)
        done = 0

        def fetch_spread(instrument: str) -> tuple[str, int | None, Decimal | None, Decimal | None]:
            meta = self._meta.get(instrument, {})
            tick_size_str = meta.get("tick_size", "0")
            try:
                tick_size = Decimal(tick_size_str)
                if tick_size <= 0:
                    return instrument, None, None, None
            except Exception:
                return instrument, None, None, None

            try:
                ticker = get_ticker(instrument)
                bid = Decimal(str(ticker.get("best_bid_price", "0")))
                ask = Decimal(str(ticker.get("best_ask_price", "0")))
                if bid <= 0 or ask <= 0:
                    return instrument, None, None, None
                spread = ask - bid
                spread_ticks = int(spread / tick_size)
                return instrument, spread_ticks, bid, ask
            except Exception:
                return instrument, None, None, None

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(fetch_spread, m): m for m in self._markets}
            for future in as_completed(futures):
                if self._stop_event.is_set():
                    return
                done += 1
                try:
                    instrument, spread_ticks, bid, ask = future.result()
                    if spread_ticks is not None and spread_ticks >= self.MIN_SPREAD_TICKS:
                        results.append((instrument, spread_ticks, bid, ask))
                except Exception:
                    pass
                # Update progress periodically
                if done % 5 == 0 or done == total:
                    self._update_progress(done, total)

        # Sort by spread descending
        results.sort(key=lambda x: x[1], reverse=True)
        self._results = results
        self.after(0, self._display_results)

    def _update_progress(self, done: int, total: int) -> None:
        self.after(0, lambda: self._progress_var.set(_("reco.analyzing_progress", done=done, total=total)))

    def _display_results(self) -> None:
        for item in self._tree.get_children():
            self._tree.delete(item)

        for instrument, spread_ticks, bid, ask in self._results:
            self._tree.insert("", tk.END, values=(instrument, spread_ticks, str(bid), str(ask)))

        count = len(self._results)
        self._progress_var.set(_("reco.found", count=count, min_ticks=self.MIN_SPREAD_TICKS))


def get_available_markets() -> list[str]:
    try:
        instruments = get_all_instruments()
        markets = [
            i["instrument"]
            for i in instruments
            if i.get("base") not in EXCLUDED_BASES and i.get("kind") == "PERPETUAL"
        ]
        return sorted(markets)
    except Exception:
        return ["CRV_USDT_Perp"]


def get_market_catalog() -> tuple[list[str], dict[str, dict[str, str]]]:
    """Return (markets, meta) where meta contains min_size/min_notional strings per market."""
    try:
        instruments = get_all_instruments()
        markets: list[str] = []
        meta: dict[str, dict[str, str]] = {}
        for i in instruments:
            if i.get("base") in EXCLUDED_BASES or i.get("kind") != "PERPETUAL":
                continue
            inst = i.get("instrument")
            if not inst:
                continue
            markets.append(inst)
            meta[inst] = {
                "min_size": str(i.get("min_size") or "0"),
                "min_notional": str(i.get("min_notional") or "0"),
                "tick_size": str(i.get("tick_size") or "0"),
            }
        return sorted(markets), meta
    except Exception:
        return ["CRV_USDT_Perp"], {}


class PositionMonitorWindow(tk.Toplevel):
    """Window showing positions for both accounts with auto-refresh and WS order tracking."""

    def __init__(self, parent: tk.Tk, account_pair: AccountPair, cookie_manager: "CookieManager", cached_positions: tuple | None = None):
        super().__init__(parent)
        self.title(_("monitor.title"))
        _set_scaled_geometry(self, 820, 400)  # Wider for orders column (scaled for DPI)
        self.account_pair = account_pair
        self.cookie_manager = cookie_manager
        self._refresh_job: str | None = None
        self._urgent_refresh_job: str | None = None
        self._refresh_inflight = threading.Event()
        self._last_rest_refresh_ts: float = 0.0
        self._cached_data = cached_positions  # (pos1, pos2, prices, timestamp) from preload
        self._ws_manager: PositionWSManager | None = None
        # WS callbacks arrive on a background thread; never touch Tk from that thread.
        self._ws_update_flag = threading.Event()
        self._ws_poll_job: str | None = None
        self._last_pos1: dict[str, Decimal] = {}
        self._last_pos2: dict[str, Decimal] = {}
        self._last_prices: dict[str, Decimal] = {}
        self._last_warn: str = ""
        self._lev_inflight = threading.Event()
        self._lev_btn: ttk.Button | None = None
        self._lev_btn_var: tk.StringVar | None = None

        self._build_ui()

        # Start WS manager for open-order updates (fast). Positions/prices still use REST (slower)
        # at a reduced interval to avoid hammering the API.
        self._ws_manager = PositionWSManager(
            self.account_pair.primary,
            self.account_pair.secondary,
            cookie_getter1=lambda: self.cookie_manager.get_cookie(browser_state_path=self.account_pair.primary.browser_state_path),
            cookie_getter2=lambda: self.cookie_manager.get_cookie(browser_state_path=self.account_pair.secondary.browser_state_path),
            on_update=self._on_ws_update,
        )
        self._ws_manager.start()
        self._update_ws_status()
        self._ws_poll_job = self.after(200, self._poll_ws_updates)
        
        # Use cached data for instant display if available
        if self._cached_data and len(self._cached_data) >= 3:
            pos1, pos2, prices = self._cached_data[0], self._cached_data[1], self._cached_data[2] if len(self._cached_data) > 2 else {}
            self._last_pos1 = pos1 or {}
            self._last_pos2 = pos2 or {}
            self._last_prices = prices or {}
            self._render()
            # Then refresh in background for fresh data
            self.after(100, self._refresh)
        else:
            self._refresh()

    def _on_ws_update(self) -> None:
        """WS update callback (runs on WS thread). Only sets a flag."""
        self._ws_update_flag.set()

    def _poll_ws_updates(self) -> None:
        """Poll WS update flag from the Tk thread and update UI safely."""
        if not self.winfo_exists():
            return
        if self._ws_update_flag.is_set():
            self._ws_update_flag.clear()
            self._update_ws_status()
            # Re-render with latest open orders; positions/prices still come from REST.
            # Schedule a REST refresh on any order activity so manual trades/fills reflect quickly.
            self._render()
            self._schedule_urgent_refresh()
        self._ws_poll_job = self.after(200, self._poll_ws_updates)

    def _update_ws_status(self) -> None:
        if not self._ws_manager:
            self.ws_status_var.set("WS: --")
            return
        c1, c2 = self._ws_manager.is_connected()
        self.ws_status_var.set(_("monitor.ws", a1=("ON" if c1 else "OFF"), a2=("ON" if c2 else "OFF")))

    def _orders_count_for_market(self, market: str) -> tuple[int, int]:
        if not self._ws_manager:
            return 0, 0
        o1, o2 = self._ws_manager.get_all_open_orders()
        return len(o1.get(market, []) or []), len(o2.get(market, []) or [])

    def _render(self) -> None:
        """Render the table from cached positions/prices + WS open orders."""
        # Clear table
        for item in self.tree.get_children():
            self.tree.delete(item)

        pos1 = self._last_pos1 or {}
        pos2 = self._last_pos2 or {}
        prices = self._last_prices or {}

        orders1, orders2 = ({}, {})
        if self._ws_manager:
            orders1, orders2 = self._ws_manager.get_all_open_orders()

        all_markets = set(pos1.keys()) | set(pos2.keys()) | set(orders1.keys()) | set(orders2.keys())

        inserted = 0
        for market in sorted(all_markets):
            p1 = pos1.get(market, Decimal("0"))
            p2 = pos2.get(market, Decimal("0"))
            price = prices.get(market, Decimal("0"))
            usd1 = abs(p1) * price
            usd2 = abs(p2) * price

            n1, n2 = self._orders_count_for_market(market)
            # Prevent "ghost rows" caused by stale/empty order keys.
            if p1 == 0 and p2 == 0 and n1 == 0 and n2 == 0:
                continue
            orders_txt = f"{n1}/{n2}" if (n1 or n2) else ""
            status_txt, tag = self._get_status(p1, p2)

            self.tree.insert(
                "",
                tk.END,
                values=(
                    market,
                    f"{p1:+.1f}" if p1 != 0 else "",
                    f"${usd1:,.0f}" if p1 != 0 else "",
                    f"{p2:+.1f}" if p2 != 0 else "",
                    f"${usd2:,.0f}" if p2 != 0 else "",
                    orders_txt,
                    status_txt,
                ),
                tags=(tag,),
            )
            inserted += 1

        if inserted == 0:
            self.tree.insert("", tk.END, values=(_("monitor.no_positions"), "", "", "", "", "", ""))
            return

    def _build_ui(self) -> None:
        # Top bar with status and WS indicator
        top = ttk.Frame(self, padding=5)
        top.pack(fill=tk.X)

        self.last_update_var = tk.StringVar(value="")
        ttk.Label(top, textvariable=self.last_update_var).pack(side=tk.LEFT)
 
        # WS connection status indicator
        self.ws_status_var = tk.StringVar(value="WS: --")
        self.ws_status_label = ttk.Label(top, textvariable=self.ws_status_var)
        self.ws_status_label.pack(side=tk.RIGHT, padx=10)

        # Manual leverage control: set all open-position markets to 50x (or highest accepted).
        self._lev_btn_var = tk.StringVar(value=_("monitor.btn.set_leverage_50"))
        self._lev_btn = ttk.Button(top, textvariable=self._lev_btn_var, command=self._set_positions_leverage_50x)
        self._lev_btn.pack(side=tk.RIGHT, padx=6)

        # Table with orders column
        columns = ("market", "acc1_size", "acc1_usd", "acc2_size", "acc2_usd", "orders", "status")
        self.tree = ttk.Treeview(self, columns=columns, show="headings", height=15)
        _configure_treeview_rowheight(self.tree)
        headings = {
            "market": _("monitor.col.market"),
            "acc1_size": _("monitor.col.a1_size"),
            "acc1_usd": _("monitor.col.a1_usd"),
            "acc2_size": _("monitor.col.a2_size"),
            "acc2_usd": _("monitor.col.a2_usd"),
            "orders": _("monitor.col.orders"),
            "status": _("monitor.col.status"),
        }
        self.tree.heading("market", text=headings["market"])
        self.tree.heading("acc1_size", text=headings["acc1_size"])
        self.tree.heading("acc1_usd", text=headings["acc1_usd"])
        self.tree.heading("acc2_size", text=headings["acc2_size"])
        self.tree.heading("acc2_usd", text=headings["acc2_usd"])
        self.tree.heading("orders", text=headings["orders"])
        self.tree.heading("status", text=headings["status"])

        self.tree.column("market", anchor=tk.W)
        self.tree.column("acc1_size", anchor=tk.E)
        self.tree.column("acc1_usd", anchor=tk.E)
        self.tree.column("acc2_size", anchor=tk.E)
        self.tree.column("acc2_usd", anchor=tk.E)
        self.tree.column("orders", anchor=tk.CENTER)
        self.tree.column("status", anchor=tk.W)
        _fit_treeview_headings(
            self.tree,
            headings,
            base_widths={
                "market": 140,
                "acc1_size": 90,
                "acc1_usd": 90,
                "acc2_size": 90,
                "acc2_usd": 90,
                "orders": 80,
                "status": 90,
            },
        )

        # Tags for coloring
        self.tree.tag_configure("hedge", foreground="green")
        self.tree.tag_configure("unbalanced", foreground="orange")
        self.tree.tag_configure("same_side", foreground="red")

        scrollbar = ttk.Scrollbar(self, orient=tk.VERTICAL, command=self.tree.yview)
        xscroll = ttk.Scrollbar(self, orient=tk.HORIZONTAL, command=self.tree.xview)
        self.tree.configure(yscrollcommand=scrollbar.set, xscrollcommand=xscroll.set)

        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5, pady=5)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y, pady=5)
        xscroll.pack(side=tk.BOTTOM, fill=tk.X, padx=5, pady=(0, 5))

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _set_positions_leverage_50x(self) -> None:
        """Set initial leverage to 50x for markets with open positions (both accounts)."""
        if self._lev_inflight.is_set() or not self.winfo_exists():
            return

        if not messagebox.askyesno(_("monitor.lev.title"), _("monitor.lev.confirm")):
            return

        # Disable the button while we run (avoid duplicate clicks and keep UI responsive).
        self._lev_inflight.set()
        if self._lev_btn is not None:
            try:
                self._lev_btn.configure(state=tk.DISABLED)
            except Exception:
                pass
        if self._lev_btn_var is not None:
            try:
                self._lev_btn_var.set(_("monitor.lev.running"))
            except Exception:
                pass

        def work():
            c1 = self.cookie_manager.get_cookie(browser_state_path=self.account_pair.primary.browser_state_path)
            c2 = self.cookie_manager.get_cookie(browser_state_path=self.account_pair.secondary.browser_state_path)
            if not c1 or not c2:
                return ("cookie_error",)

            pos1 = get_all_positions_map(self.account_pair.primary, c1)
            pos2 = get_all_positions_map(self.account_pair.secondary, c2)
            if pos1 is None and pos2 is None:
                return ("auth_error",)
            pos1 = pos1 or {}
            pos2 = pos2 or {}

            lev1_items = get_all_initial_leverage(self.account_pair.primary, c1) or []
            lev2_items = get_all_initial_leverage(self.account_pair.secondary, c2) or []

            def build_lev_map(items: list[dict]) -> dict[str, str]:
                out: dict[str, str] = {}
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    inst = it.get("i") or it.get("instrument")
                    lev = it.get("l") or it.get("leverage")
                    if inst is None or lev is None:
                        continue
                    out[str(inst)] = str(lev)
                return out

            cur1 = build_lev_map(lev1_items)
            cur2 = build_lev_map(lev2_items)

            def is_50x(v: str | None) -> bool:
                if not v:
                    return False
                try:
                    return int(float(str(v))) == 50
                except Exception:
                    return str(v).strip() == "50"

            ok = 0
            fail = 0
            skipped = 0
            fallback = 0
            lines: list[str] = []

            def apply_for(acc, cookie: str, positions: dict[str, Decimal], cur: dict[str, str]) -> None:
                nonlocal ok, fail, skipped, fallback, lines
                for inst in sorted(positions.keys()):
                    if is_50x(cur.get(inst)):
                        skipped += 1
                        continue
                    lev = ensure_initial_leverage(acc, cookie, instrument=inst, target_leverage="50")
                    if lev is None:
                        fail += 1
                        lines.append(f"{acc.name} {inst}: FAILED")
                        continue
                    ok += 1
                    if str(lev) != "50":
                        fallback += 1
                    lines.append(f"{acc.name} {inst}: {lev}x")

            apply_for(self.account_pair.primary, c1, pos1, cur1)
            apply_for(self.account_pair.secondary, c2, pos2, cur2)

            return ("ok", ok, fail, skipped, fallback, lines)

        def finish(res) -> None:
            try:
                if not self.winfo_exists():
                    return

                if self._lev_btn is not None:
                    try:
                        self._lev_btn.configure(state=tk.NORMAL)
                    except Exception:
                        pass
                if self._lev_btn_var is not None:
                    try:
                        self._lev_btn_var.set(_("monitor.btn.set_leverage_50"))
                    except Exception:
                        pass

                if not res:
                    messagebox.showerror(_("monitor.lev.title"), _("monitor.lev.failed"))
                    return

                status = res[0]
                if status == "cookie_error":
                    messagebox.showerror(_("monitor.lev.title"), _("monitor.lev.cookie_error"))
                    return
                if status == "auth_error":
                    messagebox.showerror(_("monitor.lev.title"), _("monitor.lev.auth_error"))
                    return
                if status != "ok":
                    messagebox.showerror(_("monitor.lev.title"), _("monitor.lev.failed"))
                    return

                _, ok, fail, skipped, fallback, lines = res
                body = _("monitor.lev.result", ok=ok, fail=fail, skipped=skipped, fallback=fallback)
                if lines:
                    body += "\n\n" + "\n".join(lines[:80])
                    if len(lines) > 80:
                        body += "\n..."
                messagebox.showinfo(_("monitor.lev.title"), body)
            finally:
                self._lev_inflight.clear()

        def run():
            try:
                res = work()
            except Exception:
                res = ("error",)
            self.after(0, lambda r=res: finish(r))

        threading.Thread(target=run, daemon=True).start()

    def _refresh(self) -> None:
        # Avoid overlapping refresh threads (can happen with rapid WS updates).
        if self._refresh_inflight.is_set():
            self._schedule_next()
            return

        # Cancel pending refresh
        if self._refresh_job:
            self.after_cancel(self._refresh_job)
            self._refresh_job = None

        def fetch():
            c1 = self.cookie_manager.get_cookie(browser_state_path=self.account_pair.primary.browser_state_path)
            c2 = self.cookie_manager.get_cookie(browser_state_path=self.account_pair.secondary.browser_state_path)
            if not c1:
                return "cookie_error", "Account 1: Cookie refresh failed", None, {}
            if not c2:
                return "cookie_error", "Account 2: Cookie refresh failed", None, {}

            pos1 = get_all_positions_map(self.account_pair.primary, c1)
            pos2 = get_all_positions_map(self.account_pair.secondary, c2)

            # Allow partial results: show whichever account still works.
            warn = []
            if pos1 is None:
                warn.append("Account 1: Session expired/invalid")
                pos1 = {}
            if pos2 is None:
                warn.append("Account 2: Session expired/invalid")
                pos2 = {}

            # Get prices for all markets CONCURRENTLY (much faster with many markets)
            prices = {}
            all_markets = set(pos1.keys()) | set(pos2.keys())
            
            def fetch_price(market):
                try:
                    ticker = get_ticker(market)
                    mid = (Decimal(ticker["best_bid_price"]) + Decimal(ticker["best_ask_price"])) / 2
                    return market, mid
                except Exception:
                    return market, Decimal("0")
            
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=10) as executor:
                results = list(executor.map(fetch_price, all_markets))
            for market, mid in results:
                prices[market] = mid
            
            return "ok", pos1, pos2, prices, (" | ".join(warn) if warn else "")

        def update(result):
            # Check if window still exists
            if not self.winfo_exists():
                self._refresh_inflight.clear()
                return

            status, *data = result
            if status == "cookie_error":
                self.last_update_var.set(f"ERROR: {data[0]}")
                self._refresh_inflight.clear()
                self._schedule_next()
                return
            if status == "error":
                self.last_update_var.set(f"ERROR: {data[0]}")
                self._refresh_inflight.clear()
                self._schedule_next()
                return

            pos1, pos2, prices, warn = data
            self._last_pos1 = pos1 or {}
            self._last_pos2 = pos2 or {}
            self._last_prices = prices or {}
            self._last_warn = warn or ""
            self._last_rest_refresh_ts = time.time()

            self._render()
            self._update_ws_status()

            ts = time.strftime("%H:%M:%S")
            self.last_update_var.set(
                _("monitor.last_updated_warn", ts=ts, warn=warn) if warn else _("monitor.last_updated", ts=ts)
            )
            self._refresh_inflight.clear()
            self._schedule_next()

        def run():
            self._refresh_inflight.set()
            try:
                result = fetch()
            except Exception as e:
                result = ("error", f"{type(e).__name__}: {e}")
            self.after(0, lambda: update(result))

        threading.Thread(target=run, daemon=True).start()

    def _get_status(self, pos1: Decimal, pos2: Decimal) -> tuple[str, str]:
        if pos1 == 0 and pos2 == 0:
            return "", ""
        if pos1 * pos2 < 0:  # Opposite directions
            if abs(pos1) == abs(pos2):
                return _("monitor.status.hedge"), "hedge"
            return _("monitor.status.unbalanced"), "unbalanced"
        return _("monitor.status.same_side"), "same_side"

    def _schedule_next(self) -> None:
        # WS only covers open orders; positions still require REST.
        interval_ms = 8000
        if self._ws_manager:
            c1, c2 = self._ws_manager.is_connected()
            if not (c1 or c2):
                interval_ms = 4000
        self._refresh_job = self.after(interval_ms, self._refresh)

    def _schedule_urgent_refresh(self) -> None:
        """Debounced refresh used when WS reports any order activity."""
        if not self.winfo_exists():
            return
        # If we refreshed very recently, skip (prevents API spam during bursty WS updates).
        if (time.time() - self._last_rest_refresh_ts) < 0.75:
            return
        if self._urgent_refresh_job:
            return
        self._urgent_refresh_job = self.after(250, self._run_urgent_refresh)

    def _run_urgent_refresh(self) -> None:
        self._urgent_refresh_job = None
        if not self.winfo_exists():
            return
        self._refresh()

    def _display_cached(self, pos1: dict, pos2: dict, prices: dict) -> None:
        """Display cached positions with USD values instantly."""
        self._last_pos1 = pos1 or {}
        self._last_pos2 = pos2 or {}
        self._last_prices = prices or {}
        self._render()
        self.last_update_var.set(_("monitor.loading"))

    def _on_close(self) -> None:
        if self._refresh_job:
            self.after_cancel(self._refresh_job)
        if self._urgent_refresh_job:
            try:
                self.after_cancel(self._urgent_refresh_job)
            except Exception:
                pass
            self._urgent_refresh_job = None
        if self._ws_poll_job:
            try:
                self.after_cancel(self._ws_poll_job)
            except Exception:
                pass
            self._ws_poll_job = None
        if self._ws_manager:
            self._ws_manager.stop()
        self.destroy()


class CookieManager:
    def __init__(self, *, ttl_sec: int = DEFAULT_COOKIE_REFRESH_INTERVAL_SEC):
        self._ttl_sec = ttl_sec
        self._lock = threading.Lock()
        self._cache: dict[Path, tuple[str, float]] = {}
        self._per_path_lock: dict[Path, threading.Lock] = {}

    def get_cookie(self, *, browser_state_path: Path, validate: Callable[[str], bool] | None = None) -> str | None:
        """Get cookie with lazy validation.
        
        Returns cached cookie immediately if fresh (< TTL).
        Only calls validate() if cache is stale or empty to avoid network round-trips.
        """
        now = time.time()
        with self._lock:
            cached = self._cache.get(browser_state_path)
            # Fast path: return cached cookie if fresh (no validation to avoid network call)
            if cached and (now - cached[1]) <= self._ttl_sec:
                return cached[0]
            lock = self._per_path_lock.get(browser_state_path)
            if lock is None:
                lock = threading.Lock()
                self._per_path_lock[browser_state_path] = lock

        with lock:
            now = time.time()
            with self._lock:
                cached = self._cache.get(browser_state_path)
                if cached and (now - cached[1]) <= self._ttl_sec:
                    return cached[0]

            # Cache miss or stale - fetch fresh cookie (only validate if stale)
            cookie = get_fresh_cookie(browser_state_path)
            if cookie and validate is not None and not validate(cookie):
                cookie = get_fresh_cookie(browser_state_path, force_refresh=True)
            if cookie:
                with self._lock:
                    self._cache[browser_state_path] = (cookie, time.time())
            return cookie

    def get_cookies_for_pair(
        self,
        pair: AccountPair,
        *,
        validate_primary: Callable[[str], bool] | None = None,
        validate_secondary: Callable[[str], bool] | None = None,
    ) -> tuple[str | None, str | None]:
        # Fetch cookies in parallel to cut worst-case latency in half
        from concurrent.futures import ThreadPoolExecutor, as_completed
        
        def get_primary():
            return self.get_cookie(browser_state_path=pair.primary.browser_state_path, validate=validate_primary)
        
        def get_secondary():
            return self.get_cookie(browser_state_path=pair.secondary.browser_state_path, validate=validate_secondary)
        
        with ThreadPoolExecutor(max_workers=2) as executor:
            future_primary = executor.submit(get_primary)
            future_secondary = executor.submit(get_secondary)
            c1 = future_primary.result()
            c2 = future_secondary.result()
        
        return c1, c2


@dataclass(frozen=True)
class RunConfig:
    market: str
    mode: str
    rounds: int
    delay_sec: float
    max_margin: float
    hold_minutes: int
    direction_policy: str


class MarketRunPanel(ttk.Frame):
    def __init__(self, parent: ttk.Notebook, *, app: "VolumeBoostGUI"):
        super().__init__(parent, padding=10)
        self.app = app

        self._running = threading.Event()
        self._thread: threading.Thread | None = None
        self._queue: queue.Queue[tuple[str, str]] = queue.Queue()
        # Keep GUI logs high-signal by default; debug can be toggled on.
        self._show_debug_var = tk.BooleanVar(value=False)

        self._sticky_policy_for_run: str | None = None
        self._resolved_size_for_run: Decimal | None = None
        self._ticker_monitor: TickerMonitor | None = None
        self._order_ws: OrderStreamClient | None = None

        self._build_ui()
        self._apply_mode_visibility()
        
        # Min hint updates are triggered by explicit events only (not traces to avoid lag)
        try:
            # Debounce size entry updates
            self._size_hint_timer: str | None = None
            def debounced_size_hint(*_):
                if self._size_hint_timer:
                    self.after_cancel(self._size_hint_timer)
                self._size_hint_timer = self.after(300, self._update_min_hint)
            self.size_entry.bind("<KeyRelease>", debounced_size_hint)
            self.notional_entry.bind("<KeyRelease>", debounced_size_hint)
        except Exception:
            pass
        self._update_min_hint()
        self.after(100, self._drain_queue)

    def _build_ui(self) -> None:
        header = ttk.Frame(self)
        header.pack(fill=tk.X)

        left = ttk.Frame(header)
        left.pack(side=tk.LEFT, fill=tk.X, expand=True)

        ttk.Label(left, text=_("panel.market")).grid(row=0, column=0, sticky=tk.W, pady=2)
        self.market_var = tk.StringVar(value="?")
        self._market_display_to_full: dict[str, str] = {}  # "BNB" -> "BNB_USDT_Perp"
        self._market_spreads: dict[str, int] = {}  # "BNB_USDT_Perp" -> spread_ticks
        self._top_markets: set[str] = set()  # Top 5 by spread
        
        market_frame = ttk.Frame(left)
        market_frame.grid(row=0, column=1, sticky=tk.W, pady=2)
        self.market_combo = ttk.Combobox(market_frame, textvariable=self.market_var, width=18)
        self.market_combo.pack(side=tk.LEFT)
        self.market_combo.bind("<KeyRelease>", self._filter_markets)
        self.market_combo.bind("<<ComboboxSelected>>", self._on_market_selected)
        
        self._choose_btn = ttk.Button(market_frame, text=_("panel.pick_for_me"), command=self._choose_for_me)
        self._choose_btn.pack(side=tk.LEFT, padx=2)
        
        self._pick_progress_var = tk.StringVar(value="")
        self._pick_progress_label = ttk.Label(market_frame, textvariable=self._pick_progress_var, foreground="gray")
        self._pick_progress_label.pack(side=tk.LEFT, padx=4)

        ttk.Label(left, text=_("panel.mode")).grid(row=1, column=0, sticky=tk.W, pady=2)
        self.mode_var = tk.StringVar(value="instant")
        mode_frame = ttk.Frame(left)
        mode_frame.grid(row=1, column=1, sticky=tk.W, pady=2)
        ttk.Radiobutton(
            mode_frame, text=_("panel.mode.instant"), variable=self.mode_var, value="instant", command=self._apply_mode_visibility
        ).grid(row=0, column=0, sticky=tk.W)
        ttk.Radiobutton(
            mode_frame,
            text=_("panel.mode.build_hold_close"),
            variable=self.mode_var,
            value="build_hold_close",
            command=self._apply_mode_visibility,
        ).grid(row=0, column=1, sticky=tk.W, padx=8)
        ttk.Radiobutton(
            mode_frame, text=_("panel.mode.build_hold"), variable=self.mode_var, value="build_hold", command=self._apply_mode_visibility
        ).grid(row=1, column=0, sticky=tk.W)
        ttk.Radiobutton(
            mode_frame,
            text=_("panel.mode.close_existing"),
            variable=self.mode_var,
            value="close_existing",
            command=self._apply_mode_visibility,
        ).grid(row=1, column=1, sticky=tk.W, padx=8)

        right = ttk.Frame(header)
        right.pack(side=tk.RIGHT, fill=tk.Y)
        self.start_btn = ttk.Button(right, text=_("panel.start"), command=self.start, width=12)
        self.start_btn.pack(anchor=tk.E, pady=2)
        self.stop_btn = ttk.Button(right, text=_("panel.stop"), command=self.stop, width=12, state=tk.DISABLED)
        self.stop_btn.pack(anchor=tk.E, pady=2)
        
        # Spread warning (shown when spread is tight)
        self.spread_warn_var = tk.StringVar(value="")
        self.spread_warn_label = ttk.Label(right, textvariable=self.spread_warn_var, foreground="orange")
        self.spread_warn_label.pack(anchor=tk.E, pady=1)
        ttk.Checkbutton(right, text=_("panel.debug"), variable=self._show_debug_var).pack(anchor=tk.E, pady=(2, 0))

        self.params = ttk.LabelFrame(self, text=_("panel.params"), padding=10)
        self.params.pack(fill=tk.X, pady=8)

        ttk.Label(self.params, text=_("panel.size_type")).grid(row=0, column=0, sticky=tk.W, pady=2)
        # Keep internal value stable; display strings are localized.
        self.size_type_var = tk.StringVar(value=SIZE_TYPE_CONTRACTS)
        self.size_type_display_var = tk.StringVar(value=_("panel.size_type.contracts"))
        self._size_type_display_to_internal = {
            _("panel.size_type.contracts"): SIZE_TYPE_CONTRACTS,
            _("panel.size_type.usd"): SIZE_TYPE_USD_NOTIONAL,
        }
        self.size_type_combo = ttk.Combobox(
            self.params,
            textvariable=self.size_type_display_var,
            values=list(self._size_type_display_to_internal.keys()),
            state="readonly",
            width=14,
        )
        self.size_type_combo.grid(row=0, column=1, sticky=tk.W, pady=2)
        def _on_size_type_change(_e=None):
            v = self.size_type_display_var.get()
            internal = self._size_type_display_to_internal.get(v, SIZE_TYPE_CONTRACTS)
            self.size_type_var.set(internal)
            self._apply_mode_visibility()
        self.size_type_combo.bind("<<ComboboxSelected>>", _on_size_type_change)

        self.size_label = ttk.Label(self.params, text=_("panel.size_contracts"))
        self.size_label.grid(row=1, column=0, sticky=tk.W, pady=2)
        self.size_var = tk.StringVar(value="100")
        self.size_entry = ttk.Entry(self.params, textvariable=self.size_var, width=14)
        self.size_entry.grid(row=1, column=1, sticky=tk.W, pady=2)
        self.min_hint_label = ttk.Label(self.params, text="", foreground="gray")
        self.min_hint_label.grid(row=1, column=2, sticky=tk.W, padx=8)

        self.notional_label = ttk.Label(self.params, text=_("panel.notional_usd"))
        self.notional_var = tk.StringVar(value="100")
        self.notional_entry = ttk.Entry(self.params, textvariable=self.notional_var, width=14)
        self.computed_size_var = tk.StringVar(value="")
        self.computed_size_label = ttk.Label(self.params, textvariable=self.computed_size_var)

        ttk.Label(self.params, text=_("panel.direction")).grid(row=2, column=0, sticky=tk.W, pady=2)
        self._direction_display_to_policy = {
            _("panel.dir.random"): SIDE_POLICY_RANDOM,
            _("panel.dir.a1_long"): SIDE_POLICY_ACCOUNT1_LONG,
            _("panel.dir.a1_short"): SIDE_POLICY_ACCOUNT1_SHORT,
        }
        self.direction_var = tk.StringVar(value=_("panel.dir.random"))
        self.direction_combo = ttk.Combobox(
            self.params,
            textvariable=self.direction_var,
            values=list(self._direction_display_to_policy.keys()),
            state="readonly",
            width=18,
        )
        self.direction_combo.grid(row=2, column=1, sticky=tk.W, pady=2)
        ttk.Label(self.params, text=self.app.account1_display()).grid(row=2, column=2, sticky=tk.W, padx=8)

        ttk.Label(self.params, text=_("panel.rounds")).grid(row=3, column=0, sticky=tk.W, pady=2)
        self.rounds_var = tk.StringVar(value="10")
        self.rounds_entry = ttk.Entry(self.params, textvariable=self.rounds_var, width=14)
        self.rounds_entry.grid(row=3, column=1, sticky=tk.W, pady=2)

        self.delay_label = ttk.Label(self.params, text=_("panel.delay"))
        self.delay_label.grid(row=4, column=0, sticky=tk.W, pady=2)
        self.delay_var = tk.StringVar(value="1.0")
        self.delay_entry = ttk.Entry(self.params, textvariable=self.delay_var, width=14)
        self.delay_entry.grid(row=4, column=1, sticky=tk.W, pady=2)

        self.margin_label = ttk.Label(self.params, text=_("panel.max_margin"))
        self.margin_label.grid(row=5, column=0, sticky=tk.W, pady=2)
        self.margin_var = tk.StringVar(value="15")
        self.margin_entry = ttk.Entry(self.params, textvariable=self.margin_var, width=14)
        self.margin_entry.grid(row=5, column=1, sticky=tk.W, pady=2)

        self.hold_label = ttk.Label(self.params, text=_("panel.hold"))
        self.hold_label.grid(row=6, column=0, sticky=tk.W, pady=2)
        self.hold_var = tk.StringVar(value="30")
        self.hold_entry = ttk.Entry(self.params, textvariable=self.hold_var, width=14)
        self.hold_entry.grid(row=6, column=1, sticky=tk.W, pady=2)

        self.close_method_var = tk.StringVar(value="percent")
        self.close_method_label = ttk.Label(self.params, text="Close method:")
        self.close_method_frame = ttk.Frame(self.params)
        ttk.Radiobutton(
            self.close_method_frame,
            text="Percent",
            variable=self.close_method_var,
            value="percent",
            command=self._apply_mode_visibility,
        ).grid(row=0, column=0, sticky=tk.W)
        ttk.Radiobutton(
            self.close_method_frame,
            text="Fixed size",
            variable=self.close_method_var,
            value="size",
            command=self._apply_mode_visibility,
        ).grid(row=0, column=1, sticky=tk.W, padx=8)

        self.close_pct_label = ttk.Label(self.params, text="Close %:")
        self.close_pct_var = tk.StringVar(value="50")
        self.close_pct_combo = ttk.Combobox(
            self.params,
            textvariable=self.close_pct_var,
            width=12,
            state="readonly",
            values=["10", "25", "50", "75", "100"],
        )

        self.close_size_label = ttk.Label(self.params, text="Close size:")
        self.close_size_var = tk.StringVar(value="100")
        self.close_size_entry = ttk.Entry(self.params, textvariable=self.close_size_var, width=14)

        ttk.Label(self, text="Status:").pack(anchor=tk.W)
        self.status_text = scrolledtext.ScrolledText(self, height=16, width=78, state=tk.NORMAL)
        self.status_text.pack(fill=tk.BOTH, expand=True)
        self.status_text.tag_configure("error", foreground="red", font=("TkDefaultFont", 9, "bold"))

    def set_markets(self, markets: list[str]) -> None:
        """Set markets with short display names (BNB instead of BNB_USDT_Perp)."""
        display_names = []
        for m in markets:
            short = self._to_short_name(m)
            display_names.append(short)
            self._market_display_to_full[short] = m
        self.market_combo.configure(values=display_names)
        self._update_min_hint()

    def _min_requirements(self) -> tuple[Decimal, Decimal]:
        """Return (min_size, min_notional_usd) for current market from cache only.
        
        Never makes network calls - returns (0, 0) if cache miss to avoid UI lag.
        """
        market = self._selected_market_full()
        if not market:
            return Decimal("0"), Decimal("0")
        meta = self.app.market_meta.get(market) if hasattr(self.app, "market_meta") else None
        if meta:
            try:
                return Decimal(str(meta.get("min_size") or "0")), Decimal(str(meta.get("min_notional") or "0"))
            except Exception:
                pass
        # Cache miss - return zeros rather than blocking UI with network call
        return Decimal("0"), Decimal("0")

    def _update_min_hint(self) -> None:
        """Update the UI hint with min size/notional info (shows in all modes)."""
        min_size, min_notional = self._min_requirements()

        # Show min size and min notional info
        hint = _("hint.min_size", min_size=min_size)
        if min_notional > 0:
            hint = _("hint.min_size_notional", min_size=min_size, min_notional=min_notional)

        # Validate entered size without blocking typing (only in contracts mode)
        bad = False
        size_type = self.size_type_var.get()
        if size_type != SIZE_TYPE_USD_NOTIONAL:
            try:
                size = Decimal(self.size_var.get())
                if min_size > 0 and size < min_size:
                    bad = True
                    hint = _("hint.min_size_low", min_size=min_size)
            except Exception:
                pass

        try:
            self.min_hint_label.configure(text=hint, foreground=("red" if bad else "gray"))
            self.min_hint_label.grid()
        except Exception:
            pass
        
        # Also validate START button state
        self._validate_inputs()

    def _validate_inputs(self) -> None:
        """Check if all inputs are valid and enable/disable START button accordingly."""
        try:
            # Check accounts configured
            if not self.app.account_pair:
                self.start_btn.configure(state=tk.DISABLED)
                return
            
            # Check market selected
            if not self._selected_market_full():
                self.start_btn.configure(state=tk.DISABLED)
                return
            
            # Check size/notional entered
            size_type = self.size_type_var.get()
            if size_type == SIZE_TYPE_USD_NOTIONAL:
                try:
                    val = Decimal(self.notional_var.get())
                    if val <= 0:
                        self.start_btn.configure(state=tk.DISABLED)
                        return
                except Exception:
                    self.start_btn.configure(state=tk.DISABLED)
                    return
            else:
                try:
                    val = Decimal(self.size_var.get())
                    if val <= 0:
                        self.start_btn.configure(state=tk.DISABLED)
                        return
                except Exception:
                    self.start_btn.configure(state=tk.DISABLED)
                    return
            
            # Check rounds
            try:
                rounds = int(self.rounds_var.get())
                if rounds <= 0:
                    self.start_btn.configure(state=tk.DISABLED)
                    return
            except Exception:
                self.start_btn.configure(state=tk.DISABLED)
                return
            
            # All valid - enable if not running
            if not self._running.is_set():
                self.start_btn.configure(state=tk.NORMAL)
        except Exception:
            pass

    def _update_spread_warning(self) -> None:
        """Check spread for selected market (UI thread safe version)."""
        market = self._selected_market_full()
        if market:
            threading.Thread(target=lambda: self._update_spread_warning_async(market), daemon=True).start()
        else:
            self.spread_warn_var.set("")

    def _update_spread_warning_async(self, market: str) -> None:
        """Check spread in background thread, update UI safely via self.after."""
        try:
            # Get tick size from market meta (safe - dict access)
            meta = self.app.market_meta.get(market, {})
            tick_size = Decimal(str(meta.get("tick_size") or "0.01"))
            
            # Get current spread (network call)
            ticker = get_ticker(market)
            bid = Decimal(str(ticker.get("best_bid_price", "0")))
            ask = Decimal(str(ticker.get("best_ask_price", "0")))
            
            if bid > 0 and ask > 0 and tick_size > 0:
                spread_ticks = int((ask - bid) / tick_size)
                # Always show spread ticks for the selected market (helps explain external fills).
                if spread_ticks <= 2:
                    result = _("spread.tight", ticks=spread_ticks)
                else:
                    result = _("spread.ok", ticks=spread_ticks)
            else:
                result = ""
        except Exception:
            result = ""
        
        # Update Tk var safely on UI thread
        try:
            def apply_result() -> None:
                # If the user changed the selected market while this thread was running,
                # don't overwrite the label with a stale result.
                if self._selected_market_full() != market:
                    return
                self.spread_warn_var.set(result)

            self.after(0, apply_result)
        except Exception:
            pass

    def _preflight_min_check(self) -> None:
        """Fail fast if requested size/notional is below instrument minimums."""
        market = self._selected_market_full()
        if not market:
            raise ValueError("Market is required")
        min_size, min_notional = self._min_requirements()
        size_type = self.size_type_var.get()

        if size_type == SIZE_TYPE_USD_NOTIONAL:
            notional = Decimal(self.notional_var.get())
            if min_notional > 0 and notional < min_notional:
                raise ValueError(f"Notional ${notional} is below minimum ${min_notional} for {market}")
            # Also ensure computed size would meet min_size using current mid.
            if min_size > 0:
                try:
                    ticker = get_ticker(market)
                    mid = (Decimal(str(ticker["best_bid_price"])) + Decimal(str(ticker["best_ask_price"]))) / 2
                    est_size = notional / mid if mid > 0 else Decimal("0")
                    if est_size < min_size:
                        raise ValueError(f"Computed size {est_size} is below minimum size {min_size} for {market}")
                except ValueError:
                    raise
                except Exception:
                    # If we can't fetch price, at least enforce min_notional.
                    pass
            return

        # Contracts mode
        size = Decimal(self.size_var.get())
        if min_size > 0 and size < min_size:
            raise ValueError(f"Size {size} is below minimum size {min_size} for {market}")
        if min_notional > 0:
            # Best-effort: compute notional using current mid to avoid runtime failures.
            try:
                ticker = get_ticker(market)
                mid = (Decimal(str(ticker["best_bid_price"])) + Decimal(str(ticker["best_ask_price"]))) / 2
                notional = size * mid
                if notional < min_notional:
                    raise ValueError(f"Order notional ${notional:.2f} is below minimum ${min_notional} for {market}")
            except ValueError:
                raise
            except Exception:
                pass

    def start(self) -> None:
        if self._running.is_set():
            return

        try:
            _ = self._parse_run_config()
            # Note: _preflight_min_check removed from UI thread to avoid network lag
            # Validation now uses cached values from market_meta
        except Exception as e:
            messagebox.showerror("Error", str(e))
            return

        self.status_text.delete(1.0, tk.END)
        self._sticky_policy_for_run = None
        self._resolved_size_for_run = None
        self._running.set()
        self.start_btn.configure(state=tk.DISABLED)
        self.stop_btn.configure(state=tk.NORMAL)
        self.direction_combo.configure(state=tk.DISABLED)

        self._thread = threading.Thread(target=self._run_thread, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if not self._running.is_set():
            return
        self._running.clear()
        self._log("Stop requested; stopping after current action...")

    def _show_external_fill_warning(self, warning_msg: str) -> bool:
        """Thread-safe dialog to ask user if they want to continue after external fills detected.
        Returns True if user wants to continue, False to stop.
        """
        result = [False]  # Default to stop
        event = threading.Event()
        
        def show_dialog():
            dialog = tk.Toplevel(self)
            dialog.title("⚠ External Fills Detected")
            dialog.geometry(f"{_px(dialog, 480)}x{_px(dialog, 180)}")
            dialog.transient(self)
            dialog.grab_set()
            
            ttk.Label(dialog, text="⚠ External fills detected!", 
                      font=("", 11, "bold"), foreground="orange").pack(pady=(15, 5))
            ttk.Label(dialog, text=warning_msg, wraplength=_px(dialog, 450)).pack(pady=5)
            ttk.Label(dialog, text="Market might be unstable. Continue remaining iterations?").pack(pady=5)
            
            btn_frame = ttk.Frame(dialog)
            btn_frame.pack(pady=15)
            
            def choose(continue_run):
                result[0] = continue_run
                dialog.destroy()
                event.set()
            
            tk.Button(btn_frame, text="🛑 Stop", command=lambda: choose(False),
                      bg="#FF6B6B", fg="white", width=12).pack(side=tk.LEFT, padx=10)
            tk.Button(btn_frame, text="▶ Continue", command=lambda: choose(True),
                      bg="#4CAF50", fg="white", width=12).pack(side=tk.LEFT, padx=10)
            
            dialog.protocol("WM_DELETE_WINDOW", lambda: choose(False))
        
        # Schedule dialog on UI thread and wait
        self.after(0, show_dialog)
        event.wait()  # Block worker thread until user responds
        return result[0]

    def _show_stop_required(self, title: str, body: str) -> None:
        """Thread-safe blocking dialog for situations that require manual intervention."""
        event = threading.Event()

        def show_dialog():
            dialog = tk.Toplevel(self)
            dialog.title(title)
            dialog.geometry(f"{_px(dialog, 520)}x{_px(dialog, 180)}")
            dialog.transient(self)
            dialog.grab_set()

            ttk.Label(dialog, text=title, font=("", 11, "bold"), foreground="orange").pack(pady=(15, 5))
            ttk.Label(dialog, text=body, wraplength=_px(dialog, 490)).pack(pady=5)

            btn_frame = ttk.Frame(dialog)
            btn_frame.pack(pady=15)

            def close():
                dialog.destroy()
                event.set()

            tk.Button(btn_frame, text="OK", command=close, width=12).pack(side=tk.LEFT, padx=10)
            dialog.protocol("WM_DELETE_WINDOW", close)

        self.after(0, show_dialog)
        event.wait()

    def _filter_markets(self, _event) -> None:
        typed = (self.market_var.get() or "").upper()
        display_names = list(self._market_display_to_full.keys()) or [self._to_short_name(m) for m in self.app.markets]
        if not typed or not display_names:
            self.market_combo.configure(values=display_names)
            return
        filtered = [m for m in display_names if typed in m.upper()]  # Changed to contains match
        self.market_combo.configure(values=filtered if filtered else display_names)
        
        # Auto-open dropdown to show matches as user types
        if typed and filtered:
            self.market_combo.event_generate('<Down>')

    def _on_market_selected(self, _event) -> None:
        """Called when user selects a market from dropdown."""
        self.app.rename_tab_for(self)
        self._update_min_hint()
        # Check spread in background - capture market on UI thread first
        market = self._selected_market_full()
        if market:
            threading.Thread(target=lambda: self._update_spread_warning_async(market), daemon=True).start()

    def _to_short_name(self, full_name: str) -> str:
        """Convert BNB_USDT_Perp to BNB."""
        return full_name.replace("_USDT_Perp", "").replace("_USDT_perp", "")

    def _to_full_name(self, short_name: str) -> str:
        """Convert short name back to full instrument name."""
        if short_name in self._market_display_to_full:
            return self._market_display_to_full[short_name]
        # Fallback: try to find in app.markets
        for m in self.app.markets:
            if m.startswith(short_name + "_"):
                return m
        return short_name + "_USDT_Perp"

    def _selected_market_full(self) -> str:
        """Get the full instrument name for the currently selected market.
        
        Handles display names like '★ PAXG (55)' or 'BNB' and converts to 'PAXG_USDT_Perp'.
        """
        display = (self.market_var.get() or "").strip()
        if not display:
            return ""

        # Fast path: the combobox value is usually a key in our mapping, including starred entries
        # like "★ PAXG (55)".
        direct = self._market_display_to_full.get(display)
        if direct:
            return direct

        # Strip star prefix and spread suffix: "★ PAXG (55)" -> "PAXG (55)".
        s = display.lstrip("★").strip()
        direct = self._market_display_to_full.get(s)
        if direct:
            return direct

        token = s.split()[0].strip() if s else ""
        if not token:
            return ""

        # If the user typed the short name, resolve via mapping; otherwise only accept a real
        # instrument name that exists in the market catalog to avoid "fake" markets like "?_USDT_Perp".
        mapped = self._market_display_to_full.get(token)
        if mapped:
            return mapped
        if token in self.app.markets:
            return token
        if "_" not in token:
            candidate = token + "_USDT_Perp"
            if candidate in self.app.markets:
                return candidate
        for m in self.app.markets:
            if m.startswith(token + "_"):
                return m
        return ""

    def _choose_for_me(self) -> None:
        """Analyze market spreads and reorder dropdown with top 5 highlighted."""
        self._choose_btn.configure(state=tk.DISABLED)
        self.market_combo.configure(values=[_("common.loading")])
        threading.Thread(target=self._analyze_and_sort_markets, daemon=True).start()

    def _analyze_and_sort_markets(self) -> None:
        """Background thread: fetch spreads and reorder markets."""
        # Ensure markets are loaded
        if not self.app.market_meta:
            markets, meta = get_market_catalog()
            self.app.markets = markets
            self.app.market_meta = meta

        results = []
        MIN_SPREAD_TICKS = 4

        def fetch_spread(instrument: str) -> tuple[str, int | None]:
            meta = self.app.market_meta.get(instrument, {})
            tick_size_str = meta.get("tick_size", "0")
            try:
                tick_size = Decimal(tick_size_str)
                if tick_size <= 0:
                    return instrument, 0
            except Exception:
                return instrument, 0
            try:
                ticker = get_ticker(instrument)
                bid = Decimal(str(ticker.get("best_bid_price", "0")))
                ask = Decimal(str(ticker.get("best_ask_price", "0")))
                if bid <= 0 or ask <= 0:
                    return instrument, 0
                spread = ask - bid
                return instrument, int(spread / tick_size)
            except Exception:
                return instrument, 0

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(fetch_spread, m): m for m in self.app.markets}
            total = len(futures)
            done = 0
            for future in as_completed(futures):
                done += 1
                try:
                    instrument, spread_ticks = future.result()
                    results.append((instrument, spread_ticks))
                except Exception:
                    pass
                # Update progress
                if done % 5 == 0 or done == total:
                    self.after(0, lambda d=done, t=total: self._pick_progress_var.set(f"{d}/{t} inspected"))

        # Sort by spread descending
        results.sort(key=lambda x: x[1], reverse=True)

        # Build display names and mappings
        self._market_display_to_full.clear()
        self._market_spreads.clear()
        self._top_markets.clear()

        display_names = []
        for i, (instrument, spread) in enumerate(results):
            short = self._to_short_name(instrument)
            # Add spread indicator for top 5
            if i < 5 and spread >= MIN_SPREAD_TICKS:
                display = f"★ {short} ({spread})"
                self._top_markets.add(short)
            else:
                display = short
            display_names.append(display)
            self._market_display_to_full[display] = instrument
            self._market_display_to_full[short] = instrument
            self._market_spreads[instrument] = spread

        self.after(0, lambda: self._update_market_dropdown(display_names))

    def _update_market_dropdown(self, display_names: list[str]) -> None:
        """Update dropdown with sorted markets."""
        self.market_combo.configure(values=display_names)
        self._choose_btn.configure(state=tk.NORMAL)
        self._pick_progress_var.set("")  # Clear progress
        # Auto-select top market
        if display_names:
            self.market_var.set(display_names[0])
            self.app.rename_tab_for(self)
            self._update_min_hint()

    def _apply_mode_visibility(self) -> None:
        mode = self.mode_var.get()
        size_type = self.size_type_var.get()

        def hide(widget: tk.Widget) -> None:
            widget.grid_remove()

        def show(widget: tk.Widget) -> None:
            widget.grid()

        for w in (
            self.size_type_combo,
            self.size_label,
            self.size_entry,
            self.rounds_entry,
            self.delay_entry,
            self.margin_entry,
            self.hold_entry,
            self.direction_combo,
        ):
            show(w)

        self.notional_label.grid_remove()
        self.notional_entry.grid_remove()
        self.computed_size_label.grid_remove()
        self.computed_size_var.set("")

        for w in (
            self.close_method_label,
            self.close_method_frame,
            self.close_pct_label,
            self.close_pct_combo,
            self.close_size_label,
            self.close_size_entry,
        ):
            hide(w)

        if mode == "instant":
            show(self.delay_label)
            show(self.delay_entry)
            hide(self.margin_label)
            hide(self.margin_entry)
            hide(self.hold_label)
            hide(self.hold_entry)
        elif mode == "build_hold_close":
            hide(self.delay_label)
            hide(self.delay_entry)
            show(self.margin_label)
            show(self.margin_entry)
            show(self.hold_label)
            show(self.hold_entry)
        elif mode == "build_hold":
            hide(self.delay_label)
            hide(self.delay_entry)
            show(self.margin_label)
            show(self.margin_entry)
            hide(self.hold_label)
            hide(self.hold_entry)
        elif mode == "close_existing":
            # Show size type and size/notional (same as other modes)
            show(self.size_type_combo)
            show(self.rounds_entry)
            hide(self.delay_label)
            hide(self.delay_entry)
            hide(self.margin_label)
            hide(self.margin_entry)
            hide(self.hold_label)
            hide(self.hold_entry)
            hide(self.direction_combo)

        if mode in ("instant", "build_hold_close", "build_hold", "close_existing") and size_type == SIZE_TYPE_USD_NOTIONAL:
            hide(self.size_label)
            hide(self.size_entry)
            try:
                self.min_hint_label.grid_remove()
            except Exception:
                pass
            self.notional_label.grid(row=1, column=0, sticky=tk.W, pady=2)
            self.notional_entry.grid(row=1, column=1, sticky=tk.W, pady=2)
            self.computed_size_label.grid(row=1, column=2, sticky=tk.W, padx=8)
        else:
            try:
                self.min_hint_label.grid()
            except Exception:
                pass
        # Note: _update_min_hint is debounced and will be called via trace

    def _parse_run_config(self) -> RunConfig:
        market = self._selected_market_full()
        mode = self.mode_var.get()
        if not market:
            raise ValueError("Market is required")

        if mode not in ("instant", "build_hold_close", "build_hold", "close_existing"):
            raise ValueError("Invalid mode")

        # All modes now use size/notional and rounds
        _ = Decimal(self.notional_var.get()) if self.size_type_var.get() == SIZE_TYPE_USD_NOTIONAL else Decimal(self.size_var.get())
        rounds = int(self.rounds_var.get())
        if rounds <= 0:
            raise ValueError("Rounds must be > 0")

        delay = float(self.delay_var.get()) if mode == "instant" else 0.0
        max_margin = float(self.margin_var.get()) / 100 if mode in ("build_hold_close", "build_hold") else 0.0
        hold_minutes = int(self.hold_var.get()) if mode == "build_hold_close" else 0

        return RunConfig(
            market=market,
            mode=mode,
            rounds=rounds,
            delay_sec=delay,
            max_margin=max_margin,
            hold_minutes=hold_minutes,
            direction_policy=self._direction_display_to_policy.get(self.direction_var.get(), SIDE_POLICY_RANDOM),
        )

    def _resolve_size(self, inst_info: dict) -> tuple[Decimal, str]:
        if self.size_type_var.get() == SIZE_TYPE_USD_NOTIONAL:
            notional = Decimal(self.notional_var.get())
            market = self._selected_market_full()
            
            # Try WS ticker buffer first (faster, no network call)
            ticker = None
            if self._ticker_monitor and self._ticker_monitor.buffer:
                latest = self._ticker_monitor.buffer.get_latest()
                if latest is not None and getattr(latest, "bid", None) is not None and getattr(latest, "ask", None) is not None:
                    bid = Decimal(str(latest.bid))
                    ask = Decimal(str(latest.ask))
                    if bid > 0 and ask > 0:
                        ticker = {"best_bid_price": str(bid), "best_ask_price": str(ask)}
            
            # Fallback to REST if no cached data
            if not ticker:
                ticker = get_ticker(market)
            
            size, mid = compute_size_from_usd_notional(inst_info, ticker, notional)
            self._resolved_size_for_run = size
            self.after(0, lambda s=size: self.computed_size_var.set(f"Computed: {s}"))
            return size, f"Computed size from notional ${notional} @ mid={mid}: size={size}"

        size_in = Decimal(self.size_var.get())
        normalized = normalize_size(inst_info, size_in).size
        self._resolved_size_for_run = normalized
        return normalized, f"Normalized size: {normalized} (input {size_in})"

    def _choose_policy_for_run(self, mode: str, policy: str) -> str:
        """Resolve policy for the run. Random is locked to a fixed direction at run start."""
        if policy != SIDE_POLICY_RANDOM:
            return policy
        # Always lock Random to a fixed policy for the entire run
        if self._sticky_policy_for_run is None:
            self._sticky_policy_for_run = (
                SIDE_POLICY_ACCOUNT1_LONG if random.choice([True, False]) else SIDE_POLICY_ACCOUNT1_SHORT
            )
        return self._sticky_policy_for_run

    def _seed_random_policy_from_existing_positions(
        self,
        pair: AccountPair,
        cookie_primary: str,
        cookie_secondary: str,
        market: str,
    ) -> bool:
        """If both accounts already have a hedged position, keep building in that direction.

        This makes "Random" deterministic when resuming Build & Hold on an existing hedge:
        we pick the account that is already long as the long leg for subsequent OPENs.
        """
        try:
            p1 = get_position_size(pair.primary, cookie_primary, market)
            p2 = get_position_size(pair.secondary, cookie_secondary, market)
            if p1 is None or p2 is None:
                return False
            p1 = Decimal(str(p1))
            p2 = Decimal(str(p2))
            if p1 == 0 or p2 == 0:
                return False
            # Only seed if it's a proper hedge (opposite signs). If it's unhedged, don't guess.
            if p1 * p2 >= 0:
                return False

            self._sticky_policy_for_run = SIDE_POLICY_ACCOUNT1_LONG if p1 > 0 else SIDE_POLICY_ACCOUNT1_SHORT
            return True
        except Exception:
            return False

    def _run_thread(self) -> None:
        ticker_monitor: TickerMonitor | None = None
        order_ws: OrderStreamClient | None = None
        try:
            cfg = self._parse_run_config()
            pair = self.app.account_pair

            # Check if accounts are configured
            if not pair:
                raise RuntimeError("No accounts configured. Click 'Setup Account' to configure.")

            # Quick validation before slow cookie refresh
            valid1, err1 = validate_state_file_ext(pair.primary.browser_state_path, require_session_key=True)
            if not valid1:
                raise RuntimeError(f"Account 1: {err1}")
            valid2, err2 = validate_state_file_ext(pair.secondary.browser_state_path, require_session_key=True)
            if not valid2:
                raise RuntimeError(f"Account 2: {err2}")

            self._log("Refreshing cookies...")
            cookie_primary, cookie_secondary = self.app.cookie_manager.get_cookies_for_pair(
                pair,
                validate_primary=lambda c: ping_auth(pair.primary, c),
                validate_secondary=lambda c: ping_auth(pair.secondary, c),
            )
            if not cookie_primary or not cookie_secondary:
                raise RuntimeError("Failed to get cookies (use `python grvt_cookie_provider.py --login`)")

            self._log(self.app.account1_display())
            self._log(f"Account 2: {pair.secondary.name} (sub_account_id={pair.secondary.sub_account_id})")
            self._log(f"Market: {cfg.market}")
            self._log(f"Mode: {cfg.mode}")

            inst_info = get_instrument(cfg.market)

            # If user chose Random direction and there is already a hedged position on this market,
            # seed the "random" direction BEFORE we spin up WS/maker selection (which also resolves
            # and locks the random policy).
            if (
                cfg.direction_policy == SIDE_POLICY_RANDOM
                and self._sticky_policy_for_run is None
                and cfg.mode in ("build_hold", "build_hold_close")
            ):
                if self._seed_random_policy_from_existing_positions(pair, cookie_primary, cookie_secondary, cfg.market):
                    self._log("[DEBUG] Random direction seeded from existing hedged positions")

            # Start ticker monitor for price stability (needs 2s warmup)
            self._log("Starting price monitor...")
            ticker_monitor = TickerMonitor(cfg.market, on_error=self._log)
            ticker_monitor.start()
            self._ticker_monitor = ticker_monitor
            
            # Wait for buffer to collect sufficient data (2 seconds)
            warmup_start = time.time()
            while time.time() - warmup_start < 2.0:
                if not self._running.is_set():
                    break
                time.sleep(0.1)
            self._log("Price monitor ready")

            if cfg.mode in ("instant", "build_hold_close", "build_hold"):
                _, msg = self._resolve_size(inst_info)
                self._log(msg)

            # Start a persistent order WS stream for the maker account (reduces ws_wait latency).
            if cfg.mode in ("instant", "build_hold_close", "build_hold"):
                try:
                    policy_for_run = self._choose_policy_for_run(cfg.mode, cfg.direction_policy)
                    long_acc, _short_acc = choose_long_short_for_open(pair, policy_for_run)
                    maker_acc = long_acc
                    order_ws = OrderStreamClient(
                        cookie_getter=lambda: self.app.cookie_manager.get_cookie(
                            browser_state_path=maker_acc.browser_state_path
                        ),
                        main_account_id=maker_acc.main_account_id,
                        sub_account_id=maker_acc.sub_account_id,
                        instrument=cfg.market,
                        on_error=self._log,
                    )
                    order_ws.start()
                    self._order_ws = order_ws
                except Exception:
                    order_ws = None
                    self._order_ws = None

            if cfg.mode == "instant":
                self._run_instant(cfg, pair, cookie_primary, cookie_secondary, inst_info, ws_client=order_ws)
            elif cfg.mode == "build_hold_close":
                self._run_build_hold_close(cfg, pair, cookie_primary, cookie_secondary, inst_info, ws_client=order_ws)
            elif cfg.mode == "build_hold":
                self._run_build_hold(cfg, pair, cookie_primary, cookie_secondary, inst_info, ws_client=order_ws)
            else:
                self._run_close_existing(cfg, pair, cookie_primary, cookie_secondary, inst_info, ws_client=order_ws)

        except Exception as e:
            err_msg = str(e)
            self._log_error(f"ERROR: {err_msg}")
            self.after(0, lambda m=err_msg: messagebox.showerror("Error", m))
        finally:
            # Stop ticker monitor
            if ticker_monitor is not None:
                ticker_monitor.stop()
                self._ticker_monitor = None
            if order_ws is not None:
                try:
                    order_ws.stop()
                except Exception:
                    pass
                self._order_ws = None
            self._running.clear()
            self.after(0, lambda: (
                self.start_btn.configure(state=tk.NORMAL),
                self.stop_btn.configure(state=tk.DISABLED),
                self.direction_combo.configure(state="readonly"),
            ))
            self._log("--- DONE ---")

    def _cookies_for_accounts(
        self, pair: AccountPair, cookie_primary: str, cookie_secondary: str, long_acc, short_acc
    ) -> tuple[str, str]:
        cookie_long = cookie_primary if long_acc is pair.primary else cookie_secondary
        cookie_short = cookie_primary if short_acc is pair.primary else cookie_secondary
        return cookie_long, cookie_short

    def _verify_auth(self, pair: AccountPair, cookie_primary: str, cookie_secondary: str, market: str) -> str | None:
        """Verify auth by checking position API. Returns error message or None if OK."""
        pos1 = get_position_size(pair.primary, cookie_primary, market)
        if pos1 is None:
            return f"Session expired or auth failed for {pair.primary.name}. Re-login required."
        pos2 = get_position_size(pair.secondary, cookie_secondary, market)
        if pos2 is None:
            return f"Session expired or auth failed for {pair.secondary.name}. Re-login required."
        return None

    def _emergency_close(self, acc: AccountConfig, cookie: str, market: str, inst_info: dict) -> bool:
        """Attempt reduce-only market close for remaining position. Returns True if successful or no position."""
        pos = get_position_size(acc, cookie, market)
        if pos is None or pos == 0:
            return pos == 0  # True if no position, False if auth failed
        result = place_market_order(acc, cookie, market, inst_info, abs(pos), is_buying=(pos < 0), reduce_only=True)
        return result is not None and result.get("r") is not None

    def _run_instant(
        self,
        cfg: RunConfig,
        pair: AccountPair,
        cookie_primary: str,
        cookie_secondary: str,
        inst_info: dict,
        *,
        ws_client: OrderStreamClient | None = None,
    ) -> None:
        # Verify auth before starting
        auth_err = self._verify_auth(pair, cookie_primary, cookie_secondary, cfg.market)
        if auth_err:
            raise RuntimeError(auth_err)

        size = self._resolved_size_for_run or Decimal("0")
        total_volume = Decimal("0")
        self._log(f"Rounds: {cfg.rounds} | Delay: {cfg.delay_sec}s")

        # Resolve policy once at start (so Random is locked for entire run)
        policy_for_run = self._choose_policy_for_run(cfg.mode, cfg.direction_policy)
        self._log(f"Direction: {cfg.direction_policy} (resolved={policy_for_run})")
        long_acc, short_acc = choose_long_short_for_open(pair, policy_for_run)
        cookie_long, cookie_short = self._cookies_for_accounts(pair, cookie_primary, cookie_secondary, long_acc, short_acc)

        for i in range(cfg.rounds):
            if not self._running.is_set():
                break

            self._log(f"[Round {i+1}/{cfg.rounds}]")
            continued_after_warning = False
            open_had_external_fill_warning = False

            # Use WS ticker buffer for mid price (faster, no network call)
            mid = None
            if self._ticker_monitor and self._ticker_monitor.buffer:
                latest = self._ticker_monitor.buffer.get_latest()
                if latest is not None and getattr(latest, "bid", None) is not None and getattr(latest, "ask", None) is not None:
                    bid = Decimal(str(latest.bid))
                    ask = Decimal(str(latest.ask))
                    if bid > 0 and ask > 0:
                        mid = (bid + ask) / 2
            
            # Fallback to REST if no cached data
            if mid is None:
                ticker = get_ticker(cfg.market)
                mid = (Decimal(str(ticker["best_bid_price"])) + Decimal(str(ticker["best_ask_price"]))) / 2

            self._log(f"  OPEN {size} (long={long_acc.name}, short={short_acc.name})")
            success, err = place_order_pair_with_retry(
                long_acc,
                short_acc,
                cookie_long,
                cookie_short,
                cfg.market,
                inst_info,
                size,
                is_opening=True,
                max_retries=10,
                on_log=self._log,
                price_buffer=self._ticker_monitor.buffer if self._ticker_monitor else None,
                ws_client=ws_client,
            )
            if not success:
                self._log_error(f"  FAILED: {err}")
                break
            # OPEN contributes 2 legs of volume (one per account). Count it even if we later stop on warning.
            total_volume += 2 * size * mid
            # Check for external fill warning after OPEN
            if err and "EXTERNAL_FILL_WARNING" in err:
                open_had_external_fill_warning = True
                self._log(f"  ⚠ {err}")
                if not self._show_external_fill_warning(err):
                    self._log("User chose to stop after external fills detected")
                    break
                continued_after_warning = True

            time.sleep(cfg.delay_sec)

            # If OPEN had external fills and the strategy auto-recovered to flat (or a smaller hedge),
            # don't blindly attempt to CLOSE the original `size` (reduce-only will fail).
            close_size: Decimal | None = size
            if open_had_external_fill_warning and continued_after_warning:
                pos_long_now = get_position_size(long_acc, cookie_long, cfg.market)
                pos_short_now = get_position_size(short_acc, cookie_short, cfg.market)
                if pos_long_now is None or pos_short_now is None:
                    self._log_error("  ERROR: Failed to read positions after OPEN warning (auth issue?)")
                    break
                pos_long_now = Decimal(str(pos_long_now))
                pos_short_now = Decimal(str(pos_short_now))
                if pos_long_now == 0 and pos_short_now == 0:
                    close_size = None
                    self._log("  CLOSE skipped (already flat after OPEN warning)")
                else:
                    # Close only the hedgeable amount (prevents reduce-only errors).
                    close_size = min(abs(pos_long_now), abs(pos_short_now))
                    if close_size <= 0:
                        close_size = None
                        self._log("  CLOSE skipped (no hedgeable position after OPEN warning)")

            if close_size is not None:
                self._log(f"  CLOSE {close_size}")
                success, err = place_order_pair_with_retry(
                    long_acc,
                    short_acc,
                    cookie_long,
                    cookie_short,
                    cfg.market,
                    inst_info,
                    close_size,
                    is_opening=False,
                    max_retries=10,
                    on_log=self._log,
                    price_buffer=self._ticker_monitor.buffer if self._ticker_monitor else None,
                    ws_client=ws_client,
                )
                if not success:
                    self._log_error(f"  CLOSE FAILED: {err or 'unknown error'}")
                    self._log("  Attempting emergency close...")
                    ok1 = self._emergency_close(long_acc, cookie_long, cfg.market, inst_info)
                    ok2 = self._emergency_close(short_acc, cookie_short, cfg.market, inst_info)
                    if ok1 and ok2:
                        self._log("  Emergency close successful")
                    else:
                        self._log_error("  Emergency close failed - manual intervention required!")
                    break
                # CLOSE contributes another 2 legs of volume.
                total_volume += 2 * close_size * mid
                # Check for external fill warning after CLOSE
                if err and "EXTERNAL_FILL_WARNING" in err:
                    self._log(f"  ⚠ {err}")
                    if not self._show_external_fill_warning(err):
                        self._log("User chose to stop after external fills detected")
                        break
                    continued_after_warning = True

            # Safety: after a full OPEN+CLOSE round, both accounts should be flat.
            pos_long = get_position_size(long_acc, cookie_long, cfg.market)
            pos_short = get_position_size(short_acc, cookie_short, cfg.market)
            if pos_long is None or pos_short is None:
                self._log_error("  ERROR: Failed to verify positions after close (auth issue?)")
                break
            pos_long = Decimal(str(pos_long))
            pos_short = Decimal(str(pos_short))
            if pos_long != 0 or pos_short != 0:
                # If we just chose to continue after an external fill warning and the
                # residual positions are still perfectly hedged, do NOT force-close.
                if continued_after_warning and (pos_long + pos_short == 0):
                    self._log(
                        f"  Residual hedge on {cfg.market} (continuing): {long_acc.name}={pos_long}, {short_acc.name}={pos_short}"
                    )
                else:
                    self._log_error(
                        f"  ⚠ Residual position on {cfg.market} after round: {long_acc.name}={pos_long}, {short_acc.name}={pos_short}"
                    )
                    self._log("  Attempting emergency close...")
                    ok1 = self._emergency_close(long_acc, cookie_long, cfg.market, inst_info)
                    ok2 = self._emergency_close(short_acc, cookie_short, cfg.market, inst_info)
                    if ok1 and ok2:
                        self._log("  Emergency close successful")
                    else:
                        self._log_error("  Emergency close failed - manual intervention required!")
                    break

            if (i + 1) % 10 == 0:
                self._log(f"  >> Accumulated: ${total_volume:,.2f}")

            if i < cfg.rounds - 1 and self._running.is_set():
                time.sleep(random.uniform(1, 2))

        self._log(f"Total USD volume: ${total_volume:,.2f}")

    def _run_build_hold_close(
        self,
        cfg: RunConfig,
        pair: AccountPair,
        cookie_primary: str,
        cookie_secondary: str,
        inst_info: dict,
        *,
        ws_client: OrderStreamClient | None = None,
    ) -> None:
        # Verify auth before starting
        auth_err = self._verify_auth(pair, cookie_primary, cookie_secondary, cfg.market)
        if auth_err:
            raise RuntimeError(auth_err)

        size = self._resolved_size_for_run or Decimal("0")
        if cfg.direction_policy == SIDE_POLICY_RANDOM and self._sticky_policy_for_run is None:
            if self._seed_random_policy_from_existing_positions(pair, cookie_primary, cookie_secondary, cfg.market):
                self._log("[DEBUG] Random direction seeded from existing hedged positions")
        policy_for_run = self._choose_policy_for_run(cfg.mode, cfg.direction_policy)
        self._log(f"Direction policy: {cfg.direction_policy} (resolved={policy_for_run})")

        long_acc, short_acc = choose_long_short_for_open(pair, policy_for_run)
        cookie_long, cookie_short = self._cookies_for_accounts(pair, cookie_primary, cookie_secondary, long_acc, short_acc)

        opened_rounds = 0
        self._log("=== PHASE 1: BUILDING POSITIONS ===")
        for i in range(cfg.rounds):
            if not self._running.is_set():
                self._log("Stop requested, stopping build-up...")
                break

            margin_a = get_margin_ratio(long_acc, cookie_long)
            margin_b = get_margin_ratio(short_acc, cookie_short)
            if margin_a is None or margin_b is None:
                self._log_error(f"[Round {i+1}] ERROR: Failed to get margin, stopping")
                break
            self._log(f"[Round {i+1}] Margin: long={margin_a:.1%}, short={margin_b:.1%}")

            if margin_a > cfg.max_margin or margin_b > cfg.max_margin:
                limit_pct = cfg.max_margin * 100.0
                self._log(
                    f"  Max margin reached (limit={limit_pct:.2f}%): long={margin_a*100.0:.2f}%, short={margin_b*100.0:.2f}%"
                )
                break

            self._log(f"  OPEN {size} {cfg.market}...")
            success, err = place_order_pair_with_retry(
                long_acc,
                short_acc,
                cookie_long,
                cookie_short,
                cfg.market,
                inst_info,
                size,
                is_opening=True,
                max_retries=10,
                on_log=self._log,
                price_buffer=self._ticker_monitor.buffer if self._ticker_monitor else None,
                ws_client=ws_client,
            )
            if not success:
                self._log_error(f"  FAILED: {err}")
                break

            if err and "EXTERNAL_FILL_WARNING" in err:
                self._log(f"  ⚠ {err}")
                if not self._show_external_fill_warning(err):
                    self._log("User chose to stop after external fills detected")
                    break

            opened_rounds += 1
            self._log("  OK")
            time.sleep(1)

        if opened_rounds == 0:
            self._log("No positions opened")
            return

        self._log(f"\n=== PHASE 2: HOLDING ({cfg.hold_minutes} min) ===")
        self._log(f"Opened {opened_rounds} rounds, total size: {size * opened_rounds}")

        hold_seconds = cfg.hold_minutes * 60
        for remaining in range(hold_seconds, 0, -1):
            if not self._running.is_set():
                self._log("Stop requested during hold, proceeding to close...")
                break
            if remaining % 60 == 0:
                self._log(f"  Closing in {remaining // 60} min...")
            time.sleep(1)

        self._log("\n=== PHASE 3: CLOSING POSITIONS ===")
        # Refresh cookies before close phase (may have expired during hold)
        self._log("Refreshing cookies...")
        cookie_primary, cookie_secondary = self.app.cookie_manager.get_cookies_for_pair(
            pair,
            validate_primary=lambda c: ping_auth(pair.primary, c),
            validate_secondary=lambda c: ping_auth(pair.secondary, c),
        )
        if not cookie_primary or not cookie_secondary:
            self._log_error("ERROR: Failed to refresh cookies for close phase. Re-login required.")
            return
        cookie_long, cookie_short = self._cookies_for_accounts(pair, cookie_primary, cookie_secondary, long_acc, short_acc)

        closed_iters = 0
        while True:
            if not self._running.is_set():
                # User may have hit Stop; still continue closing until flat.
                self._log("Stop requested, continuing close...")

            pos_long = get_position_size(long_acc, cookie_long, cfg.market)
            pos_short = get_position_size(short_acc, cookie_short, cfg.market)
            if pos_long is None or pos_short is None:
                self._log_error("ERROR: Failed to read positions during close phase. Re-login required.")
                break
            pos_long = Decimal(str(pos_long))
            pos_short = Decimal(str(pos_short))

            if pos_long == 0 and pos_short == 0:
                self._log("All positions closed")
                break
            if pos_long * pos_short >= 0:
                self._log_error(f"Positions not hedging during close on {cfg.market}! long={pos_long}, short={pos_short}")
                self._log("Attempting emergency close...")
                ok1 = self._emergency_close(long_acc, cookie_long, cfg.market, inst_info)
                ok2 = self._emergency_close(short_acc, cookie_short, cfg.market, inst_info)
                if ok1 and ok2:
                    self._log("Emergency close successful")
                else:
                    self._log_error("Emergency close failed - manual intervention required!")
                break

            close_size = min(size, abs(pos_long), abs(pos_short))
            closed_iters += 1
            self._log(f"[Close {closed_iters}] CLOSE {close_size}...")
            success, err = place_order_pair_with_retry(
                long_acc,
                short_acc,
                cookie_long,
                cookie_short,
                cfg.market,
                inst_info,
                close_size,
                is_opening=False,
                max_retries=10,
                on_log=self._log,
                price_buffer=self._ticker_monitor.buffer if self._ticker_monitor else None,
                ws_client=ws_client,
            )
            if not success:
                self._log_error(f"  CLOSE FAILED: {err}")
                self._log("  Attempting emergency close...")
                ok1 = self._emergency_close(long_acc, cookie_long, cfg.market, inst_info)
                ok2 = self._emergency_close(short_acc, cookie_short, cfg.market, inst_info)
                if ok1 and ok2:
                    self._log("  Emergency close successful")
                else:
                    self._log_error("  Emergency close failed - manual intervention required!")
                break
            if err and "EXTERNAL_FILL_WARNING" in err:
                self._log(f"  ⚠ {err}")
                if not self._show_external_fill_warning(err):
                    self._log("User chose to stop after external fills detected")
                    # Ensure we finish flat before exiting.
                    ok1 = self._emergency_close(long_acc, cookie_long, cfg.market, inst_info)
                    ok2 = self._emergency_close(short_acc, cookie_short, cfg.market, inst_info)
                    if ok1 and ok2:
                        self._log("  Emergency close successful")
                    else:
                        self._log_error("  Emergency close failed - manual intervention required!")
                    break

            self._log("  OK")
            time.sleep(1)

            # Guard against pathological loops (should never happen in normal conditions).
            if closed_iters > opened_rounds + 20:
                self._log_error("Close phase exceeded expected iterations; using emergency close...")
                ok1 = self._emergency_close(long_acc, cookie_long, cfg.market, inst_info)
                ok2 = self._emergency_close(short_acc, cookie_short, cfg.market, inst_info)
                if ok1 and ok2:
                    self._log("Emergency close successful")
                else:
                    self._log_error("Emergency close failed - manual intervention required!")
                break

    def _run_build_hold(
        self,
        cfg: RunConfig,
        pair: AccountPair,
        cookie_primary: str,
        cookie_secondary: str,
        inst_info: dict,
        *,
        ws_client: OrderStreamClient | None = None,
    ) -> None:
        # Verify auth before starting
        auth_err = self._verify_auth(pair, cookie_primary, cookie_secondary, cfg.market)
        if auth_err:
            raise RuntimeError(auth_err)

        size = self._resolved_size_for_run or Decimal("0")
        if cfg.direction_policy == SIDE_POLICY_RANDOM and self._sticky_policy_for_run is None:
            if self._seed_random_policy_from_existing_positions(pair, cookie_primary, cookie_secondary, cfg.market):
                self._log("[DEBUG] Random direction seeded from existing hedged positions")
        policy_for_run = self._choose_policy_for_run(cfg.mode, cfg.direction_policy)
        self._log(f"Direction policy: {cfg.direction_policy} (resolved={policy_for_run})")

        long_acc, short_acc = choose_long_short_for_open(pair, policy_for_run)
        cookie_long, cookie_short = self._cookies_for_accounts(pair, cookie_primary, cookie_secondary, long_acc, short_acc)

        opened_rounds = 0
        self._log("=== BUILDING POSITIONS ===")
        for i in range(cfg.rounds):
            if not self._running.is_set():
                self._log("Stop requested, stopping build-up...")
                break

            margin_a = get_margin_ratio(long_acc, cookie_long)
            margin_b = get_margin_ratio(short_acc, cookie_short)
            if margin_a is None or margin_b is None:
                self._log_error(f"[Round {i+1}] ERROR: Failed to get margin, stopping")
                break
            self._log(f"[Round {i+1}] Margin: long={margin_a:.1%}, short={margin_b:.1%}")

            if margin_a > cfg.max_margin or margin_b > cfg.max_margin:
                limit_pct = cfg.max_margin * 100.0
                self._log(
                    f"  Max margin reached (limit={limit_pct:.2f}%): long={margin_a*100.0:.2f}%, short={margin_b*100.0:.2f}%"
                )
                break

            self._log(f"  OPEN {size} {cfg.market}...")
            success, err = place_order_pair_with_retry(
                long_acc,
                short_acc,
                cookie_long,
                cookie_short,
                cfg.market,
                inst_info,
                size,
                is_opening=True,
                max_retries=10,
                on_log=self._log,
                price_buffer=self._ticker_monitor.buffer if self._ticker_monitor else None,
                ws_client=ws_client,
            )
            if not success:
                self._log_error(f"  FAILED: {err}")
                break

            if err and "EXTERNAL_FILL_WARNING" in err:
                self._log(f"  ⚠ {err}")
                if not self._show_external_fill_warning(err):
                    self._log("User chose to stop after external fills detected")
                    break

            opened_rounds += 1
            self._log("  OK")
            time.sleep(1)

        self._log(f"\nBuild complete. Opened {opened_rounds} rounds.")
        self._log("Use 'Close Existing' mode to close positions when ready.")

    def _run_close_existing(
        self,
        cfg: RunConfig,
        pair: AccountPair,
        cookie_primary: str,
        cookie_secondary: str,
        inst_info: dict,
        *,
        ws_client: OrderStreamClient | None = None,
    ) -> None:
        # Check auth by getting positions (None = auth failed)
        pos1 = get_position_size(pair.primary, cookie_primary, cfg.market)
        if pos1 is None:
            raise RuntimeError(f"Session expired or auth failed for {pair.primary.name}. Re-login required.")
        pos2 = get_position_size(pair.secondary, cookie_secondary, cfg.market)
        if pos2 is None:
            raise RuntimeError(f"Session expired or auth failed for {pair.secondary.name}. Re-login required.")

        self._log("Current positions:")
        self._log(f"  Account 1: {pos1}")
        self._log(f"  Account 2: {pos2}")

        if pos1 == 0 and pos2 == 0:
            self._log_error("No positions to close")
            return

        if pos1 * pos2 >= 0:
            raise RuntimeError(f"Positions not hedging! Account1={pos1}, Account2={pos2}")

        # Determine who is currently long/short on this market.
        long_acc = pair.primary if Decimal(str(pos1)) > 0 else pair.secondary
        short_acc = pair.secondary if long_acc is pair.primary else pair.primary
        cookie_long, cookie_short = self._cookies_for_accounts(pair, cookie_primary, cookie_secondary, long_acc, short_acc)

        # Resolve size per round (same as other modes)
        size_per_round, size_msg = self._resolve_size(inst_info)
        self._log(size_msg)

        # Calculate how many rounds needed
        total_rounds = cfg.rounds
        self._log(f"Closing {size_per_round} per round, {total_rounds} rounds requested")

        closed = Decimal("0")
        for i in range(total_rounds):
            if not self._running.is_set():
                self._log("Stop requested, stopping close...")
                break

            # Re-read live positions each round: they can change due to fills, lag, or warnings/recovery.
            pos_long_now = get_position_size(long_acc, cookie_long, cfg.market)
            pos_short_now = get_position_size(short_acc, cookie_short, cfg.market)
            if pos_long_now is None or pos_short_now is None:
                self._log_error("  ERROR: Failed to read positions while closing (auth issue?)")
                break
            pos_long_now = Decimal(str(pos_long_now))
            pos_short_now = Decimal(str(pos_short_now))

            if pos_long_now == 0 and pos_short_now == 0:
                self._log("All positions closed")
                break

            # Close the hedgeable amount via self-match (maker+IOC). If one side is already flat
            # (unhedged residual), STOP and prompt for manual intervention (never market-close).
            hedgeable = min(abs(pos_long_now), abs(pos_short_now))
            if hedgeable <= 0:
                msg = (
                    f"Close Existing stopped: residual unhedged position on {cfg.market}.\n\n"
                    f"{long_acc.name}={pos_long_now}\n{short_acc.name}={pos_short_now}\n\n"
                    "Manual intervention required (no market close was sent)."
                )
                self._log_error(msg.replace("\n", " "))
                self._show_stop_required("Manual Intervention Required", msg)
                break

            close_size = min(size_per_round, hedgeable)
            self._log(f"[Round {i+1}/{total_rounds}] CLOSE {close_size} (self-match)")

            success, err = place_order_pair_with_retry(
                long_acc,
                short_acc,
                cookie_long,
                cookie_short,
                cfg.market,
                inst_info,
                close_size,
                is_opening=False,
                max_retries=10,
                on_log=self._log,
                price_buffer=self._ticker_monitor.buffer if self._ticker_monitor else None,
                ws_client=ws_client,
            )
            if not success:
                msg = (
                    f"Close Existing stopped: self-match CLOSE failed on {cfg.market}.\n\n"
                    f"Error: {err or 'unknown error'}\n\n"
                    "Manual intervention required (no market close was sent)."
                )
                self._log_error(f"  CLOSE FAILED: {err or 'unknown error'}")
                self._show_stop_required("Manual Intervention Required", msg)
                break

            if err and "EXTERNAL_FILL_WARNING" in err:
                self._log(f"  ⚠ {err}")
                if not self._show_external_fill_warning(err):
                    self._log("User chose to stop after external fills detected")
                    break

            closed += close_size
            self._log(f"  OK (closed ~{closed})")
            time.sleep(1)

        # Final verification (best-effort): positions should be flat after closing.
        p1_final = get_position_size(pair.primary, cookie_primary, cfg.market)
        p2_final = get_position_size(pair.secondary, cookie_secondary, cfg.market)
        if p1_final is None or p2_final is None:
            self._log_error("Close complete, but failed to verify final positions (auth issue?)")
        else:
            p1_final = Decimal(str(p1_final))
            p2_final = Decimal(str(p2_final))
            if p1_final != 0 or p2_final != 0:
                self._log_error(
                    f"Residual position remains on {cfg.market}: {pair.primary.name}={p1_final}, {pair.secondary.name}={p2_final}"
                )
            else:
                self._log("Positions flat")

        self._log("Close complete")

    # --- logging ---
    def _log(self, msg: str) -> None:
        if not self._show_debug_var.get():
            s = (msg or "").lstrip()
            if s.startswith("[DEBUG]"):
                return
        self._queue.put(("normal", _tr_log_line(msg)))

    def _log_error(self, msg: str) -> None:
        self._queue.put(("error", _tr_log_line(msg)))

    def _drain_queue(self) -> None:
        MAX_LOG_LINES = 3000
        while not self._queue.empty():
            kind, msg = self._queue.get()
            if kind == "error":
                self.status_text.insert(tk.END, msg + "\n", "error")
            else:
                self.status_text.insert(tk.END, msg + "\n")
            self.status_text.see(tk.END)
        
        # Trim to last MAX_LOG_LINES to prevent UI slowdown
        try:
            line_count = int(self.status_text.index('end-1c').split('.')[0])
            if line_count > MAX_LOG_LINES:
                self.status_text.delete('1.0', f'{line_count - MAX_LOG_LINES}.0')
        except Exception:
            pass
        
        self._drain_job = self.after(100, self._drain_queue)


class VolumeBoostGUI:
    def __init__(self):
        print("[DEBUG] Creating Tk root...", flush=True)
        self.root = tk.Tk()
        env = (os.getenv("GRVT_ENV", "prod") or "prod").strip().lower()
        self.root.title(_("app.title.testnet") if env == "testnet" else _("app.title"))
        # Use DPI-aware sizing on Windows so controls aren't clipped at high display scaling.
        _set_scaled_geometry(self.root, 720, 720, max_w_frac=0.95, max_h_frac=0.9)
        self._panels: list[MarketRunPanel] = []
        self._setup_window: SetupWindow | None = None
        self._about_window: tk.Toplevel | None = None
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        # Tk is not thread-safe; background workers must communicate via this queue.
        self._ui_queue: queue.Queue[Callable[[], None]] = queue.Queue()
        self._ui_queue_job: str | None = self.root.after(100, self._drain_ui_queue)

        print("[DEBUG] Loading accounts...", flush=True)
        self.account_pair: AccountPair | None = None
        self.cookie_manager = CookieManager()
        self.markets: list[str] = ["CRV_USDT_Perp"]
        self.market_meta: dict[str, dict[str, str]] = {}
        self._monitor_window: PositionMonitorWindow | None = None
        self._config_errors: list[str] = []
        self._monitor_btn_job: str | None = None
        self._monitor_btn_refresh_inflight = threading.Event()
        self.reload_accounts()

        self._build_ui()
        # Ensure the window is not smaller than the layout requires (prevents "missing" toolbar buttons).
        try:
            self.root.update_idletasks()
            req_w = int(self.root.winfo_reqwidth())
            req_h = int(self.root.winfo_reqheight())
            sw = int(self.root.winfo_screenwidth())
            sh = int(self.root.winfo_screenheight())
            min_w = min(req_w, int(sw * 0.98)) if sw > 0 else req_w
            min_h = min(req_h, int(sh * 0.98)) if sh > 0 else req_h
            self.root.minsize(min_w, min_h)
        except Exception:
            pass
        self._load_markets_async()
        self._schedule_market_refresh()  # Refresh market catalog every 2 minutes
        self._preload_cookies_async()  # Preload cookies in background
        self._schedule_monitor_btn_refresh()  # Keep Monitor ⚠ state fresh (manual trades can change it)
        self.add_tab()

    def _drain_ui_queue(self) -> None:
        if not self.root.winfo_exists():
            return
        try:
            while True:
                fn = self._ui_queue.get_nowait()
                try:
                    fn()
                except Exception:
                    pass
        except queue.Empty:
            pass
        self._ui_queue_job = self.root.after(100, self._drain_ui_queue)

    def _schedule_market_refresh(self) -> None:
        """Schedule periodic market catalog refresh every 2 minutes."""
        def refresh():
            self._load_markets_async()
            self.root.after(120_000, refresh)  # 2 minutes = 120,000 ms
        self.root.after(120_000, refresh)  # First refresh after 2 minutes

    def _preload_cookies_async(self) -> None:
        """Preload cookies, positions, and prices in background to speed up monitor."""
        def preload():
            if not self.account_pair:
                return
            try:
                # Fetch cookies to warm the cache
                c1, c2 = self.cookie_manager.get_cookies_for_pair(self.account_pair)
                print("[Preload] Cookies cached")
                
                # Also preload position data and prices
                if c1 and c2:
                    try:
                        pos1 = get_all_positions_map(self.account_pair.primary, c1)
                        pos2 = get_all_positions_map(self.account_pair.secondary, c2)
                        
                        # Fetch prices for all markets with positions
                        prices = {}
                        all_markets = set((pos1 or {}).keys()) | set((pos2 or {}).keys())
                        for market in all_markets:
                            try:
                                ticker = get_ticker(market)
                                mid = (Decimal(ticker["best_bid_price"]) + Decimal(ticker["best_ask_price"])) / 2
                                prices[market] = mid
                            except Exception:
                                prices[market] = Decimal("0")
                        
                        # Cache the results for Monitor to use
                        self._cached_positions = (pos1, pos2, prices, time.time())
                        print("[Preload] Positions + prices cached")
                        
                        # Check for unhedged positions and update UI
                        unhedged = self._check_unhedged(pos1 or {}, pos2 or {})
                        try:
                            self._ui_queue.put(lambda u=unhedged: self._update_monitor_btn(u))
                        except Exception:
                            pass
                    except Exception as e:
                        print(f"[Preload] Position preload failed: {e}")
            except Exception as e:
                print(f"[Preload] Cookie preload failed: {e}")

        threading.Thread(target=preload, daemon=True).start()

    def _schedule_monitor_btn_refresh(self) -> None:
        """Periodically recompute the Monitor warning state.

        The Monitor button warning can become stale if the user manually trades/clears positions.
        Refresh in the background so the UI reflects latest hedge state.
        """
        if self._monitor_btn_job:
            try:
                self.root.after_cancel(self._monitor_btn_job)
            except Exception:
                pass
            self._monitor_btn_job = None

        def tick():
            if not self.root.winfo_exists():
                return
            self._refresh_monitor_btn_async()
            self._monitor_btn_job = self.root.after(5_000, tick)  # every 5s

        self._monitor_btn_job = self.root.after(3_000, tick)

    def _refresh_monitor_btn_async(self) -> None:
        if self._monitor_btn_refresh_inflight.is_set():
            return
        self._monitor_btn_refresh_inflight.set()

        def work():
            try:
                if not self.account_pair:
                    try:
                        self._ui_queue.put(lambda: self._update_monitor_btn(False))
                    except Exception:
                        pass
                    return

                c1 = self.cookie_manager.get_cookie(browser_state_path=self.account_pair.primary.browser_state_path)
                c2 = self.cookie_manager.get_cookie(browser_state_path=self.account_pair.secondary.browser_state_path)
                if not c1 or not c2:
                    # If we can't check, avoid a sticky warning (confusing UX).
                    try:
                        self._ui_queue.put(lambda: self._update_monitor_btn(False))
                    except Exception:
                        pass
                    return

                pos1 = get_all_positions_map(self.account_pair.primary, c1) or {}
                pos2 = get_all_positions_map(self.account_pair.secondary, c2) or {}
                unhedged = self._check_unhedged(pos1, pos2)
                try:
                    self._ui_queue.put(lambda u=unhedged: self._update_monitor_btn(u))
                except Exception:
                    pass
            finally:
                self._monitor_btn_refresh_inflight.clear()

        threading.Thread(target=work, daemon=True).start()

    def _check_unhedged(self, pos1: dict, pos2: dict) -> bool:
        """Return True if any position is unhedged (not perfectly offset)."""
        all_markets = set(pos1.keys()) | set(pos2.keys())
        for market in all_markets:
            p1 = pos1.get(market, Decimal("0"))
            p2 = pos2.get(market, Decimal("0"))
            # Unhedged if: same side, or different sizes
            if p1 + p2 != 0:
                return True
        return False

    def _update_monitor_btn(self, unhedged: bool) -> None:
        """Update Monitor button styling based on hedge status."""
        if unhedged:
            self._monitor_btn.configure(text=_("btn.monitor_warn"), bg="#FFD700", fg="black")
        else:
            self._monitor_btn.configure(text=_("btn.monitor"), bg="SystemButtonFace", fg="black")

    def reload_accounts(self) -> None:
        """Reload accounts from session files (called after QR login)."""
        accounts, errors = get_all_accounts()
        self._config_errors = errors

        # Preserve the user's "Account 1/2" mapping (session file #1/#2).
        self.account_pair = AccountPair(primary=accounts[0], secondary=accounts[1]) if len(accounts) >= 2 else None

        # Update status indicator if UI exists
        try:
            if hasattr(self, "_status_label"):
                self._update_account_status()
        except Exception:
            pass

        # Update monitor window if open.
        try:
            if self._monitor_window is not None and self._monitor_window.winfo_exists():
                if self.account_pair is None:
                    # Monitor assumes a valid pair; close it if accounts become unavailable.
                    try:
                        self._monitor_window.destroy()
                    finally:
                        self._monitor_window = None
                else:
                    self._monitor_window.account_pair = self.account_pair
                    self._monitor_window._refresh()  # best-effort immediate refresh
        except Exception:
            pass

    def account1_display(self) -> str:
        if not self.account_pair:
            return _("account.no_config")
        p = self.account_pair.primary
        return _("account.display1", name=p.name, sub_account_id=p.sub_account_id)

    def _build_ui(self) -> None:
        top = ttk.Frame(self.root, padding=10)
        top.pack(fill=tk.X)

        # At high Windows display scaling, a single-row toolbar can push buttons off-screen.
        # Use a two-row layout so all controls remain visible without resizing the window.
        row1 = ttk.Frame(top)
        row1.pack(fill=tk.X)
        row2 = ttk.Frame(top)
        row2.pack(fill=tk.X, pady=(_px(self.root, 6), 0))

        ttk.Button(row1, text=_("btn.add_market"), command=self.add_tab).pack(side=tk.LEFT)
        ttk.Button(row1, text=_("btn.remove_current"), command=self.remove_current_tab).pack(side=tk.LEFT, padx=8)

        # Environment toggle (Prod/Testnet). We restart the app to apply it safely since
        # many modules read endpoints at import time.
        cur_env = (os.getenv("GRVT_ENV", "prod") or "prod").strip().lower()
        self._env_var = tk.StringVar(value=_("env.testnet") if cur_env == "testnet" else _("env.prod"))
        ttk.Label(row1, text=_("label.env")).pack(side=tk.LEFT, padx=(10, 2))
        self._env_combo = ttk.Combobox(
            row1,
            textvariable=self._env_var,
            values=[_("env.prod"), _("env.testnet")],
            state="readonly",
            width=8,
        )
        self._env_combo.pack(side=tk.LEFT)
        self._env_combo.bind("<<ComboboxSelected>>", self._on_env_change)

        # Language toggle (English/Chinese). Restart for consistency.
        cur_lang = _get_lang()
        lang_btn_text = _("btn.lang_to_zh") if cur_lang == "en" else _("btn.lang_to_en")
        self._lang_btn = ttk.Button(row1, text=lang_btn_text, command=self._toggle_lang)
        self._lang_btn.pack(side=tk.LEFT, padx=(8, 0))
         
        # Monitor button with dynamic styling for unhedged position warning
        self._monitor_btn = tk.Button(row2, text=_("btn.monitor"), command=self._open_monitor)
        self._monitor_btn.pack(side=tk.LEFT)
         
        # Setup button with dynamic styling based on account status
        self._setup_btn = tk.Button(row2, text=_("btn.setup_account"), command=self._open_setup)
        self._setup_btn.pack(side=tk.LEFT, padx=8)
        
        # Account status indicator
        self._status_label = tk.Label(row2, text="●", font=("Arial", _px(self.root, 14)))
        self._status_label.pack(side=tk.LEFT, padx=4)
        self._update_account_status()
        
        # Right-side controls.
        right = ttk.Frame(row2)
        right.pack(side=tk.RIGHT)
        self._about_btn = tk.Button(right, text=_("btn.about"), command=self._open_about)
        self._about_btn.pack(side=tk.LEFT, padx=4)
        # Stop All button (stops all running tabs)
        self._stop_all_btn = tk.Button(right, text=_("btn.stop_all"), command=self._stop_all, bg="#FF6B6B", fg="white")
        self._stop_all_btn.pack(side=tk.LEFT, padx=4)

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

    def _restart_self(self) -> None:
        """Restart via `-m grvt_volume_boost.gui_multi_market` so imports resolve from repo root.

        When this file is launched via `python -m ...`, a naive exec of `sys.argv` will relaunch
        the file path (e.g. `grvt_volume_boost/gui_multi_market.py`), which breaks top-level
        imports like `import grvt_config`. Always restart as a module from the repo root.
        """
        try:
            from pathlib import Path

            repo_root = Path(__file__).resolve().parents[1]
            os.chdir(str(repo_root))
        except Exception:
            pass

        os.execv(sys.executable, [sys.executable, "-m", "grvt_volume_boost.gui_multi_market"])

    def _on_env_change(self, _e=None) -> None:
        selected = (self._env_var.get() or "").strip().lower()
        new_env = "testnet" if selected in ("testnet", "测试网") else "prod"
        cur_env = (os.getenv("GRVT_ENV", "prod") or "prod").strip().lower()
        if new_env == cur_env:
            return

        # Stop runs on env switch to avoid cross-env state confusion.
        any_running = any(p._running.is_set() for p in self._panels)
        if any_running:
            ok = messagebox.askyesno(_("dlg.switch_env.title"), _("dlg.switch_env.body"))
            if not ok:
                # Revert selection
                self._env_var.set(_("env.testnet") if cur_env == "testnet" else _("env.prod"))
                return
            self._stop_all()

        _save_gui_env(new_env)
        os.environ["GRVT_ENV"] = new_env

        # Restart the current python process (ensures endpoints/session dir are reloaded cleanly).
        try:
            self._restart_self()
        except Exception as ex:
            messagebox.showerror(_("dlg.restart_failed.title"), _("dlg.restart_failed.body", err=f"{type(ex).__name__}: {ex}"))
            # Best-effort: revert selection
            self._env_var.set(_("env.testnet") if cur_env == "testnet" else _("env.prod"))

    def _toggle_lang(self) -> None:
        new_lang = "zh" if _get_lang() == "en" else "en"

        any_running = any(p._running.is_set() for p in self._panels)
        if any_running:
            ok = messagebox.askyesno(_("dlg.switch_lang.title"), _("dlg.switch_lang.body"))
            if not ok:
                return
            self._stop_all()

        _save_gui_lang(new_lang)
        os.environ["GRVT_LANG"] = new_lang

        try:
            self._restart_self()
        except Exception as ex:
            messagebox.showerror(_("dlg.restart_failed.title"), _("dlg.restart_failed.body", err=f"{type(ex).__name__}: {ex}"))

    def _update_account_status(self) -> None:
        """Update account status indicator and Setup button styling."""
        if self.account_pair:
            # Both accounts loaded - green
            self._status_label.configure(fg="green")
            self._setup_btn.configure(bg="SystemButtonFace", fg="black")
        else:
            # Accounts not configured - yellow warning
            self._status_label.configure(fg="orange")
            self._setup_btn.configure(bg="#FFD700", fg="black")

    def _stop_all(self) -> None:
        """Stop all running market panels."""
        stopped = 0
        for panel in self._panels:
            if panel._running.is_set():
                panel.stop()
                stopped += 1
        if stopped > 0:
            messagebox.showinfo(_("dlg.stop_all.title"), _("dlg.stop_all.stopped", n=stopped))
        else:
            messagebox.showinfo(_("dlg.stop_all.title"), _("dlg.stop_all.none"))

    def _open_setup(self) -> None:
        """Open setup window (singleton)."""
        if self._setup_window is None or not self._setup_window.winfo_exists():
            self._setup_window = SetupWindow(self.root, app=self)
        else:
            self._setup_window.lift()
            self._setup_window.focus_force()

    def _open_monitor(self) -> None:
        """Open position monitor window (singleton)."""
        if not self.account_pair:
            messagebox.showwarning(_("dlg.not_configured.title"), _("dlg.not_configured.body"))
            return
        if self._monitor_window is None or not self._monitor_window.winfo_exists():
            # Pass cached positions for instant display
            cached = getattr(self, "_cached_positions", None)
            self._monitor_window = PositionMonitorWindow(self.root, self.account_pair, self.cookie_manager, cached_positions=cached)
        else:
            self._monitor_window.lift()
            self._monitor_window.focus_force()

    def _open_about(self) -> None:
        if self._about_window is not None and self._about_window.winfo_exists():
            self._about_window.lift()
            self._about_window.focus_force()
            return

        w = tk.Toplevel(self.root)
        self._about_window = w
        w.title(_("about.title"))
        _set_scaled_geometry(w, 520, 180)
        w.resizable(False, False)

        outer = ttk.Frame(w, padding=12)
        outer.pack(fill=tk.BOTH, expand=True)

        author = "撸毛小狗"
        twitter_url = "https://x.com/LumaoDoggie"
        ref_url = "https://grvt.io/?ref=lumaoDoggie"

        ttk.Label(outer, text=f"{_('about.author')} {author}").grid(row=0, column=0, sticky="w", pady=(0, 8))

        def link_row(row: int, label: str, url: str) -> None:
            ttk.Label(outer, text=label).grid(row=row, column=0, sticky="w")
            link = tk.Label(outer, text=url, fg="#1a73e8", cursor="hand2")
            try:
                link.configure(font=("TkDefaultFont", 10, "underline"))
            except Exception:
                pass
            link.grid(row=row, column=1, sticky="w", padx=(6, 0))
            link.bind("<Button-1>", lambda _e: webbrowser.open_new_tab(url))

        link_row(1, _("about.twitter"), twitter_url)
        link_row(2, _("about.referral"), ref_url)

        def on_close():
            try:
                w.destroy()
            finally:
                self._about_window = None

        ttk.Button(outer, text=_("reco.close"), command=on_close).grid(row=3, column=0, sticky="e", pady=(12, 0))
        w.protocol("WM_DELETE_WINDOW", on_close)


    def _load_markets_async(self) -> None:
        def fetch():
            markets, meta = get_market_catalog()
            try:
                self.root.after(0, lambda: self._set_market_catalog(markets, meta))
            except RuntimeError:
                # Fallback: set directly (may cause UI glitch but won't crash)
                self._set_market_catalog(markets, meta)

        threading.Thread(target=fetch, daemon=True).start()

    def _set_market_catalog(self, markets: list[str], meta: dict[str, dict[str, str]]) -> None:
        # Always update metadata for min size/notional hints
        self.market_meta = meta
        # Only update dropdowns if market list actually changed (avoids UI churn)
        if markets != self.markets:
            self.markets = markets
            for panel in self._panels:
                panel.set_markets(markets)

    def add_tab(self) -> None:
        panel = MarketRunPanel(self.notebook, app=self)
        self._panels.append(panel)
        self.notebook.add(panel, text=panel.market_var.get())
        panel.set_markets(self.markets)
        self.notebook.select(panel)

    def rename_tab_for(self, panel: MarketRunPanel) -> None:
        title = panel.market_var.get() or "Market"
        try:
            self.notebook.tab(panel, text=title)
        except Exception:
            pass

    def remove_current_tab(self) -> None:
        current = self.notebook.select()
        if not current:
            return
        idx = self.notebook.index(current)
        panel = self._panels[idx]
        if panel._running.is_set() and not messagebox.askyesno("Confirm", "This market is running. Stop and remove tab?"):
            return
        panel.stop()
        # Cancel the drain queue loop and destroy widget
        if hasattr(panel, '_drain_job'):
            panel.after_cancel(panel._drain_job)
        self.notebook.forget(current)
        panel.destroy()
        self._panels.pop(idx)

    def _on_close(self) -> None:
        if self._monitor_btn_job:
            try:
                self.root.after_cancel(self._monitor_btn_job)
            except Exception:
                pass
            self._monitor_btn_job = None
        if self._ui_queue_job:
            try:
                self.root.after_cancel(self._ui_queue_job)
            except Exception:
                pass
            self._ui_queue_job = None
        # Stop all panels
        for panel in self._panels:
            panel.stop()
            if hasattr(panel, '_drain_job'):
                panel.after_cancel(panel._drain_job)
        # Wait for threads to finish (up to 2 seconds total)
        deadline = time.time() + 2
        for panel in self._panels:
            if panel._thread and panel._thread.is_alive():
                remaining = max(0, deadline - time.time())
                panel._thread.join(timeout=remaining)
        self.root.destroy()

    def run(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        self.root.mainloop()


def main() -> None:
    # Entry point for `python -m grvt_volume_boost.gui_multi_market`.
    VolumeBoostGUI().run()


if __name__ == "__main__":
    main()
