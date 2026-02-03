from __future__ import annotations

import os
import re


def get_lang() -> str:
    lang = (os.getenv("GRVT_LANG", "en") or "en").strip().lower()
    return "zh" if lang in ("zh", "zh-cn", "cn", "chinese") else "en"


_T: dict[str, dict[str, str]] = {
    "en": {
        "session.missing": "Session file not found: {name}",
        "session.invalid_json": "Invalid JSON in {name}",
        "session.raw_localstorage": "{name} is raw localStorage format (re-login via QR)",
        "session.no_cookies": "{name} has no cookies (re-login via QR)",
        "session.missing_session_key": "{name} missing grvt_ss_on_chain (re-login via QR)",
        "app.title": "GRVT Volume Boost (Multi-market)",
        "app.title.testnet": "GRVT Volume Boost (Multi-market) [TESTNET]",
        "label.env": "Env:",
        "env.prod": "PROD",
        "env.testnet": "TESTNET",
        "btn.add_market": "Add Market",
        "btn.remove_current": "Remove Current",
        "btn.monitor": "Monitor",
        "btn.monitor_warn": "Monitor ⚠",
        "btn.setup_account": "Setup Account",
        "btn.stop_all": "Stop All",
        "btn.about": "About",
        "dlg.stop_all.title": "Stop All",
        "dlg.stop_all.stopped": "Stopped {n} running tab(s)",
        "dlg.stop_all.none": "No tabs were running",
        "btn.lang_to_zh": "中文",
        "btn.lang_to_en": "English",
        "dlg.switch_env.title": "Switch Environment",
        "dlg.switch_env.body": "Switching environment will STOP running tabs and restart the app.\n\nContinue?",
        "dlg.switch_lang.title": "Switch Language",
        "dlg.switch_lang.body": "Switching language will STOP running tabs and restart the app.\n\nContinue?",
        "dlg.restart_failed.title": "Restart Failed",
        "dlg.restart_failed.body": "Failed to restart: {err}",
        "dlg.not_configured.title": "Not Configured",
        "dlg.not_configured.body": "Configure accounts first via 'Setup Account'",
        "about.title": "About / 关于",
        "about.author": "Author / 作者:",
        "about.twitter": "Twitter / 推特账号:",
        "about.referral": "GRVT referral sign-up / GRVT高返佣账号注册:",
        "setup.title": "Account Setup",
        "setup.account": "Account {n}",
        "setup.session": "Session:",
        "setup.capture": "Capture QR",
        "setup.select_image": "Select Image...",
        "setup.login": "Login",
        "setup.remove_session": "Remove Session",
        "setup.close": "Close",
        "setup.not_checked": "Not checked",
        "setup.session_ok": "OK",
        "setup.session_ok_missing_ids": "OK (missing IDs; will derive on start)",
        "setup.capture_cancelled": "Capture cancelled",
        "setup.select_region": "Select QR region...",
        "setup.region_instructions": "Drag to select QR code region. Press ESC to cancel.",
        "setup.qr_decoded": "QR decoded. Click Login to start.",
        "setup.qr_decode_failed": "QR decode failed{extra}. Try recapturing with more padding.",
        "setup.qr_not_grvt": "Not a GRVT QR code",
        "setup.no_qr_yet": "No QR yet. Use Capture QR or Select Image.",
        "setup.logging_in": "Logging in headless... (may take 1-2 min)",
        "email_verify.title": "GRVT Email Verification",
        "email_verify.body": "Email verification required.\n\nEnter the code from your email:",
        "setup.login_ok": "Success",
        "setup.login_ok_body": "Account {n} logged in successfully!",
        "setup.login_failed": "Login Failed",
        "setup.login_failed_hint": "Login failed. Capture a fresh QR and try again.",
        "setup.sub_select_title": "Select Subaccount (Account {n})",
        "setup.sub_select_body": "Multiple subaccounts detected.\nSelect which subaccount to use for trading:",
        "setup.sub_cancel": "Cancel",
        "setup.sub_use": "Use Selected",
        "setup.sub_selected_title": "Subaccount Selected",
        "setup.sub_selected_body": "Account {n} now uses subaccount {chain_id}.",
        "setup.invalid_subaccount": "Invalid subaccount selection data.",
        "setup.remove_confirm_title": "Confirm Removal",
        "setup.remove_confirm_body": "Remove session for Account {n}?\n\nThis will require re-login via QR code.",
        "setup.removed_title": "Removed",
        "setup.removed_body": "Session for Account {n} removed.",
        "setup.remove_failed": "Failed to remove session: {err}",
        "setup.no_session": "No session file for Account {n}",
        "setup.session_removed": "Session removed",
        "setup.pillow_missing": "PIL/Pillow not installed. Run: pip install Pillow",
        "monitor.title": "Position Monitor",
        "monitor.ws": "WS: A1={a1} A2={a2}",
        "monitor.last_updated": "Last updated at {ts}",
        "monitor.last_updated_warn": "Last updated at {ts}  ({warn})",
        "monitor.loading": "Last updated at ... (loading)",
        "monitor.no_positions": "(No open positions/orders)",
        "monitor.col.market": "Market",
        "monitor.col.a1_size": "Acc1 Size",
        "monitor.col.a1_usd": "Acc1 USD",
        "monitor.col.a2_size": "Acc2 Size",
        "monitor.col.a2_usd": "Acc2 USD",
        "monitor.col.orders": "Open Orders",
	        "monitor.col.status": "Status",
	        "monitor.status.hedge": "Hedge",
	        "monitor.status.unbalanced": "Unbalanced",
	        "monitor.status.same_side": "Same Side!",
	        "monitor.btn.set_leverage_50": "Set Leverage 50x",
	        "monitor.lev.title": "Set Leverage",
	        "monitor.lev.confirm": "Set initial leverage to 50x for all markets with open positions (both accounts)?\n\nIf 50x is not allowed, the highest accepted leverage will be used.",
	        "monitor.lev.running": "Setting...",
	        "monitor.lev.cookie_error": "Cookie refresh failed. Please re-login.",
	        "monitor.lev.auth_error": "Failed to read positions (session expired?). Please re-login.",
	        "monitor.lev.failed": "Failed to set leverage (unexpected error).",
	        "monitor.lev.result": "Done.\n\nSuccess: {ok}\nFallback: {fallback}\nSkipped (already 50x): {skipped}\nFailed: {fail}",
	        "panel.market": "Market:",
	        "panel.pick_for_me": "pick for me",
	        "panel.mode": "Mode:",
        "panel.mode.instant": "Open & Instant Close",
        "panel.mode.build_hold_close": "Build, Hold & Close",
        "panel.mode.build_hold": "Build & Hold",
        "panel.mode.close_existing": "Close Existing",
        "panel.start": "START",
        "panel.stop": "STOP",
        "panel.debug": "Debug",
        "panel.params": "Parameters",
        "panel.size_type": "Size type:",
        "panel.size_type.contracts": "Contracts",
        "panel.size_type.usd": "USD Notional",
        "panel.size_contracts": "Size (contracts):",
        "panel.notional_usd": "Notional (USD):",
        "panel.direction": "Direction:",
        "panel.dir.random": "Random",
        "panel.dir.a1_long": "Account 1 long",
        "panel.dir.a1_short": "Account 1 short",
        "panel.rounds": "Rounds:",
        "panel.delay": "Instant delay (sec):",
        "panel.max_margin": "Max margin (%):",
        "panel.hold": "Hold time (min):",
        "hint.min_size": "Min size: {min_size}",
        "hint.min_size_low": "Min size: {min_size} (too low)",
        "hint.min_size_notional": "Min size: {min_size} | Min notional: ${min_notional}",
        "account.no_config": "No accounts configured",
        "account.display1": "Account 1: {name} ({sub_account_id})",
        "account.display2": "Account 2: {name} ({sub_account_id})",
        "spread.tight": "⚠ Spread tight ({ticks} ticks)",
        "spread.ok": "Spread: {ticks} ticks",
        "reco.title": "Recommended Markets (by Spread)",
        "reco.body": "Markets with ≥{min_ticks} ticks spread (ranked largest first):",
        "reco.col.market": "Market",
        "reco.col.spread": "Spread (ticks)",
        "reco.col.bid": "Bid",
        "reco.col.ask": "Ask",
        "reco.found": "Found {count} markets with ≥{min_ticks} ticks spread",
        "reco.analyzing": "Analyzing...",
        "reco.analyzing_progress": "Analyzing... {done}/{total}",
        "reco.refresh": "Refresh",
        "reco.close": "Close",
        "common.loading": "Loading...",
    },
    "zh": {
        "session.missing": "未找到会话文件：{name}",
        "session.invalid_json": "会话文件 JSON 无效：{name}",
        "session.raw_localstorage": "{name} 不是完整会话格式（请重新扫码登录）",
        "session.no_cookies": "{name} 缺少 Cookie（请重新扫码登录）",
        "session.missing_session_key": "{name} 缺少 grvt_ss_on_chain（请重新扫码登录）",
        "app.title": "GRVT 刷量工具（多市场）",
        "app.title.testnet": "GRVT 刷量工具（多市场）[测试网]",
        "label.env": "环境：",
        "env.prod": "正式",
        "env.testnet": "测试网",
        "btn.add_market": "添加市场",
        "btn.remove_current": "移除当前",
        "btn.monitor": "监控",
        "btn.monitor_warn": "监控 ⚠",
        "btn.setup_account": "账户设置",
        "btn.stop_all": "全部停止",
        "btn.about": "关于",
        "dlg.stop_all.title": "全部停止",
        "dlg.stop_all.stopped": "已停止 {n} 个运行中的标签页",
        "dlg.stop_all.none": "没有正在运行的标签页",
        "btn.lang_to_zh": "中文",
        "btn.lang_to_en": "English",
        "dlg.switch_env.title": "切换环境",
        "dlg.switch_env.body": "切换环境会停止正在运行的任务并重启程序。\n\n继续吗？",
        "dlg.switch_lang.title": "切换语言",
        "dlg.switch_lang.body": "切换语言会停止正在运行的任务并重启程序。\n\n继续吗？",
        "dlg.restart_failed.title": "重启失败",
        "dlg.restart_failed.body": "重启失败：{err}",
        "dlg.not_configured.title": "未配置",
        "dlg.not_configured.body": "请先在“账户设置”中配置两个账号",
        "about.title": "关于",
        "about.author": "作者：",
        "about.twitter": "推特账号：",
        "about.referral": "GRVT高返佣账号注册：",
        "setup.title": "账户设置",
        "setup.account": "账号 {n}",
        "setup.session": "会话：",
        "setup.capture": "截图二维码",
        "setup.select_image": "选择图片…",
        "setup.login": "登录",
        "setup.remove_session": "移除会话",
        "setup.close": "关闭",
        "setup.not_checked": "未检查",
        "setup.session_ok": "正常",
        "setup.session_ok_missing_ids": "正常（缺少ID；启动时会自动补全）",
        "setup.capture_cancelled": "已取消截图",
        "setup.select_region": "请选择二维码区域…",
        "setup.region_instructions": "拖动选择二维码区域，按 ESC 取消。",
        "setup.qr_decoded": "二维码已解析，点击“登录”开始。",
        "setup.qr_decode_failed": "二维码解析失败{extra}。请重新截图并留出更多边距。",
        "setup.qr_not_grvt": "不是 GRVT 二维码",
        "setup.no_qr_yet": "还没有二维码。请使用“截图二维码”或“选择图片”。",
        "setup.logging_in": "正在无头登录…（可能需要 1-2 分钟）",
        "email_verify.title": "GRVT 邮箱验证",
        "email_verify.body": "需要邮箱验证码。\n\n请输入邮件中的验证码：",
        "setup.login_ok": "成功",
        "setup.login_ok_body": "账号 {n} 登录成功！",
        "setup.login_failed": "登录失败",
        "setup.login_failed_hint": "登录失败。请重新获取新的二维码再试。",
        "setup.sub_select_title": "选择子账户（账号 {n}）",
        "setup.sub_select_body": "检测到多个子账户。\n请选择用于交易的子账户：",
        "setup.sub_cancel": "取消",
        "setup.sub_use": "使用所选",
        "setup.sub_selected_title": "已选择子账户",
        "setup.sub_selected_body": "账号 {n} 已切换到子账户 {chain_id}。",
        "setup.invalid_subaccount": "子账户数据无效。",
        "setup.remove_confirm_title": "确认移除",
        "setup.remove_confirm_body": "确定移除账号 {n} 的会话吗？\n\n移除后需要重新扫码登录。",
        "setup.removed_title": "已移除",
        "setup.removed_body": "账号 {n} 的会话已移除。",
        "setup.remove_failed": "移除会话失败：{err}",
        "setup.no_session": "账号 {n} 没有会话文件",
        "setup.session_removed": "会话已移除",
        "setup.pillow_missing": "未安装 Pillow。请运行：pip install Pillow",
        "monitor.title": "持仓监控",
        "monitor.ws": "WS：A1={a1} A2={a2}",
        "monitor.last_updated": "更新时间 {ts}",
        "monitor.last_updated_warn": "更新时间 {ts}  （{warn}）",
        "monitor.loading": "正在加载…",
        "monitor.no_positions": "（无持仓/挂单）",
        "monitor.col.market": "市场",
        "monitor.col.a1_size": "账号1数量",
        "monitor.col.a1_usd": "账号1美元",
        "monitor.col.a2_size": "账号2数量",
        "monitor.col.a2_usd": "账号2美元",
        "monitor.col.orders": "挂单数",
	        "monitor.col.status": "状态",
	        "monitor.status.hedge": "对冲",
	        "monitor.status.unbalanced": "不平衡",
	        "monitor.status.same_side": "同向！",
	        "monitor.btn.set_leverage_50": "一键设为 50x",
	        "monitor.lev.title": "设置杠杆",
	        "monitor.lev.confirm": "将所有有持仓的市场（两个账号）初始杠杆设置为 50x？\n\n如果 50x 不允许，将自动设置为可用的最高杠杆。",
	        "monitor.lev.running": "设置中…",
	        "monitor.lev.cookie_error": "Cookie 刷新失败，请重新登录。",
	        "monitor.lev.auth_error": "读取持仓失败（会话过期？），请重新登录。",
	        "monitor.lev.failed": "设置杠杆失败（未知错误）。",
	        "monitor.lev.result": "完成。\n\n成功：{ok}\n降级（非 50x）：{fallback}\n跳过（已是 50x）：{skipped}\n失败：{fail}",
	        "panel.market": "市场：",
	        "panel.pick_for_me": "帮我选",
	        "panel.mode": "模式：",
        "panel.mode.instant": "开仓并立即平仓",
        "panel.mode.build_hold_close": "建仓、持有并平仓",
        "panel.mode.build_hold": "建仓并持有",
        "panel.mode.close_existing": "仅平已有仓位",
        "panel.start": "开始",
        "panel.stop": "停止",
        "panel.debug": "调试",
        "panel.params": "参数",
        "panel.size_type": "下单方式：",
        "panel.size_type.contracts": "合约数量",
        "panel.size_type.usd": "美元名义金额",
        "panel.size_contracts": "数量（合约）：",
        "panel.notional_usd": "名义金额（美元）：",
        "panel.direction": "方向：",
        "panel.dir.random": "随机",
        "panel.dir.a1_long": "账号1做多",
        "panel.dir.a1_short": "账号1做空",
        "panel.rounds": "轮数：",
        "panel.delay": "立即平仓延迟（秒）：",
        "panel.max_margin": "最大保证金比例（%）：",
        "panel.hold": "持有时间（分钟）：",
        "hint.min_size": "最小数量：{min_size}",
        "hint.min_size_low": "最小数量：{min_size}（过小）",
        "hint.min_size_notional": "最小数量：{min_size} | 最小名义：${min_notional}",
        "account.no_config": "未配置账号",
        "account.display1": "账号1：{name}（{sub_account_id}）",
        "account.display2": "账号2：{name}（{sub_account_id}）",
        "spread.tight": "⚠ 点差较小（{ticks} 跳）",
        "spread.ok": "点差：{ticks} 跳",
        "reco.title": "推荐市场（按点差）",
        "reco.body": "点差 ≥{min_ticks} 跳的市场（按点差从大到小）：",
        "reco.col.market": "市场",
        "reco.col.spread": "点差（跳）",
        "reco.col.bid": "买一价",
        "reco.col.ask": "卖一价",
        "reco.found": "找到 {count} 个点差 ≥{min_ticks} 跳的市场",
        "reco.analyzing": "正在分析…",
        "reco.analyzing_progress": "正在分析… {done}/{total}",
        "reco.refresh": "刷新",
        "reco.close": "关闭",
        "common.loading": "加载中…",
    },
}


