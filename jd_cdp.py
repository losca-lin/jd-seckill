"""
京东下单流程的 CDP 封装（复用已验证可靠的原生 CDP + websocket-client 方案）。

原本的 jd_direct_order.py / raw_click.py 等脚本保持不变，本模块供 jd_gui_server.py 调用。
关键点（已踩坑结论）：
- 调试 Chrome 需开 --remote-debugging-port=9222 --remote-allow-origins=*，连 ws://127.0.0.1:9222/devtools/browser/<uuid>
- 结算页底部「在线支付」按钮是 TARO-BUTTON-CORE，class 含 ActionBar_submit_*（不要用文字「在线支付」定位，会和支付方式 radio 撞）
- websocket-client 必须 suppress_origin=True 否则 Chrome 403
"""
import json
import re
import os
import shutil
import socket
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import websocket  # websocket-client

CDP_PORT = 9222
DEFAULT_SKU = "100342780502"
# 调试 Chrome 的 user-data-dir：放项目根目录，与 jd_cdp.py 同级
CHROME_USER_DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chrome_jd")

# ----------------------------------------------------------------------------
# 多账号（每个京东账号 = 一个独立的 Chrome profile 目录，登录态互不干扰）
# ----------------------------------------------------------------------------
ACCOUNTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jd_accounts.json")
DEFAULT_PROFILE = "chrome_jd"


def _default_accounts():
    return {"active": "默认账号",
            "accounts": [{"name": "默认账号", "profile": DEFAULT_PROFILE, "note": ""}]}


def load_accounts():
    if not os.path.exists(ACCOUNTS_FILE):
        return _default_accounts()
    try:
        with open(ACCOUNTS_FILE, encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d, dict) or not d.get("accounts"):
            return _default_accounts()
        names = [a.get("name") for a in d["accounts"]]
        if d.get("active") not in names:
            d["active"] = names[0]
        return d
    except Exception:
        return _default_accounts()


def save_accounts(d):
    tmp = ACCOUNTS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, ACCOUNTS_FILE)


def get_active_account():
    d = load_accounts()
    for a in d["accounts"]:
        if a["name"] == d["active"]:
            return a
    return d["accounts"][0]


def active_profile_dir():
    acc = get_active_account()
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), acc["profile"])


def list_accounts():
    d = load_accounts()
    return {"ok": True, "active": d["active"], "accounts": d["accounts"]}


def add_account(name, note=""):
    """新增一个京东账号（= 一个新的 Chrome profile 目录）。"""
    name = (name or "").strip()
    if not name:
        return {"ok": False, "error": "账号名不能为空"}
    d = load_accounts()
    if any(a["name"] == name for a in d["accounts"]):
        return {"ok": False, "error": "账号名已存在"}
    base = "chrome_jd_" + (re.sub(r"[^0-9A-Za-z一-鿿]+", "_", name).strip("_") or "acct")
    profile = base
    existing = {a["profile"] for a in d["accounts"]}
    i = 1
    while profile in existing:
        profile = f"{base}_{i}"
        i += 1
    d["accounts"].append({"name": name, "profile": profile, "note": note or ""})
    save_accounts(d)
    return {"ok": True, "name": name, "profile": profile,
            "message": f"已新增账号「{name}」，对应 Chrome 档案 {profile}。切换到它并登录京东即可。"}


def switch_account(name, headless=True):
    """切换到指定账号：保存激活态 → 关闭当前调试 Chrome → 用该账号 profile 重启。"""
    d = load_accounts()
    acc = next((a for a in d["accounts"] if a["name"] == name), None)
    if not acc:
        return {"ok": False, "error": "账号不存在"}
    d["active"] = name
    save_accounts(d)
    close_debug_chrome()
    for _ in range(20):
        time.sleep(0.3)
        if not _is_port_open():
            break
    invalidate_login_cache()  # 切换账号后登录态需重新探测
    return launch_debug_chrome(headless=headless, profile_dir=active_profile_dir())


def logout_account(name=None):
    """注销指定账号（默认当前账号）：清除该 profile 的京东登录态。

    登录态持久化在 user-data-dir，故只需删除该 profile 的 Cookies 数据库即可彻底注销。
    若注销的是当前运行中的账号，先通过 CDP 关闭调试 Chrome 以释放 Cookies 文件锁。
    """
    d = load_accounts()
    target = name or d.get("active")
    acc = next((a for a in d["accounts"] if a["name"] == target), None)
    if not acc:
        return {"ok": False, "error": "账号不存在"}
    profile_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), acc["profile"])
    if target == d.get("active"):
        try:
            close_debug_chrome()
        except Exception:
            pass
        for _ in range(20):
            time.sleep(0.3)
            if not _is_port_open():
                break
    removed = []
    for fn in ("Cookies", "Cookies-journal"):
        p = os.path.join(profile_dir, fn)
        if os.path.exists(p):
            try:
                os.remove(p)
                removed.append(fn)
            except Exception as e:
                return {"ok": False, "error": f"删除登录态失败（{fn}）: {e}"}
    invalidate_login_cache()  # 注销后登录态必为 False，立即失效缓存
    return {"ok": True, "account": acc["name"], "removed": removed,
            "message": f"已清除「{acc['name']}」的京东登录态，请重新登录"}


def _atexit_save():
    """进程退出钩子占位：登录态已持久化在 user-data-dir，无需额外落盘。
    GUI 服务端会 unregister 本函数并注册自己的 _shutdown_browser（关闭调试 Chrome）。"""
    try:
        load_accounts()
    except Exception:
        pass


# ----------------------------------------------------------------------------
# 底层
# ----------------------------------------------------------------------------
def get_browser_ws():
    raw = urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/version", timeout=5).read()
    return json.loads(raw)["webSocketDebuggerUrl"]


