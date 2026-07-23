# 京东直达下单工具 · 操作手册

> 适用版本：2026-07-22 整理
> 配套文件：`jd_order.py`（调度器）、`jd_order_browserless.py`（无浏览器版）、`jd_direct_order.py`（浏览器版）
> 配套分析：本报告同目录 `jd-trade-commlist-analysis.md`

---

## 一、这套工具是干嘛的

把你转发/分享的京东链接，一键变成「**直达结算 → 自动登录态 → 下单（可选）→ 抓取提交参数**」的完整流程。支持三种运行形态：

| 形态 | 文件 | 特点 |
|---|---|---|
| **统一调度器** | `jd_order.py` | 默认入口。**无浏览器优先，失败自动回退浏览器版** |
| **无浏览器版** | `jd_order_browserless.py` | 纯 `requests` 发请求；登录态从运行中的 Chrome 用 CDP 自动抠，**不模拟浏览器下单** |
| **浏览器版** | `jd_direct_order.py` | Playwright **CDP 直连你真实运行的 Chrome**，复用真实登录态与指纹，绕过风控 |

核心结论（详见分析报告）：
- 京东「直达结算」链接形如 `https://trade.m.jd.com/pay?commlist=<SKU>,,<数量>,<SKU>,<数量>,0,0`，靠 `commlist` 跳过购物车，靠 `pt_key` cookie 证明已登录。
- 下单真正加的参数由前端 JS 在点击提交时补：`token` + `h5st` 签名 + `x-api-eid-token` 风控令牌 + 设备指纹，POST 到 `balance_submitOrder` / `balance_submitOrder_m`。

---

## 二、环境准备（只需做一次）

### 1. 让 Chrome 可被脚本控制
- 打开 Chrome，地址栏输入 `chrome://inspect/#remote-debugging`，把 **Remote debugging** 开关拨到 **On**。
- 拨开后 Chrome 会在本地写一个端口文件：`C:\Users\78384\AppData\Local\Google\Chrome\User Data\DevToolsActivePort`（第 1 行是端口，第 2 行是浏览器级 ws 路径）。脚本自动读它。

### 2. 在该 Chrome 里登录京东
- 打开 `m.jd.com` 登录（**必须是移动端 m.jd.com**，PC 端 www.jd.com 的登录态不通用）。
- 登录态就是 `pt_key`/`pt_pin` 两个 HttpOnly cookie，脚本会自己从这里抠，你不用手动复制。

### 3. 安装 Python 依赖
依赖已装在隔离 venv 里，直接用它跑即可（无需你再装）：

```
C:\Users\78384\.workbuddy\binaries\python\envs\default\Scripts\python.exe
```

该 venv 已含：`playwright`（含你系统 Chrome 复用，无需下载 150MB chromium）、`requests`、`websocket-client`。
如要自行重建：

```bash
C:\Users\78384\.workbuddy\binaries\python\versions\3.13.12\python.exe -m venv C:\Users\78384\.workbuddy\binaries\python\envs\default
C:\Users\78384\.workbuddy\binaries\python\envs\default\Scripts\pip install -i https://pypi.tuna.tsinghua.edu.cn/simple playwright requests websocket-client
```

---

## 三、三种常用命令

> 工作目录：`C:\Users\78384\WorkBuddy\2026-07-22-09-22-27`
> 下文用 `PY` 指代上面的 venv python 路径。

### ① 给 SKU 直接下单（最常用）
```bash
PY jd_order.py 10042124137320 1 --auto-order --chrome
```
流程：自动抠 `pt_key` → 试无浏览器版 → 被京东拒 → 自动 CDP 直连真实 Chrome → 打开结算页 → 点「在线支付」→ 跳收银台 → 检测订单号。

### ② 给分享短链（3.cn/...?jkl=...）
```bash
PY jd_order.py "https://3.cn/2WmCh-2q?jkl=@N31Nk4PlxO0w@ ZH9112" --chrome --auto-order
```
注意：分享文案里链接后面常跟一个空格 + 邀请码（如 `ZH9112`），脚本会自动把链接和邀请码截断，只取链接部分。短链的 `jkl` 跳转是前端 JS 做的，**只能浏览器版解析**，调度器会自动切到浏览器版。

### ③ 只想看抓到的登录态（只读，不下单）
```bash
PY jd_order.py 10042124137320 --grab-cookie
```
打印 `pt_key` / `pt_pin`，方便你确认或保存。**不启动浏览器、不下单。**

---

## 四、参数与模式说明

### `jd_order.py` 调度器参数
| 参数 | 作用 |
|---|---|
| 位置参数 1 | 输入：SKU 数字 / 商品页 URL / 分享短链 |
| 位置参数 2 | 数量（默认 1） |
| `--mode auto` | 默认：**无浏览器先试，失败回退浏览器** |
| `--mode browserless` | 只用纯请求，失败**不**回退（需有效 `pt_key` + JD 接受签名） |
| `--mode browser` | 只用浏览器版 |
| `--chrome` | 复用你系统里的 Chrome（**必带**，否则浏览器版要下载 150MB chromium） |
| `--auto-order` | 真正点提交下单；**不带此参数只打开结算页不买** |
| `--pt-key` / `--pt-pin` | 手动传登录态（不传则脚本自动从 Chrome 抠） |
| `--no-grab` | 禁止自动抠 cookie（强制手动传 `--pt-key`） |
| `--phone` | 浏览器版短信重登用的手机号 |
| `--profile` | 浏览器档案目录（默认 `./jd_profile`，持久化 cookie） |
| `--grab-cookie` | 只抓并打印登录态，不进入下单流程 |

