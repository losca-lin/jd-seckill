# 京东抢购助手（JD Seckill Assistant）

本地运行的京东抢购辅助工具：在浏览器里打开结算页、核对商品信息、一键 / 定时 / 并发提交订单，内置多账号管理。

## 核心特性

- 苹果风格本地 GUI（浏览器打开 http://127.0.0.1:8899）
- 解析京东分享文案 / 短链（`3.cn` / `jd.com` / `u.jd.com`）自动提取 SKU
- 打开结算页并解析商品名、价格、数量、收货地址、**完整手机号**
- 一键提交 / 定时提交 / 并发提交（真实下单，跳转京东收银台完成付款）
- 多账号管理：**新增 / 切换 / 注销**

## 架构：多账号为什么变轻了？（方案②）

旧方案为每个账号开一份 GB 级的 Chrome `user-data-dir`，切换账号要重启整个浏览器进程。
本版改用 **Playwright 在同一浏览器进程内为每个京东账号创建独立的 `BrowserContext`**：

- 登录态以轻量 `storage_state` JSON 持久化到 `states/<账号>.json`
- 切换账号 = 切换 / 新建 context，**毫秒级、不重启浏览器**
- 单份浏览器二进制，磁盘占用极小
- 对外接口（`jd_cdp.py`）函数签名与旧 CDP 版兼容，便于维护

## 目录结构

```
jd-seckill/
├── jd_gui_server.py     # 本地 GUI 服务（内嵌 HTML/CSS/JS）
├── jd_cdp.py            # 浏览器驱动核心（Playwright 多 Context 隔离）
├── jd_accounts.json     # 账号配置（仅账号名 / 备注）
├── states/              # 各账号登录态（自动生成，已 gitignore，含隐私）
├── requirements.txt
├── README.md
├── docs/
│   └── jd-operation-manual.md   # 详细操作手册（由 doc/ 搬迁而来）
└── .gitignore
```

## 环境要求

- Python 3.8+
- 本机已安装 Google Chrome（程序复用，无需另下 Chromium）
- 已安装 Playwright Python 包

## 安装

```bash
pip install -r requirements.txt
# 若想让 Playwright 自带一个干净 Chromium（可选，不指定 channel 时使用）：
playwright install chromium
```

## 运行

```bash
python jd_gui_server.py
# 浏览器打开 http://127.0.0.1:8899
```

## 使用流程

1. 点「启动调试 Chrome」（首次建议用窗口模式方便登录）
2. 点「打开登录页」在弹出窗口完成京东登录（含验证码）
3. 在「提交订单」区填 SKU / 数量，或粘贴分享链接点「解析链接」
4. 点「打开并核对」确认商品 / 价格 / 地址 / **手机号**
5. 点「立即提交」真实下单，跳转京东收银台后去付款

> 更详细的逐页操作截图与说明见 [docs/jd-operation-manual.md](docs/jd-operation-manual.md)（原 `doc/` 下文档，已随项目迁移）。

## 多账号

- **新增**：账号卡片点「+ 新增」，填名称（可备注京东昵称）
- **切换**：点账号分段控件，毫秒级切换，不重启浏览器
- **注销**：点「注销当前账号登录」，清除本地登录态（不影响京东账号本身）

## 隐私与安全

- 登录态（cookie / localStorage）保存在 `states/`，已写入 `.gitignore`，**不会进入版本库**
- 请勿将 `states/` 目录分享或提交到任何仓库

## API 速览

| 接口 | 说明 |
|------|------|
| `/api/status` | 连接 / 登录 / 结算 / 收银台状态（含当前账号） |
| `/api/launch_chrome` | 启动浏览器（可切 headless） |
| `/api/open_login` | 打开登录页 |
| `/api/checkout` | 打开并核对结算页 |
| `/api/checkout_retry` | 风控冷却后重试 |
| `/api/resolve` | 解析分享链接 → SKU |
| `/api/submit` | 立即提交（真实下单） |
| `/api/submit_schedule` | 定时提交 |
| `/api/submit_tasks` / `/api/submit_cancel` | 任务列表 / 取消 |
| `/api/accounts` | 账号列表 |
| `/api/account_switch` | 切换账号（毫秒级） |
| `/api/account_add` | 新增账号 |
| `/api/account_logout` | 注销账号登录态 |

## 免责声明

本工具仅供学习研究，请遵守京东用户协议与相关法规。下单为真实交易，请自行评估风险，作者不对使用后果负责。