def list_pages():
    return json.loads(urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json", timeout=5).read())


def find_page(keyword):
    for p in list_pages():
        if p.get("type") == "page" and keyword in p.get("url", ""):
            return p
    return None


class CDPPage:
    """单个页面的 CDP 连接。"""

    def __init__(self, ws_url):
        self.ws = websocket.create_connection(ws_url, timeout=15, suppress_origin=True)
        self._id = 0
        self.send("Page.enable")
        self.send("Runtime.enable")
        self.send("Network.enable")

    def send(self, method, params=None):
        self._id += 1
        self.ws.send(json.dumps({"id": self._id, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self._id:
                return msg

    def eval(self, expr):
        return self.send("Runtime.evaluate", {"expression": expr, "returnByValue": True})

    def eval_js(self, expr):
        """执行 JS 并返回 json 值（无 returnByValue 包裹）。"""
        r = self.eval(expr)
        try:
            return r["result"]["result"]["value"]
        except Exception:
            return None

    def navigate(self, url):
        self.send("Page.navigate", {"url": url})

    def get_cookies(self):
        """返回该页作用域下的全部 cookie（含 HttpOnly）。"""
        r = self.send("Network.getCookies")
        return r.get("result", {}).get("cookies", [])

    def screenshot(self, path):
        r = self.send("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False})
        b64 = r["result"]["data"]
        import base64
        with open(path, "wb") as f:
            f.write(base64.b64decode(b64))

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def _browser_send(method, params=None):
    bw = websocket.create_connection(get_browser_ws(), timeout=25, suppress_origin=True)
    _id = [0]
    def bsend(m, p=None):
        _id[0] += 1
        bw.send(json.dumps({"id": _id[0], "method": m, "params": p or {}}))
        while True:
            x = json.loads(bw.recv())
            if x.get("id") == _id[0]:
                return x
    try:
        return bsend(method, params)
    finally:
        bw.close()


def create_target(url, background=True):
    """新建标签页。background=True 时标签在后台打开，不抢前台焦点（窗口模式下也静默）。"""
    params = {"url": url}
    if background:
        params["background"] = True
    r = _browser_send("Target.createTarget", params)
    return r["result"]["targetId"]


def close_target(target_id):
    try:
        _browser_send("Target.closeTarget", {"targetId": target_id})
    except Exception:
        pass


def page_ws_by_target(target_id, tries=12):
    for _ in range(tries):
        for p in list_pages():
            if p.get("id") == target_id and p.get("webSocketDebuggerUrl"):
                return p["webSocketDebuggerUrl"]
        time.sleep(0.3)
    return None


def open_page(url, background=True):
    """新建标签页打开 url，返回 (target_id, CDPPage)。
    background=True(默认) 时标签在后台打开，不抢前台焦点。
    """
    tid = create_target(url, background=background)
    ws = page_ws_by_target(tid)
    if not ws:
        raise RuntimeError("无法获取新标签页的 CDP 连接")
    return tid, CDPPage(ws)


# ----------------------------------------------------------------------------
# 启动调试 Chrome
# ----------------------------------------------------------------------------
def find_chrome():
    """查找 Chrome 可执行文件路径。"""
    p = shutil.which("chrome") or shutil.which("chrome.exe")
    if p:
        return p
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe"),
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None


def _is_port_open(host="127.0.0.1", port=CDP_PORT, timeout=2):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def close_debug_chrome():
    """通过 CDP 关闭整个调试 Chrome（保留 user-data-dir 里的登录态）。"""
    try:
        bw = websocket.create_connection(get_browser_ws(), timeout=10, suppress_origin=True)
        bw.send(json.dumps({"id": 1, "method": "Browser.close", "params": {}}))
        bw.close()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def restart_debug_chrome(headless=True):
    """关闭现有调试 Chrome 并按指定模式重启（登录态保留在 user-data-dir）。"""
    close_debug_chrome()
    for _ in range(20):
        time.sleep(0.3)
        if not _is_port_open():
            break
    return launch_debug_chrome(headless=headless)


def launch_debug_chrome(headless=True, profile_dir=None):
    """启动调试 Chrome（9222 端口、chrome_jd 档案）。如已在运行则直接返回。

    启动后独立于当前 Python 进程（DETACHED_PROCESS + CREATE_NEW_PROCESS_GROUP）。
    headless=True（默认）：--headless=new 无窗口后台运行，不抢焦点、不跳到前台，
        所有操作走 CDP，适合纯脚本/界面驱动。
    headless=False：有可见窗口，方便手动登录/肉眼核对。
    标志：--remote-debugging-port=9222 --remote-allow-origins=*
          --user-data-dir=chrome_jd --no-first-run --no-default-browser-check
    """
    if _is_port_open():
        return {"ok": True, "already_running": True,
                "message": f"调试 Chrome 已在 {CDP_PORT} 端口运行",
                "chrome_path": "(已在运行)", "headless": headless}
    chrome = find_chrome()
    if not chrome:
        return {"ok": False,
                "error": "未找到 chrome.exe，请确认已安装 Google Chrome(标准安装路径)"}
    profile_dir = profile_dir or active_profile_dir()
    os.makedirs(profile_dir, exist_ok=True)
    flags = [
        chrome,
        f"--remote-debugging-port={CDP_PORT}",
        "--remote-allow-origins=*",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if headless:
        # 新无头模式：无窗口、不抢占前台焦点，但仍可被 CDP 完全操控
        flags += ["--headless=new", "--disable-gpu", "--no-startup-window",
                  "--disable-backgrounding-occluded-windows",
                  "--disable-renderer-backgrounding",
                  "--disable-features=Translate,BackForwardCache"]
    else:
        # 有界面：放后台避免立即抢焦点
        flags += ["--background"]
    DETACHED_PROCESS = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    try:
        subprocess.Popen(
            flags,
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except Exception as e:
        return {"ok": False, "error": f"启动 Chrome 失败: {e}"}
    # 等端口起来
    for _ in range(25):
        time.sleep(0.4)
        if _is_port_open():
            return {"ok": True, "already_running": False,
                    "message": f"调试 Chrome 已启动（{'后台无窗口' if headless else '有窗口后台'}）→ {chrome}",
                    "chrome_path": chrome,
                    "user_data_dir": profile_dir,
                    "headless": headless}
    return {"ok": False,
            "error": "Chrome 进程已拉起但 9222 端口未在 10s 内响应，请查看任务管理器 chrome.exe"}


# ----------------------------------------------------------------------------
# 高层流程
# ----------------------------------------------------------------------------
# 登录态探测代价较高（每次都要开 m.jd.com 标签 + 等加载），故加短缓存：
# 常规 /api/status 轮询（前端每 3 秒一次）直接返回缓存，避免一直占用单线程浏览器 worker；
# 仅在 force=True（登录中轮询等待）或缓存过期（默认 15 秒）时才真正去探测。
_LOGIN_CACHE = {"val": None, "ts": 0.0}
_LOGIN_CACHE_TTL = 15.0


def invalidate_login_cache():
    """登录态可能已变化时调用：清掉缓存，下次状态查询强制重新探测。"""
    _LOGIN_CACHE["val"] = None
    _LOGIN_CACHE["ts"] = 0.0


def _probe_logged_in():
    """开 m.jd.com 探针页，等加载后读 cookie 判断是否有 pt_key。"""
    tid = create_target("https://m.jd.com")
    ws = page_ws_by_target(tid)
    pg = CDPPage(ws)
    time.sleep(1.5)
    cookies = pg.get_cookies()
    names = [c.get("name") for c in cookies]
    pg.close()
    close_target(tid)
    return "pt_key" in names


def chrome_status(force=False):
    """返回调试 Chrome 连接状态 + 登录态 + 是否有结算页/收银台。"""
    out = {"chrome_connected": False, "logged_in": False, "has_checkout": False,
           "has_payment": False, "detail": "", "active_account": get_active_account()["name"]}
    try:
        list_pages()  # 探活（一个 HTTP 到 9222，毫秒级）
        out["chrome_connected"] = True
    except Exception as e:
        out["detail"] = f"无法连接 9222: {e}"
        _LOGIN_CACHE["val"] = False
        _LOGIN_CACHE["ts"] = time.time()
        return out
    # 登录态：优先用缓存，避免每次轮询都开标签 + 等 1.5 秒
    need_probe = force or _LOGIN_CACHE["val"] is None \
        or (time.time() - _LOGIN_CACHE["ts"]) > _LOGIN_CACHE_TTL
    if need_probe:
        try:
            out["logged_in"] = _probe_logged_in()
            _LOGIN_CACHE["val"] = out["logged_in"]
            _LOGIN_CACHE["ts"] = time.time()
        except Exception as e:
            out["detail"] = f"登录态检测失败: {e}"
            # 探测失败时不刷新缓存（保留上次有效值），避免误判
    else:
        out["logged_in"] = _LOGIN_CACHE["val"]
    try:
        out["has_checkout"] = find_page("trade.m.jd.com/pay") is not None
        out["has_payment"] = find_page("mpay.m.jd.com") is not None
    except Exception:
        pass
    return out


def debug_cookies():
    """调试用：返回当前调试 Chrome 的 cookie（域名+名称），用于排查登录态。
    始终新建一个 m.jd.com 探针页读取（cookie 在 profile 内共享），读取后关闭，避免复用已失效连接。
    """
    try:
        tid = create_target("https://m.jd.com", background=True)
        ws = page_ws_by_target(tid, tries=15)
        if not ws:
            return {"ok": False, "error": "无法连接探针页", "cookies": []}
        pg = CDPPage(ws)
        time.sleep(1.5)
        cookies = pg.get_cookies()
        pg.close()
        close_target(tid)
    except Exception as e:
        return {"ok": False, "error": str(e), "cookies": []}
    out = [{"domain": c.get("domain"), "name": c.get("name"),
            "path": c.get("path"), "httpOnly": c.get("httpOnly")} for c in cookies]
    return {"ok": True, "count": len(out),
            "names": sorted({c["name"] for c in out if c.get("name")}),
            "has_pt_key": any(c.get("name") == "pt_key" for c in out),
            "cookies": out}


def _extract_sku_from_url(u):
    """从 URL 抠 SKU（/product/<n>、/item/<n>、commlist 链路等）。"""
    import re
    if not u:
        return None
    m = re.search(r"/(?:product|item(?:\.m)?)/(\d{6,})", u)
    if m:
        return m.group(1)
    m = re.search(r"commlist=(\d{6,})", u)
    if m:
        return m.group(1)
    return None


def _resolve_via_http(url):
    """纯 HTTP 跟 3.cn/u.jd.com 短链跳转，拿到最终 URL 后抠 SKU。

    京东分享短链（3.cn）服务端 302/refresh 一次就跳到 item.m.jd.com/product/<sku>.html，
    不需要登录、不需要浏览器、毫秒级。失败返回 None。
    """
    import re
    s = (url or "").strip()
    if not s:
        return None
    # 已含 SKU 形态直接抠
    sku = _extract_sku_from_url(s)
    if sku:
        return {"ok": True, "sku": sku, "from": "url_direct"}
    # 纯数字 SKU
    if re.fullmatch(r"\d{6,}", s):
        return {"ok": True, "sku": s, "from": "raw_sku"}
    # 短链/含 jd.com 的链接：HTTP 跟 302
    if "3.cn" not in s and "jd.com" not in s and "u.jd.com" not in s:
        return None  # 不是可 HTTP 解析的
    # 京东风控对 UA 路由敏感：iPhone Safari 默认落到京东验证页（要 JS），
    # desktop Chrome + Referer 可直接 302 到商品页。多 UA 轮询，任一成功即可；
    # 全失败才回退到浏览器方案。
    USER_AGENTS = [
        # desktop Chrome + 3.cn Referer —— 3.cn 短链最稳的路由
        ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
         "https://3.cn/"),
        # iPhone Safari —— 走移动 m.jd.com 跳转链
        ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
         "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 "
         "Mobile/15E148 Safari/604.1",
         None),
    ]
    for ua, referer in USER_AGENTS:
        try:
            req = urllib.request.Request(s, method="GET")
            req.add_header("User-Agent", ua)
            if referer:
                req.add_header("Referer", referer)
            r = urllib.request.urlopen(req, timeout=8)
            final = r.geturl()
            # 1) 落到京东风控验证页：商品 URL 藏在 returnurl 参数里，直接抠
            if "cfe.m.jd.com" in final or "risk_handler" in final:
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(final).query)
                ru = qs.get("returnurl", [""])[0]
                sku = _extract_sku_from_url(ru)
                if sku:
                    return {"ok": True, "sku": sku, "from": "http_risk_handler",
                            "final_url": ru}
                continue  # returnurl 也没商品信息，换 UA
            sku = _extract_sku_from_url(final)
            if sku:
                return {"ok": True, "sku": sku, "from": "http_followed",
                        "final_url": final}
            # 落地页没含 SKU，读前 4KB HTML 找 meta refresh
            try:
                body = r.read(4096).decode("utf-8", "ignore")
            except Exception:
                body = ""
            if "京东验证" in body:  # 风控页 HTML 标识（防御性）
                continue
            m = re.search(r'<meta[^>]+http-equiv=["\']refresh["\'][^>]+url=([^"\' >]+)',
                          body, re.I)
            if m:
                target = urllib.parse.urljoin(final, m.group(1).replace("&amp;", "&"))
                sku = _extract_sku_from_url(target)
                if sku:
                    return {"ok": True, "sku": sku, "from": "http_meta_refresh",
                            "final_url": target}
            # 其它非商品页（如 u.jd.com 中转），不返回，等浏览器方案处理
        except urllib.error.HTTPError as e:
            loc = e.headers.get("Location") if e.headers else None
            if loc and e.code in (301, 302, 303, 307, 308):
                try:
                    req2 = urllib.request.Request(urllib.parse.urljoin(req.full_url, loc),
                                                  method="GET")
                    req2.add_header("User-Agent", ua)
                    if referer:
                        req2.add_header("Referer", referer)
                    r2 = urllib.request.urlopen(req2, timeout=8)
                    sku = _extract_sku_from_url(r2.geturl())
                    if sku:
                        return {"ok": True, "sku": sku, "from": "http_followed",
                                "final_url": r2.geturl()}
                except Exception:
                    pass
            continue
        except Exception:
            continue
    return None


def extract_jd_url(text):
    """从整段京东分享文案中提取第一个京东链接（3.cn / jd.com / u.jd.com 等）。

    京东 App 分享出的文案形如：
      【京东】https://3.cn/xxxx?jkl=@...@ CA1507 「商品名」 点击链接直接打开...
    直接把整段文本塞给 HTTP 解析会因 URL 被后续文字污染而失败，这里先抠出干净链接。
    找不到任何链接则返回原文本（兼容直接填 SKU 数字的情况）。
    """
    import re
    if not text:
        return ""
    t = text.strip()
    # 找出所有 http/https 链接（到空白或常见中英文标点为止）
    urls = re.findall(r'https?://[^\s，。、；;：:（）()\[\]【】「」]+', t)
    for u in urls:
        if '3.cn' in u or 'jd.com' in u:
            return u
    # 退一步：含 jd.com/3.cn 但缺协议头的片段
    m = re.search(r'(?:3\.cn|[\w.-]*jd\.com)[^\s，。、；;：:（）()\[\]【】「」]+', t)
    if m:
        frag = m.group(0)
        return frag if frag.startswith('http') else 'https://' + frag
    return t


def resolve_share_link(url):
    """解析京东分享短链 → 真实商品 SKU。

    优先级：
    1) 纯 HTTP 跟 302/meta refresh 拿到商品页 URL（毫秒级、**不需登录、不需浏览器**）— 覆盖 3.cn/u.jd.com 等大多数分享链接
    2) 浏览器方案兜底（极少数需 JS 渲染的链接）

    返回 {'ok':True,'sku':...} 或 {'ok':False,'error':...}。
    """
    import re
    s = (url or "").strip()
    if not s:
        return {"ok": False, "error": "链接为空"}
    # 从整段京东分享文案中提取真正的链接（去掉【京东】前缀、商品名、引导语等噪声）
    s = extract_jd_url(s)
    # 1) 纯 HTTP 优先
    r = _resolve_via_http(s)
    if r:
        return r
    # 2) 回退浏览器方案（少数需 JS 二次跳转的链接，如部分 u.jd.com）
    if "jd.com" not in s and "3.cn" not in s and "u.jd.com" not in s:
        return {"ok": False, "error": "无法识别的链接/文本（需含 jd.com / 3.cn / u.jd.com，或直接是 SKU 数字）"}
    try:
        tid = create_target(s, background=True)
        ws = page_ws_by_target(tid)
        if not ws:
            return {"ok": False, "error": "无法打开分享链接（HTTP 也未解析到 SKU）"}
        pg = CDPPage(ws)
        sku = None
        final_url = ""
        try:
            for _ in range(20):
                time.sleep(0.8)
                try:
                    final_url = pg.eval_js("location.href") or ""
                except Exception:
                    final_url = final_url or ""  # 单次超时继续轮询
                m = re.search(r"/(?:product|item(?:\.m)?)/(\d{6,})", final_url)
                if m:
                    sku = m.group(1)
                    break
        finally:
            try:
                pg.close()
            except Exception:
                pass
            try:
                close_target(tid)
            except Exception:
                pass
        if sku:
            return {"ok": True, "sku": sku, "from": "browser", "final_url": final_url}
        return {"ok": False, "error": "短链未跳转到商品页（HTTP/浏览器均失败）"}
    except Exception as e:
        return {"ok": False, "error": f"解析失败: {e}（HTTP/浏览器均未拿到 SKU）"}


def open_login_page(sku=DEFAULT_SKU, qty=1):
    """在调试 Chrome 新建标签页打开京东登录页（带 returnurl 回跳结算）。用户手动登录。"""
    pay = f"https://trade.m.jd.com/pay?commlist={sku},,{qty},{sku},1,0,0"
    login_url = ("https://plogin.m.jd.com/login/login?appid=web&returnurl="
                 + urllib.parse.quote(pay, safe=""))
    tid, pg = open_page(login_url, background=False)  # 登录页需可见，前台打开
    time.sleep(1.0)
    url = pg.eval_js("location.href")
    pg.close()
    invalidate_login_cache()  # 打开登录页后，下次状态查询应重新探测是否登录成功
    return {"ok": True, "target_id": tid, "url": url}


def checkout(sku=DEFAULT_SKU, qty=1, keep_open=False):
    """打开/刷新结算页并解析商品信息。

    关键坑（已验证）：旧的结算页 tab 可能因之前下单/导航而进入异常态，
    其 CDP websocket 会卡死（连 `1+1` 都超时）。因此对“已打开的结算页”做
    Page.navigate 或继续复用都会阻塞。正确做法：每次先关掉旧的结算页 tab，
    再新建一个干净标签页，读完即关。
    """
    import re
    pay_url = f"https://trade.m.jd.com/pay?commlist={sku},,{qty},{sku},1,0,0"
    # 关掉旧的结算页（可能卡死），避免复用其失效连接
    for p in list_pages():
        if p.get("type") == "page" and "trade.m.jd.com/pay" in p.get("url", "") and "mpay" not in p.get("url", ""):
            try:
                close_target(p["id"])
            except Exception:
                pass
    # 新建干净标签页
    tid, pg = open_page(pay_url)
    pg.send("Network.enable")
    # 后台线程拦截结算数据接口的 JSON 响应（比解析 DOM 更稳更快）。
    # 关键坑：CDP 同一 ws 连接不能多线程并发 recv，故监听线程用「独立 CDP 连接」
    # 连到同一 target（独立 session 也能收到该 target 的 Network 事件）。
    listen_ws = page_ws_by_target(tid)
    listen_pg = CDPPage(listen_ws) if listen_ws else None
    if listen_pg:
        listen_pg.send("Network.enable")
    api_body = {"lock": threading.Lock(), "data": None, "fid": None}
    stop = {"v": False}

    # 后台线程：用独立连接监听 Network 事件，拦截结算数据接口的 JSON 响应
    def _listen():
        if not listen_pg:
            return
        lp = listen_pg
        try:
            while not stop["v"]:
                msg = lp.ws.recv()
                try:
                    m = json.loads(msg)
                except Exception:
                    continue
                meth = m.get("method", "")
                if meth == "Network.requestWillBeSent":
                    u = m["params"]["request"]["url"]
                    if "functionId=" in u:
                        fid = re.search(r"functionId=([^&]+)", u)
                        if fid and fid.group(1) in CHECKOUT_API_IDS:
                            rid = m["params"].get("requestId")
                            with api_body["lock"]:
                                api_body["fid"] = fid.group(1)
                                api_body["url"] = u
                                api_body.setdefault("_reqids", set())
                                api_body["_reqids"].add(rid)
                elif meth == "Network.loadingFinished":
                    rid = m["params"].get("requestId")
                    with api_body["lock"]:
                        reqids = api_body.get("_reqids", set())
                    if rid in reqids:
                        try:
                            rb = lp.send("Network.getResponseBody", {"requestId": rid})
                            b = rb.get("result", {}).get("body", "")
                            if b:
                                with api_body["lock"]:
                                    api_body["data"] = b
                        except Exception:
                            pass
        except Exception:
            pass
    listener = threading.Thread(target=_listen, daemon=True)
    listener.start()
    # headless=new 下 Taro 页面 innerText/textContent 可能为空（自定义元素渲染 bug），
    # 因此始终抓取完整 outerHTML，并优先用 HTML 结构解析；innerText 仅作 fallback。
    # 结算页是 SPA，商品数据异步加载，需轮询直到出现实质内容（价格/商品名），最长 ~25s。
    # 同时后台拦截数据接口 JSON（优先数据源）。
    html = ""
    txt = ""
    title = ""
    ready = False
    deadline = time.time() + 25  # 最长约 25s
    time.sleep(0.4)  # 首次给页面一点初始加载时间，然后快速轮询
    while time.time() < deadline:
        # 先查后台拦截的接口数据（本地变量，无网络开销）：到了立即返回，无需再等 DOM
        with api_body["lock"]:
            got_api = api_body.get("data")
        if got_api:
            ready = True
            break
        try:
            title = pg.eval_js("document.title") or ""
            html = pg.eval_js("document.documentElement && document.documentElement.outerHTML") or ""
            txt = pg.eval_js("document.body && document.body.innerText") or ""
        except Exception:
            time.sleep(0.25)
            continue
        if _is_risk_control(html, txt):
            break  # 风控页无需再等
        # 实质内容判定：HTML 里有价格符号且商品名类文本
        if html and len(html) > 3000:
            hsp = re.sub(r"\s+", "", html)
            if ("京东自营" in html or "合计" in html) and ("¥" in hsp or "￥" in hsp):
                ready = True
                break
        time.sleep(0.25)
    stop["v"] = True
    time.sleep(0.15)
    # 优先用接口 JSON 解析，失败回退 DOM 解析
    info = None
    with api_body["lock"]:
        raw = api_body.get("data")
        fid = api_body.get("fid")
    if raw:
        try:
            parsed = json.loads(raw)
            info = _parse_checkout_api(parsed, sku, qty)
            info["source"] = "api"
            info["api_func"] = fid
        except Exception as e:
            info = None
    if info is None or not info.get("product_name"):
        info = _parse_checkout_html(html, txt, sku, qty)
        info["source"] = "dom"
    info["title"] = title
    info["url"] = pg.eval_js("location.href")
    info["ok"] = True
    info["ready"] = ready  # 是否等到真实数据加载完成
    info["html_len"] = len(html)
    info["text_len"] = len(txt)  # 调试用：headless 下若一直=0 说明渲染未出
    # 京东风控页检测：账号被频繁访问触发「活动异常火爆」拦截，无真实结算数据
    if _is_risk_control(html, txt):
        info["risk_control"] = True
        info["product_name"] = "（账号被风控拦截）"
        info["price"] = None
        info["qty_found"] = None
        info["address_hint"] = "活动异常火爆，已优先接入快速通道，请返回上一页重新尝试"
    else:
        info["risk_control"] = False
    stop["v"] = True
    try:
        if listen_pg:
            listen_pg.close()
    except Exception:
        pass
    if keep_open:
        # 预热模式：保留结算页标签页不关闭，返回 target_id 供到点直接提交，压低开抢瞬间延迟
        return {"ok": True, "target_id": tid, "url": pay_url,
                "ready": ready, "risk_control": _is_risk_control(html, txt)}
    pg.close()
    return info


def _is_risk_control(html, txt):
    """判断结算页是否落在京东风控拦截页（无真实商品数据）。"""
    import re
    sig = "活动异常火爆"
    if sig in (html or "") or sig in (txt or ""):
        return True
    # 结构化标识：Error_text / Error_btn 样式类出现即风控页
    if "Error_text" in (html or "") and "Error_btn" in (html or ""):
        return True
    return False


def retry_checkout(sku=DEFAULT_SKU, qty=1):
    """风控/异常后重试：关闭旧结算页，重新打开并等待更久。返回 checkout() 结果。"""
    pay_url = f"https://trade.m.jd.com/pay?commlist={sku},,{qty},{sku},1,0,0"
    for p in list_pages():
        if p.get("type") == "page" and "trade.m.jd.com/pay" in p.get("url", "") and "mpay" not in p.get("url", ""):
            try:
                close_target(p["id"])
            except Exception:
                pass
    # 等风控冷却一点
    time.sleep(2.0)
    return checkout(sku, qty)


def _parse_checkout_html(html, txt, sku, qty):
    """从结算页 HTML/文本解析商品信息。

    优先用 HTML 结构（headless 下 innerText 常为空，但 DOM 完整），
    文本解析仅作兜底。风控页由调用方识别，这里只做字段提取。
    """
    import re
    # 优先从 HTML 抽可读取文本：去 style/script 后去标签
    def html_to_text(h):
        h = re.sub(r"<style[^>]*>.*?</style>", " ", h, flags=re.S)
        h = re.sub(r"<script[^>]*>.*?</script>", " ", h, flags=re.S)
        text = re.sub(r"<[^>]+>", " ", h)
        text = re.sub(r"\s+", " ", text).strip()
        return text
    src = (txt or "").strip() or html_to_text(html or "")
    src_spaced = re.sub(r"\s+", "", src)  # 去所有空白，便于匹配 ￥ 47 .41 这类带空格价格
    # 价格：优先「合计：￥X」（去掉空格后匹配），其次首个 ￥XX.XX
    price = None
    m = re.search(r"合计[：:]?[¥￥]?(\d+\.\d{2})", src_spaced)
    if m:
        price = m.group(1)
    else:
        m = re.search(r"[¥￥](\d+\.\d{2})", src_spaced)
        if m:
            price = m.group(1)
    # 数量：×N
    q = None
    m = re.search(r"[×xX](\d+)", src_spaced)
    if m:
        q = int(m.group(1))
    # 地址：含「默认」或路/大厦/小区 + 楼/室/栋 的行
    addr = None
    # 整段里截「默认 ... 电话」这一段
    m = re.search(r"默认(.{4,40}?)(?=\s*京东自营|\s*京东|\s*[A-Za-z])", src)
    if m:
        addr = ("默认" + m.group(1)).strip()
    if not addr:
        for kw in ("路", "大厦", "小区", "栋", "号楼"):
            i = src.find(kw)
            if i >= 0:
                seg = src[max(0, i - 30): i + 20]
                seg = re.sub(r"\s+", " ", seg).strip()
                if seg:
                    addr = seg; break
    # 商品名：在「京东自营」之后、第一个「￥/¥」之前的那段
    product = None
    m = re.search(r"京东自营(.{2,80}?)[¥￥]", src)
    if m:
        product = m.group(1).strip()
    if not product:
        # 兜底：找含 ml/g/套装/沐浴露 等规格词的长串
        m = re.search(r"([\u4e00-\u9fa5A-Za-z0-9]{6,60}?(?:ml|g|套装|沐浴露|洗发|洗衣液|猫粮|猫条|湿厕纸)[\u4e00-\u9fa5A-Za-z0-9]*?)", src)
        if m:
            product = m.group(1).strip()
    return {
        "sku": sku,
        "qty_input": qty,
        "qty_found": q,
        "price": price,
        "product_name": product,
        "address_hint": addr,
        "text_snippet": src[:1500],
    }


def _parse_checkout_api(data, sku, qty):
    """从结算数据接口（balance_getCurrentOrder_m）的 JSON 响应解析商品信息。

    京东结算数据真实来源是接口而非 HTML，拦截其 JSON 比解析 DOM 更稳更快。
    JD 接口 code=0 成功；data 结构多变，这里做多层兜底取值。
    """
    import re
    info = {"sku": sku, "qty_input": qty, "price": None,
            "qty_found": None, "product_name": None, "address_hint": None,
            "text_snippet": json.dumps(data, ensure_ascii=False)[:1500]}
    try:
        code = data.get("code", data.get("errcode"))
        if code not in (0, None) and "data" not in data:
            info["api_code"] = code
            return info
        d = data.get("data") or data
        # 商品名：常见路径
        product = None
        # 1) 直接字段
        for k in ("skuName", "name", "wareName", "title", "goodsName"):
            if d.get(k):
                product = d[k]; break
        # 2) 嵌套 cart/items/itemsList
        items = (d.get("items") or d.get("cartList") or d.get("itemList")
                 or d.get("itemsList") or [])
        if isinstance(items, dict):
            items = items.get("items") or items.get("list") or []
        if not product and isinstance(items, list) and items:
            it = items[0]
            if isinstance(it, dict):
                product = (it.get("skuName") or it.get("name") or it.get("wareName")
                           or it.get("goodsName") or it.get("title"))
        info["product_name"] = product
        # 数量
        if isinstance(items, list) and items:
            num = items[0].get("num") if isinstance(items[0], dict) else None
            if num is None:
                num = items[0].get("quantity") if isinstance(items[0], dict) else None
            if num is not None:
                info["qty_found"] = int(num)
        # 价格：合计/订单金额/应付
        price = (d.get("orderPrice") or d.get("payPrice") or d.get("totalPrice")
                 or d.get("price") or (items[0].get("price") if isinstance(items, list) and items and isinstance(items[0], dict) else None))
        if price is not None:
            info["price"] = str(price)
        # 地址
        addr = d.get("address") or d.get("consigneeAddr") or d.get("addr")
        if isinstance(addr, dict):
            addr = " ".join(str(addr.get(k, "")) for k in
                           ("name", "mobile", "addr", "address", "detail", "title"))
        info["address_hint"] = addr
    except Exception as e:
        info["api_error"] = str(e)
    return info


# 结算数据接口名（拦截用）：移动端下单预览/购物车
CHECKOUT_API_IDS = ("balance_getCurrentOrder_m", "balance_getCart_m",
                    "getCart", "balance_getCurrentOrder", "balance_getCart")


def submit_order(sku=DEFAULT_SKU, qty=1, ensure_checkout=True, target_id=None):
    """点击结算页底部 ActionBar_submit 提交按钮，轮询跳转到收银台。返回 orderId 等信息。

    ensure_checkout=True 时先打开/刷新结算页（保证 trade 页存在），适合定时/并发场景；
    已在界面点过「打开并核对」后也可传 False 直接复用当前结算页。
    """
    if ensure_checkout:
        try:
            checkout(sku, qty)
        except Exception as e:
            return {"ok": False, "error": f"打开结算页失败: {e}"}
    if target_id:
        page = next((p for p in list_pages() if p.get("id") == target_id), None)
    else:
        page = find_page("trade.m.jd.com/pay")
    if not page:
        return {"ok": False, "error": "未找到结算页，请先执行 checkout"}
    pg = CDPPage(page["webSocketDebuggerUrl"])
    if target_id:
        # 预热页在开抢前已打开，提交前刷新到 20:00 最新态（确保库存/提交按钮为放货后状态）
        try:
            pg.send("Page.reload")
            time.sleep(1.5)
        except Exception:
            pass
    # 记录点击前已存在的收银台页面 URL：避免把「历史残留的收银台」误判为本单成功。
    # 京东下单成功后才会打开 mpay 收银台；若浏览器里本来就有 mpay 页面（之前下单/手机同步/手动开过），
    # 全局 find_page 会立即命中它，导致「没真下单却报告成功、且订单号是别人的旧单」。
    # 因此本次只认「点击提交后新出现的」mpay 页面（URL 不在 baseline 中）。
    trade_tid = page["id"]
    baseline_mpay = {p.get("url") for p in list_pages()
                     if "mpay.m.jd.com" in (p.get("url") or "")}
    # 取提交按钮坐标
    btn = pg.eval_js("""(() => {
        // 快路径：原生 CSS 选择器一步定位，避免 [...querySelectorAll('*')] 全页遍历。
        // 注意不能用按钮文字「在线支付」定位：新版京东把文字放进 TARO-TEXT-CORE 子元素，按钮 innerText 为空，
        // 故以 class 含 ActionBar_submit 为准（与原策略1/2 等价）。
        let e=document.querySelector('taro-button-core[class*="ActionBar_submit"]')
            || document.querySelector('[class*="ActionBar_submit"]');
        if(!e){
            // 慢路径兜底：全页遍历
            const all=[...document.querySelectorAll('*')];
            // 底部 ActionBar_buttons 容器内文字含「在线支付」
            e=all.find(el=>{
                const c=(el.className||'').toString();
                const t=(el.textContent||'').trim();
                return c.includes('ActionBar_buttons') && t.includes('在线支付');
            });
            // 全页可见的、文字含「在线支付」的可点击元素
            if(!e) e=all.find(el=>{
                const t=(el.tagName||'');
                const txt=(el.textContent||'').trim();
                return (t==='TARO-BUTTON-CORE'||t==='BUTTON'||t==='A') && txt.includes('在线支付') && el.offsetParent!==null;
            });
        }
        if(!e) return {ok:false};
        e.scrollIntoView({block:'center'});
        const r=e.getBoundingClientRect();
        return {ok:true, x:r.left+r.width/2, y:r.top+r.height/2, w:r.width, h:r.height,
                tag:e.tagName, cls:(e.className||'').toString().slice(0,60), txt:(e.textContent||'').trim().slice(0,30)};
    })()""")
    if not (btn and btn.get("ok")):
        pg.close()
        return {"ok": False, "error": "未找到 ActionBar_submit 提交按钮"}
    x, y = int(btn["x"]), int(btn["y"])
    import re
    # 1) 优先用真实指针事件序列（含 mouseMoved 前置，提升命中率）
    try:
        pg.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y, "button": "left"})
        pg.send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1})
        pg.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1})
    except Exception:
        pass
    # 2) 快速轮询 ~1.2s：跳转通常 <1s 完成，检测到立即进入取单号阶段；
    #    超时未跳再用 DOM .click() 兜底（Taro 按钮的 onClick 绑定在内部 <button> 上，坐标偶发落空时保底）
    jumped = False
    for _ in range(8):
        time.sleep(0.15)
        mp = find_page("mpay.m.jd.com")
        if mp and mp.get("url") not in baseline_mpay:
            jumped = True
            break
    if not jumped:
        try:
            pg.eval_js("""(() => {
                let e=document.querySelector('taro-button-core[class*="ActionBar_submit"]')
                    || document.querySelector('[class*="ActionBar_submit"]');
                if(!e) return;
                const real = e.querySelector('button, [class*="btn"], a') || e;
                try { real.click(); } catch(_) { e.click(); }
            })()""")
        except Exception:
            pass
    # 3) 轮询跳转：以浏览器级 /json/list（find_page）为准，免疫页面 websocket 跨域重置
    order_id = None
    pay_url = ""
    for _ in range(80):  # 80 * 0.25s ≈ 20s
        time.sleep(0.25)
        # 只认点击提交后「新出现」的收银台（URL 不在点击前的 baseline 中），
        # 否则浏览器里残留的旧收银台会被误判为本单成功。
        mp = find_page("mpay.m.jd.com")
        if mp and mp.get("url") not in baseline_mpay:
            pay_url = mp.get("url", "")
            m = re.search(r"orderId=([0-9]+)", pay_url)
            if m:
                order_id = m.group(1)
            break
        # 兜底：trade 结算页自身导航到 mpay（同标签页提交，URL 变为新收银台）
        try:
            url = pg.eval_js("location.href") or ""
        except Exception:
            url = ""
        if "mpay.m.jd.com" in url and url not in baseline_mpay:
            pay_url = url
            m = re.search(r"orderId=([0-9]+)", url)
            if m:
                order_id = m.group(1)
            break
        # 兜底2：页面文本里出现 orderId（导航瞬间 websocket 可能断开，忽略异常）
        try:
            txt = pg.eval_js("document.body.innerText") or ""
        except Exception:
            txt = ""
        if "trade.m.jd.com/pay" not in url:  # 仅当本标签页已离开结算页时才采信文本
            m = re.search(r"orderId[=:]\s*([0-9]+)", txt)
            if m:
                order_id = m.group(1)
                pay_url = url
                break
    if not pay_url:
        try:
            mp = find_page("mpay.m.jd.com")
            if mp and mp.get("url") not in baseline_mpay:
                pay_url = mp.get("url", "")
        except Exception:
            pass
    try:
        pg.close()
    except Exception:
        pass
    if order_id:
        return {"ok": True, "order_id": order_id, "payment_url": pay_url}
    return {"ok": False, "error": "点击提交后未生成新订单（未跳转到收银台）。可能原因：秒杀已无库存 / 京东风控拦截 / 需先勾选购买协议。请查看浏览器结算页实际状态", "payment_url": pay_url}