def tr(key: str, **kwargs) -> str:
    lang = get_lang()
    s = _T.get(lang, {}).get(key) or _T["en"].get(key) or key
    if kwargs:
        try:
            return s.format(**kwargs)
        except Exception:
            return s
    return s


def tr_log_line(line: str) -> str:
    """Best-effort translation for free-form log lines shown in the GUI status box.

    Most logs are generated as plain strings. Refactoring to structured events would be bigger;
    instead we translate common patterns here.
    """
    if not line or get_lang() != "zh":
        return line

    # Preserve indentation.
    lead = len(line) - len(line.lstrip(" "))
    prefix, body = line[:lead], line[lead:]

    exact = {
        "OK": "正常",
        "Refreshing cookies...": "正在刷新 Cookie…",
        "Starting price monitor...": "启动价格监控…",
        "Price monitor ready": "价格监控就绪",
        "--- DONE ---": "--- 完成 ---",
    }
    if body in exact:
        return prefix + exact[body]

    m = re.match(r"^Account\s*1:\s*(.*)$", body)
    if m:
        return prefix + f"账号1：{m.group(1)}"
    m = re.match(r"^Account\s*2:\s*(.*)$", body)
    if m:
        return prefix + f"账号2：{m.group(1)}"

    m = re.match(r"^Market:\s*(.*)$", body)
    if m:
        return prefix + f"市场：{m.group(1)}"

    m = re.match(r"^Mode:\s*(.*)$", body)
    if m:
        mode = m.group(1).strip()
        mode_map = {
            "instant": "立即开平",
            "build_hold_close": "建仓-持有-平仓",
            "build_hold": "建仓并持有",
            "close_existing": "仅平已有仓位",
        }
        return prefix + f"模式：{mode_map.get(mode, mode)}"

    m = re.match(r"^Normalized size:\s*(.*)$", body)
    if m:
        return prefix + f"标准化数量：{m.group(1)}"

    m = re.match(r"^Rounds:\s*(\d+)\s*\|\s*Delay:\s*(.*)$", body)
    if m:
        return prefix + f"轮数：{m.group(1)} | 延迟：{m.group(2)}"

    m = re.match(r"^\[Round\s*(\d+)/(\d+)\]$", body)
    if m:
        return prefix + f"[第 {m.group(1)}/{m.group(2)} 轮]"

    m = re.match(r"^\[Round\s*(\d+)\]\s*Margin:\s*long=([^,]+),\s*short=(.+)$", body)
    if m:
        return prefix + f"[第 {m.group(1)} 轮] 保证金：多={m.group(2).strip()}，空={m.group(3).strip()}"

    m = re.match(r"^(OPEN|CLOSE)\s+(.*)$", body)
    if m:
        verb = "开仓" if m.group(1) == "OPEN" else "平仓"
        return prefix + f"{verb} {m.group(2)}"

    m = re.match(r"^Retry\s*(\d+)/(\d+)\s*in\s*([0-9.]+)s\s*\((.*)\)\.\.\.$", body)
    if m:
        reason = m.group(4)
        reason = reason.replace("Maker order failed:", "Maker 下单失败：")
        reason = reason.replace("Taker order failed:", "Taker 下单失败：")
        reason = reason.replace("Price unstable", "价格不稳定")
        reason = reason.replace(
            "Attempted to create a limit order at a price outside of asset's price protection band.",
            "限价单价格超出该资产的价格保护区间。",
        )
        return prefix + f"重试 {m.group(1)}/{m.group(2)}，{m.group(3)} 秒后（{reason}）…"

    if body.startswith("FAILED: "):
        return prefix + "失败：" + body[len("FAILED: ") :]
    if body.startswith("ERROR: "):
        return prefix + "错误：" + body[len("ERROR: ") :]

    if body.startswith("Direction: "):
        s = body.replace("Direction: ", "方向：", 1)
        s = s.replace("random", "随机")
        s = s.replace("account1_long", "账号1做多")
        s = s.replace("account1_short", "账号1做空")
        s = s.replace("(resolved=", "（实际=")
        if "（实际=" in s and s.endswith(")"):
            s = s[:-1] + "）"
        return prefix + s

    return line