### 浏览器版 `jd_direct_order.py` 独有行为
- **优先 CDP 直连真实 Chrome**：`connect_over_cdp(ws://127.0.0.1:<port><wsPath>)`，复用你真实的登录态和浏览器指纹 → **京东不风控**。
- **登录态失效时短信重登**：若检测到无 `pt_key`，自动切「短信登录」页签 → 填手机号 → 获取验证码 → 终端输入 → 提交；命中失败则退化成手动在浏览器登录，检测到 `pt_key` 后继续。
- **提交按钮用页面内 JS 点击**：京东移动端用 Taro 框架，按钮是 `<taro-button-core>`，普通选择器点不到，脚本用 JS 找含「在线支付/去支付」文本且可见的元素并 `.click()`。
- **跨标签页捕获**：收银台（`mpay.m.jd.com`）会在新标签页打开，脚本监听所有标签页并跨页检测 `orderId` 判断下单成功。

---

## 五、运行流程（以 ① 为例，逐步说明）

1. 解析输入 → 得到 SKU `10042124137320`、数量 1。
2. 调度器打印 `尝试无浏览器版`。
3. 无浏览器版从运行中 Chrome 抠 `pt_key`/`pt_pin`（已验证可抠到）。
4. 纯 `requests` 发 `balance_getCurrentOrder` —— 京东返回 `code=1` 拒绝（因为纯 Python 的 `h5st`/`token` 不被接受）。
5. 抛出 `BrowserlessFailed`，调度器打印 `回退到浏览器版`。
6. 浏览器版 `connect_over_cdp` 连上真实 Chrome，确认 `pt_key` 有效（免短信）。
7. 打开 `trade.m.jd.com/pay?commlist=...`（已登录态，直达结算页）。
8. 等结算页 + 订单预览加载完，JS 点击「在线支付」。
9. 京东打开收银台 `mpay.m.jd.com/...`，跨页检测到 `orderId` → 判定下单成功。

> 验证记录：本次流程实测成功下单（鲜朗猫粮 ¥188，订单号 `3567404011210666`）。

---

## 六、常见问题排查

| 现象 | 原因 | 处理 |
|---|---|---|
| `jd_cookie_count=0` / 无 `pt_key` | 运行中的 Chrome 没登录京东，或远程调试没开 | 开 `chrome://inspect/#remote-debugging`，在该 Chrome 登录 `m.jd.com` |
| CDP 连接 403 | `websocket-client` 默认带 `Origin` 头被 Chrome 拒 | 脚本已用 `suppress_origin=True` 关掉；若仍报，确认用的是脚本内置 `extract_cookies_cdp` |
| 无浏览器版一直 `code=1` | 纯 Python `h5st` 是 best-effort，JD 风控拒 | 正常现象，会自动回退浏览器版；或手动 `--pt-key` 仍大概率被拒，**无浏览器下单不保证成功** |
| 浏览器版开新上下文被风控「活动异常火爆」 | 注入 `pt_key` 到新指纹上下文被 JD 判异常 | 已修复：改为 CDP 直连**真实** Chrome，不复用新上下文注入 |
| 浏览器版点了没反应 / 捕获 0 | Taro 按钮普通选择器点不到；收银台开新标签没监听 | 已修复：JS 找按钮 `.click()` + 监听所有标签页 |
| 分享短链在调度器里解析失败 | `jkl` 跳转靠前端 JS，无浏览器版无解 | 给短链时带 `--chrome`，调度器会自动走浏览器版 |
| Playwright 提示下载 chromium | 没加 `--chrome` | 加 `--chrome` 复用系统 Chrome，免下载 |

---

## 七、重要提醒

1. **下单是真实交易**：`--auto-order` 会下真实订单、产生真实金额。不需要的待支付单请到京东「我的订单」取消，避免占库存。
2. **测试订单待清理**：调试期间已下过数笔测试单（订单号含 `...06911` / `...07558` / `...10666` 等），如不需要请取消/处理。
3. **无浏览器版是「尽力而为」**：京东签名 JS（`js_security_v3.js`）已加密、且下单依赖浏览器派生的 `token`/`x-api-eid-token`，纯 Python 无法 100% 稳定过风控。所以「无浏览器优先 + 回退浏览器版」是当前最稳的组合，不是 bug。
4. **登录态有效期**：`pt_key` 会过期。过期后浏览器版会自动走短信重登；无浏览器版则抠不到，自动回退。
5. **不要用脚本做违规批量/秒杀**：仅用于个人正常下单与参数研究。

---

## 八、一句话速记

```
# 日常：SKU 直接买（自动抠登录态 + 回退浏览器）
PY jd_order.py <SKU> <数量> --auto-order --chrome

# 分享链接：直接买
PY jd_order.py "<分享链接>" --chrome --auto-order

# 只看看登录态
PY jd_order.py <SKU> --grab-cookie
```

其中 `PY = C:\Users\78384\.workbuddy\binaries\python\envs\default\Scripts\python.exe`
工作目录 = `C:\Users\78384\WorkBuddy\2026-07-22-09-22-27`