def current_shop_page():
    """返回当前最相关的购物页（优先收银台，其次结算页）。"""
    for kw in ("mpay.m.jd.com", "trade.m.jd.com/pay"):
        p = find_page(kw)
        if p:
            return p
    return None


def screenshot_page(keyword=None):
    """对指定(或当前购物)页截图，返回 base64 PNG。用于 GUI 预览，避免盲操作。"""
    page = find_page(keyword) if keyword else current_shop_page()
    if not page:
        return {"ok": False, "error": "未找到购物页面（结算页/收银台），请先 checkout 或 submit"}
    pg = CDPPage(page["webSocketDebuggerUrl"])
    try:
        # 等待渲染稳定
        time.sleep(0.8)
        r = pg.send("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False})
        b64 = r["result"]["data"]
        return {"ok": True, "img": b64, "url": pg.eval_js("location.href")}
    finally:
        pg.close()


def pay(method=None):
    page = find_page("mpay.m.jd.com")
    if not page:
        # 也可能还在 trade 页（下单未跳），尝试再 submit
        return {"ok": False, "error": "未找到收银台页面(mpay.m.jd.com)，可能订单尚未提交"}
    pg = CDPPage(page["webSocketDebuggerUrl"])
    time.sleep(1.5)
    # 选支付方式
    if method and method != "default":
        label = {"zhaoshang": "招商银行信用卡", "jiaohang": "交通银行信用卡",
                 "wechat": "微信支付"}.get(method)
        if label:
            clicked = pg.eval_js(f"""(() => {{
                const els=[...document.querySelectorAll('*')].filter(e=>(e.innerText||'').includes({label!r}));
                if(!els.length) return false;
                const e=els[0];
                const r=e.getBoundingClientRect();
                return true; // 仅定位
            }})()""")
            # 用真实鼠标点击该元素中心
            pos = pg.eval_js(f"""(() => {{
                const els=[...document.querySelectorAll('*')].filter(e=>(e.innerText||'').includes({label!r}));
                if(!els.length) return null;
                const r=els[0].getBoundingClientRect();
                return {{x:r.left+r.width/2, y:r.top+r.height/2}};
            }})()""")
            if pos:
                pg.send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": pos["x"], "y": pos["y"], "button": "left", "clickCount": 1})
                pg.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": pos["x"], "y": pos["y"], "button": "left", "clickCount": 1})
                time.sleep(0.6)
    # 点确认付款
    pos = pg.eval_js("""(() => {
        const els=[...document.querySelectorAll('*')].filter(e=>(e.innerText||'').trim().includes('确认付款'));
        if(!els.length) return null;
        const r=els[0].getBoundingClientRect();
        return {x:r.left+r.width/2, y:r.top+r.height/2};
    })()""")
    result = {"ok": True, "method": method or "default"}
    if pos:
        pg.send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": pos["x"], "y": pos["y"], "button": "left", "clickCount": 1})
        pg.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": pos["x"], "y": pos["y"], "button": "left", "clickCount": 1})
        result["clicked_confirm"] = True
    else:
        result["clicked_confirm"] = False
        result["note"] = "未找到「确认付款」按钮"
    time.sleep(1.5)
    txt = pg.eval_js("document.body.innerText") or ""
    result["page_text"] = txt[:600]
    pg.close()
    return result


if __name__ == "__main__":
    print(json.dumps(chrome_status(), ensure_ascii=False, indent=2))
