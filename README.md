# GRVT 刷量工具（双号对敲）使用说明

本项目提供 GUI 和命令行（CLI）两种方式，使用两个 GRVT 账号在永续合约市场进行对冲交易（对敲）以产生交易量。登录方式为扫码（QR）登录并保存本地会话（不需要 API Key）。

English README: `README_en.md`.

## 风险提示

- 合约交易风险很高，可能亏损。
- “刷量/对敲/自成交” 可能违反交易所规则或当地法规。使用者自行承担全部风险与责任。

## Windows 一键运行（推荐）

1. 打开 GitHub Releases，下载最新的 `GRVTVolumeBoost-windows-x64.zip`
2. 解压缩
3. 双击运行 `GRVTVolumeBoost.exe`

说明：
- Release 版本已打包 Playwright Chromium，扫码登录/刷新 Cookie 不需要额外安装浏览器。
- 本工具会在本地生成 `session/`、`session_testnet/`、`grvt_cookie_cache*.json` 等文件（这些包含登录凭证），请勿分享。

## Python 源码运行

### 1) 安装依赖

建议 Python 3.10+

```bash
pip install -r requirements.txt
python -m playwright install
```

### 2) 启动 GUI

```bash
python volume_boost_gui.py
```

## GUI 使用方法（核心流程）

### 1) 选择环境（Prod / Testnet）

GUI 顶部可以切换：
- `PROD`：真实环境（会产生真实交易）
- `TESTNET`：测试网（用于练习/验证流程）

切换环境会重启应用，并使用不同的会话目录：
- PROD：`session/`
- TESTNET：`session_testnet/`

### 2) 账号设置（扫码登录）

进入 `Setup Account`（账号设置）页面：

对 Account 1 和 Account 2 分别执行：
- `Capture QR`：截图选择二维码区域（推荐）
  - 或 `Select Image...`：选择本地二维码图片（PNG/JPG）
- 点击 `Login` 开始登录
- 如果出现邮箱验证码验证，GUI 会提示你输入验证码

登录成功后会保存浏览器状态文件：
- `session/grvt_browser_state_1.json`
- `session/grvt_browser_state_2.json`

### 3) 选择交易市场与参数

常用参数说明：
- Market：选择交易对（例如 `BTC_USDT_Perp`）
- Mode：
  - Instant（开仓后立即平仓）
  - 其他模式用于分阶段建仓/持仓/平仓
- Size：下单数量（可用合约数量或 USD 名义价值，取决于 GUI 设置）
- Rounds：轮数
- Delay：每轮间隔
- Direction policy：方向策略（随机/固定由 Account1 做多或做空）

### 4) 启动与监控

- 点击 `Start` 开始执行
- `Monitor` 可查看两边账号的持仓与挂单（主要由 WS 驱动，更新更及时）
- 如果检测到 “外部成交/单腿风险”，GUI 会提示你选择继续或停止

## 常见问题

### 1) 提示需要重新登录/会话过期

- Cookie 通常会过期，需要工具自动刷新；如果刷新失败，重新扫码登录即可。
- 如果会话签名密钥过期（`grvt_ss_on_chain` 不存在），也需要重新扫码登录（并完成邮箱验证）。

### 2) 为什么会出现 “外部成交/单腿”？

对敲流程通常是：
1) Account A 先挂 Maker 限价单（post-only）
2) 等 WS 确认该挂单已在盘口
3) Account B 再下 IOC 来吃掉该挂单

若同价位存在外部订单排队（FIFO），或者盘口变化导致 IOC 没吃到自己的单，就可能出现 “单腿/外部成交”。工具会尽力检测并提示/处理。

### 3) 打包版/源码版的文件安全

以下文件包含登录凭证/会话信息，请勿上传或分享：
- `session/`、`session_testnet/`
- `grvt_cookie_cache*.json`
- `grvt_gui_prefs.json`

## 开发者

- 作者：撸毛小狗
- 推特：<https://x.com/LumaoDoggie>
- GRVT 高返佣注册链接：<https://grvt.io/?ref=lumaoDoggie>  , 全网最高的 35%返佣 + 1.3倍积分加成
- Telegram 交流群: <https://t.me/+Oe-Ul8Pzyck4ZGQ1>  (返佣在群里领,每个月底发放)
