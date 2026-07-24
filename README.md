# 京东抢购助手（JD Seckill Assistant）

本地运行的京东抢购辅助工具：在浏览器里打开结算页、核对商品信息、一键 / 定时 / 并发提交订单，内置多账号管理。

## 核心特性

- 苹果风格本地 GUI（浏览器打开 http://127.0.0.1:8899）
- 解析京东分享文案 / 短链（`3.cn` / `jd.com` / `u.jd.com`）自动提取 SKU
- 打开结算页并解析商品名、价格、数量、收货地址、**完整手机号**
- 一键抢购 / 定时抢购 / 并发抢购（真实下单，跳转京东收银台完成付款）
- 多账号管理：**新增 / 切换 / 注销**

## 架构：CDP + 真实 Chrome（按账号隔离 user-data-dir）

本工具通过 **Chrome DevTools Protocol（CDP）** 驱动你本机已安装的 Google Chrome，
**不使用 Playwright，也不下载无头 Chromium**。

- 每个京东账号对应一个独立的 Chrome 用户数据目录（`--user-data-dir`）：
  - `默认账号` → `chrome_jd/`
  - 其他账号 → `chrome_jd_<名>/`
- **登录态由 Chrome 自身持久化在该 profile 目录的 Cookies 数据库中**，进程退出 / 重启后依旧有效，无需额外的登录态文件。
- 切换账号 = 关闭当前调试 Chrome，用目标账号的 profile 目录重启（会重启浏览器进程，非毫秒级，但账号间登录态互不干扰）。
- 对外接口（`jd_cdp.py`）以 CDP（websocket-client）实现，与界面解耦。

> 历史方案曾用 Playwright 多 `BrowserContext` + `states/` 目录存登录态，因京东风控不签发会话 cookie 而弃用；相关 `states/` 目录已删除，请勿恢复。

## 目录结构

```
jd-seckill/
├── jd_gui_server.py     # 本地 GUI 服务（内嵌 HTML/CSS/JS）
├── jd_cdp.py            # 浏览器驱动核心（CDP + 真实 Chrome --user-data-dir 隔离）
├── jd_accounts.json     # 账号配置（账号名 / 备注 / 对应 profile 目录），本地文件，不纳入版本库
├── chrome_jd/           # 默认账号的 Chrome 用户数据（登录态在此，已 gitignore）
├── chrome_jd_*/         # 其他账号的 Chrome 用户数据（已 gitignore）
├── requirements.txt     # 依赖：websocket-client
├── README.md
├── docs/
│   └── jd-operation-manual.md   # 详细操作手册
└── .gitignore
```

## 环境要求

- Python 3.8+
- 本机已安装 Google Chrome（程序复用，无需另下 Chromium / 无需 Playwright）
- 依赖仅 `websocket-client`（`requirements.txt`）

## 安装

```bash
pip install -r requirements.txt
```

无需 `playwright install`；程序直接驱动你已装的 Chrome。

## 运行

```bash
python jd_gui_server.py
# 浏览器打开 http://127.0.0.1:8899
```

## 使用流程

1. 状态栏会自动轮询连接 / 登录 / 结算状态；点「启动调试 Chrome」拉起浏览器（是否后台无窗口可在状态栏切换）。
2. 未登录时状态栏会出现「去登录」按钮，点击后自动切到窗口模式，在弹出窗口完成京东扫码 / 短信登录（含验证码）；登录成功后按钮自动隐藏。
3. 在抢购区粘贴分享链接点「解析链接」自动提取 SKU，或手动填 SKU / 数量 / 并发 / 重试。
4. 点「🚀 立即抢购」会**自动打开结算页并下单**（无需先单独核对）；若想先核对价格 / 地址 / 手机号，可先点「仅打开结算页核对」。
5. 下单后跳转京东收银台，去手机 / 电脑完成付款。可选「定时抢购」，到点由后台线程自动执行（关掉页面也不影响）。

> 更详细的逐页操作截图与说明见 [docs/jd-operation-manual.md](docs/jd-operation-manual.md)。

## 多账号

- **新增**：账号行点「+ 新增」，填名称（可备注京东昵称）；工具会新建一个 Chrome profile 目录（`chrome_jd_<名>`）。
- **切换**：点账号分段控件，关闭当前调试 Chrome 并以目标账号的 profile 重启（会重启浏览器，账号间登录态互不干扰）。
- **注销**：点「注销当前账号登录」，删除该 profile 的 Cookies 数据库（不影响京东账号本身，下次需重新登录）。
- 账号列表维护在本地 `jd_accounts.json`（含每个账号对应的 profile 目录），该文件**不纳入版本库**，仅存在于本机。

## 隐私与安全

- 登录态（cookie）保存在各账号的 Chrome profile 目录（`chrome_jd*/`，已写入 `.gitignore`），**不会进入版本库**。
- 账号配置 `jd_accounts.json` 含账号名 / profile 映射，**按需求不纳入版本库**（已在 `.gitignore` 忽略），请勿手动 `git add`。
- 请勿将 `chrome_jd*/` 或 `jd_accounts.json` 分享或提交到任何仓库。

## API 速览

| 接口 | 说明 |
|------|------|
| `/api/status` | 连接 / 登录 / 结算 / 收银台状态（含当前账号，前端自动轮询） |
| `/api/launch_chrome` | 启动浏览器（可切 headless） |
| `/api/open_login` | 打开登录页（自动切窗口模式） |
| `/api/checkout` | 打开并核对结算页（不下单） |
| `/api/checkout_retry` | 风控冷却后重试 |
| `/api/resolve` | 解析分享链接 → SKU |
| `/api/submit` | 立即抢购（自动打开结算页并真实下单） |
| `/api/submit_schedule` | 定时抢购 |
| `/api/submit_tasks` / `/api/submit_cancel` | 任务列表 / 取消 |
| `/api/accounts` | 账号列表 |
| `/api/account_switch` | 切换账号（重启浏览器加载该账号 profile） |
| `/api/account_add` | 新增账号 |
| `/api/account_logout` | 注销账号登录态 |

## 免责声明

本工具仅供学习研究，请遵守京东用户协议与相关法规。下单为真实交易，请自行评估风险，作者不对使用后果负责。
