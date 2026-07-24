# 京东抢购助手 · Java 版（Spring Boot）

本目录是根目录 Python 版（`jd_gui_server.py` + `jd_cdp.py`）的 **1:1 对等重写**，
技术栈为 **Spring Boot 2.7.18 + Java-WebSocket 1.5.3**，功能完全一致：
结算页解析、提交订单、预热模式、循环定时抢购、Server 酱微信推送、单页 HTML 前端。

> 与 Python 版的主要差别只是语言/框架：CDP 客户端从 `websocket-client` 改为
> 纯 Java WebSocket 手写实现（因为 `cdp4j` 不在 Maven 公共仓库），其余行为一致。

## 架构

```
jd-seckill-java/
├── pom.xml
├── src/main/java/com/example/jdseckill/
│   ├── JdSeckillJavaApplication.java     # 启动类
│   ├── cdp/
│   │   ├── CdpClient.java                # 单页 CDP 客户端（send id 匹配 / eval / navigate / reload / getCookies / screenshot）
│   │   ├── ChromeManager.java            # 单线程串行所有 CDP；连 9222；启动/关闭调试 Chrome；登录态探测；多账号；短链解析
│   │   └── PageSession.java              # (targetId, CdpClient) 持有
│   ├── service/
│   │   ├── CheckoutService.java          # 等价 checkout/retry：接口 JSON 拦截 + DOM 兜底 + 风控识别
│   │   ├── SubmitService.java            # 等价 submit_order：baseline 只认新收银台 + 真实鼠标点击 + 取单号
│   │   ├── ScheduleService.java          # 定时调度：预热 + 单次/循环 + 到点 + 取消
│   │   └── NotifyService.java            # Server 酱推送（复用父目录 jd_notify.json）
│   └── controller/ApiController.java     # 镜像全部 /api/* ；GET / 返回 static/index.html
├── src/main/resources/
│   ├── application.properties            # server.port=8899
│   └── static/index.html                 # 单页前端（从 Python 版搬入）
└── .gitignore                            # 忽略 target/、*.log
```

**关键设计**
- 所有浏览器操作统一提交到 `ChromeManager` 内的**单线程 executor** 串行执行，
  避免同一 CDP 连接并发 `recv` 冲突（与 Python 版单线程 worker 等价）。
- 连本地调试 Chrome（127.0.0.1:9222）走 `Proxy.NO_PROXY`，避开公司代理；
  推送 `sctapi.ftqq.com` 走系统默认代理出公网。
- 提交成功判定：只认「点击提交后**新出现**的收银台」（URL 不在点击前 baseline），
  防止浏览器里残留的旧收银台被误判为本单成功。

## 环境要求

- **JRE 8**（必须 Java 8，如 `1.8.0_221+`；不能换 Java 17，否则需 Spring Boot 3.x）
- 构建需 **JDK 8 + Maven 3.x**；若直接用已打好的 jar，只需 JRE 8
- 本机已安装 **Google Chrome**（标准路径），程序复用其调试模式，不另下 Chromium
- 端口 `8899`（与 Python 版互斥；并存时给 Java 加 `--server.port=8901`）

## 配置文件位置（重要）

程序用「启动目录的父目录」读取以下共享配置（与 Python 版共用）：

- `../chrome_jd/` —— 默认账号 Chrome 登录态（user-data-dir）
- `../jd_notify.json` —— Server 酱 SendKey（`{"serverchan_key":"SCT..."}`）
- `../jd_accounts.json` —— 多账号配置

因此**必须在 `jd-seckill/jd-seckill-java/` 目录内运行** jar，
使其父目录 = `jd-seckill/`，才能找到上述文件。独立部署到别处时需把这些文件放到
jar 工作目录的上级，否则登录态 / 推送配置会丢失。

## 构建与运行

```bash
# 构建（首次需联网下载 Maven 依赖）
cd jd-seckill-java
mvn clean package -DskipTests
# 生成的 jar：target/jd-seckill-java-0.0.1-SNAPSHOT.jar

# 运行（默认 8899）
java -jar target/jd-seckill-java-0.0.1-SNAPSHOT.jar
# 浏览器打开 http://127.0.0.1:8899
```

换端口（例如与 Python 版并存）：

```bash
java -jar target/jd-seckill-java-0.0.1-SNAPSHOT.jar --server.port=8901
```

> 本实现为 **Windows 专属**：`ChromeManager` 里的 Chrome 路径与进程启动（`ProcessBuilder`）
> 按 Windows 写法。要到 Linux/macOS 运行需相应调整 Chrome 路径与启动参数。

## 使用流程

与 Python 版完全一致：
1. 打开页面 → 点「启动调试 Chrome」拉起 headless Chrome 到 9222（复用 `chrome_jd` 登录态）。
2. 未登录点「去登录」扫码/短信登录京东。
3. 粘贴分享链接点「解析链接」提取 SKU，或手填 SKU/数量。
4. 点「🚀 立即抢购」自动打开结算页并真实下单；或填时间后「⏰ 定时抢购」。
5. 抢到后跳转京东收银台，并**自动推送微信**（Server 酱）；立即抢购与定时抢购均会推送。

## API 速览（与 Python 版一致）

| 接口 | 说明 |
|------|------|
| `/api/status` | 连接 / 登录 / 结算 / 收银台状态 |
| `/api/launch_chrome` | 启动调试 Chrome（可切 headless） |
| `/api/open_login` | 打开登录页 |
| `/api/checkout` | 打开并核对结算页 |
| `/api/checkout_retry` | 风控冷却后重试 |
| `/api/resolve` | 解析分享链接 → SKU |
| `/api/submit` | 立即抢购（成功后推送微信） |
| `/api/submit_schedule` | 定时抢购（预热 / 循环） |
| `/api/submit_tasks` / `/api/submit_cancel` | 任务列表 / 取消 |
| `/api/notify_config` | Server 酱配置（保存 / 测试 / 读取） |
| `/api/accounts` | 账号列表 |
| `/api/account_switch` / `account_add` / `account_logout` | 切换 / 新增 / 注销账号 |
| `/api/debug_cookies` | 调试用 cookie 列表 |

## 免责声明

仅供学习研究，遵守京东用户协议与相关法规；下单为真实交易，请自行评估风险。
