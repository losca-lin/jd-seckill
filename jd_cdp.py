# -*- coding: utf-8 -*-
"""
京东抢购助手 —— 浏览器驱动核心（Playwright 版，方案②：单进程多 Context 隔离）

相比旧版（每个账号一个 Chrome user-data-dir，切换要重启浏览器、占用 GB 级磁盘），
本版用 Playwright 在同一浏览器进程内为每个京东账号创建一个独立 BrowserContext：
  * 登录态以轻量 storage_state JSON（cookies/localStorage）持久化到 states/<账号>.json
  * 切换账号 = 切换/新建 context，毫秒级，不重启浏览器进程
  * 单份浏览器二进制，磁盘占用极小

对外函数签名尽量与旧 CDP 版兼容，便于 jd_gui_server.py 直接调用。
"""
import os
import re
import json
import time
import atexit
import threading
import urllib.request
import urllib.parse
import urllib.error

from playwright.sync_api import sync_playwright, Browser, BrowserContext, Page

# ---------------------------------------------------------------------------
# 路径与常量
# ---------------------------------------------------------------------------
BASE = os.path.dirname(os.path.abspath(__file__))
ACCOUNTS_FILE = os.path.join(BASE, "jd_accounts.json")
STATES_DIR = os.path.join(BASE, "states")          # 每个账号一份登录态 JSON
DEFAULT_SKU = "100342780502"
CURRENT_HEADLESS = {"val": True}                   # 当前 headless 模式（server 共享）

CHECKOUT_API_IDS = ("balance_getCurrentOrder_m", "balance_getCart_m",
                    "getCart", "balance_getCurrentOrder", "balance_getCart")


# ---------------------------------------------------------------------------
# 多账号配置（仅登记账号名，不再绑定 Chrome profile 目录）
# ---------------------------------------------------------------------------
def _default_accounts():
    return {"active": "默认账号",
            "accounts": [{"name": "默认账号", "note": ""}]}


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


def list_accounts():
    d = load_accounts()
    return {"ok": True, "active": d["active"], "accounts": d["accounts"]}


def add_account(name, note=""):
    name = (name or "").strip()
    if not name:
        return {"ok": False, "error": "账号名不能为空"}
    d = load_accounts()
    if any(a["name"] == name for a in d["accounts"]):
        return {"ok": False, "error": "账号名已存在"}
    d["accounts"].append({"name": name, "note": note or ""})
    save_accounts(d)
    return {"ok": True, "name": name,
            "message": f"已新增账号「{name}」。切换到它并在窗口登录京东即可，登录态会存为 states/{_safe(name)}.json"}


def _safe(name):
    return re.sub(r"[^0-9A-Za-z一-鿿]+", "_", name or "").strip("_") or "acct"


def state_path(name):
    return os.path.join(STATES_DIR, _safe(name) + ".json")


# ---------------------------------------------------------------------------
# 浏览器 / 多 Context 管理（单进程）
# ---------------------------------------------------------------------------
_pw = None            # sync_playwright 实例
_browser = None       # Browser 实例（单进程）
_contexts = {}        # name -> BrowserContext
_pages = {}           # target_id -> Page


def ensure_browser(headless=True):
    global _pw, _browser
    if _browser is not None and _browser.is_connected():
        return _browser
    _pw = sync_playwright().start()
    _browser = _pw.chromium.launch(
        channel="chrome",                 # 复用本机已装 Chrome，免下载 Chromium
        headless=headless,
        args=[
            "--disable-blink-features=AutomationControlled",  # 隐藏 webdriver 痕迹
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-dev-shm-usage",
        ],
    )
    return _browser


def _stealth(ctx):
    # 进一步抹掉自动化特征
    ctx.add_init_script(
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        "try{window.chrome=window.chrome||{runtime:{}};}catch(e){}"
    )


def get_context(name=None, headless=True):
    name = name or get_active_account()["name"]
    ctx = _contexts.get(name)
    if ctx is not None and not ctx.is_closed:
        return ctx
    sp = state_path(name)
    kw = {}
    if os.path.exists(sp):
        kw["storage_state"] = sp           # 恢复登录态
    ctx = ensure_browser(headless).new_context(**kw)
    _stealth(ctx)
    _contexts[name] = ctx
    return ctx


def save_state(name=None):
    name = name or get_active_account()["name"]
    ctx = _contexts.get(name)
    if ctx is None or ctx.is_closed:
        return
    os.makedirs(STATES_DIR, exist_ok=True)
    try:
        ctx.storage_state(path=state_path(name))
    except Exception:
        pass


def close_context(name):
    ctx = _contexts.pop(name, None)
    if ctx is None:
        return
    try:
        ctx.storage_state(path=state_path(name))    # 关闭前保存登录态
    except Exception:
        pass
    try:
        ctx.close()
    except Exception:
        pass


def switch_account(name, headless=True):
    """切换到指定账号：毫秒级，仅切 context，不重启浏览器进程。"""
    d = load_accounts()
    acc = next((a for a in d["accounts"] if a["name"] == name), None)
    if not acc:
        return {"ok": False, "error": "账号不存在"}
    save_state()                                   # 保存当前账号登录态
    d["active"] = name
    save_accounts(d)
    CURRENT_HEADLESS["val"] = headless
    get_context(name, headless)                    # 懒加载新 context（若已有则复用）
    return {"ok": True, "message": f"已切换到「{name}」（毫秒级，不重启浏览器）", "active": name}


def logout_account(name=None):
    """注销当前（或指定）账号：关闭其 context 并清除本地登录态文件。"""
    name = name or get_active_account()["name"]
    close_context(name)
    sp = state_path(name)
    if os.path.exists(sp):
        try:
            os.remove(sp)
        except Exception:
            pass
    return {"ok": True, "message": f"已注销「{name}」的登录态（本地凭据已清除）", "name": name}


# ---- 兼容层：让上层 CDP 风格 API 继续可用（底层换成 Playwright Page）----
class CDPPage:
    def __init__(self, page):
        self.page = page
        self._id = 0

    def send(self, method, params=None):
        return {}                                 # Playwright 下无需实现 CDP 命令

    def eval(self, expr):
        return self.eval_js(expr)

    def eval_js(self, expr):
        try:
            return self.page.evaluate(expr)
        except Exception:
            return None

    def navigate(self, url):
        try:
            self.page.goto(url, timeout=30000)
        except Exception:
            pass

    def get_cookies(self):
        try:
            return self.page.context.cookies()
        except Exception:
            return []

    def screenshot(self, path):
        try:
            self.page.screenshot(path=path)
        except Exception:
            pass

    def close(self):
        try:
            self.page.close()
        except Exception:
            pass


def _tid(page):
    return str(id(page))


def create_target(url, background=True):
    ctx = get_context()
    page = ctx.new_page()
    tid = _tid(page)
    _pages[tid] = page
    if url:
        try:
            page.goto(url, timeout=30000)
        except Exception:
            pass
    return tid


def page_ws_by_target(tid):
    return tid                                      # 兼容：以 tid 充当 ws 标识


def open_page(url, background=True):
    tid = create_target(url, background)
    return tid, CDPPage(_pages[tid])


def close_target(tid):
    pg = _pages.pop(tid, None)
    if pg is not None:
        try:
            pg.close()
        except Exception:
            pass


def list_pages():
    out = []
    for name, ctx in list(_contexts.items()):
        if ctx.is_closed:
            continue
        for pg in ctx.pages:
            tid = _tid(pg)
            _pages.setdefault(tid, pg)
            out.append({"id": tid, "type": "page", "url": pg.url or "", "name": name})
    return out


def find_page(keyword):
    for p in list_pages():
        if p.get("type") == "page" and keyword in (p.get("url") or ""):
            return p
    return None


def launch_debug_chrome(headless=True):
    ensure_browser(headless)
    get_context(get_active_account()["name"], headless)
    CURRENT_HEADLESS["val"] = headless
    return {"ok": True, "message": "浏览器已启动（Playwright + 本机 Chrome）",
            "headless": headless, "already_running": False}


def restart_debug_chrome(headless=True):
    close_debug_chrome()
    return launch_debug_chrome(headless)


def close_debug_chrome():
    global _browser, _pw
    for name in list(_contexts.keys()):
        close_context(name)
    if _browser is not None:
        try:
            _browser.close()
        except Exception:
            pass
        _browser = None
    if _pw is not None:
        try:
            _pw.stop()
        except Exception:
            pass
        _pw = None
    return {"ok": True}


@atexit.register
def _atexit_save():
    for name in list(_contexts.keys()):
        save_state(name)


# ---------------------------------------------------------------------------
# 链接解析（与旧版一致，浏览器兜底走兼容层）
# ---------------------------------------------------------------------------
def _extract_sku_from_url(u):
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
    """纯 HTTP 跟 3.cn/u.jd.com 短链跳转，拿到最终 URL 后抠 SKU。"""
    s = (url or "").strip()
    if not s:
        return None
    sku = _extract_sku_from_url(s)
    if sku:
        return {"ok": True, "sku": sku, "from": "url_direct"}
    if re.fullmatch(r"\d{6,}", s):
        return {"ok": True, "sku": s, "from": "raw_sku"}
    if "3.cn" not in s and "jd.com" not in s and "u.jd.com" not in s:
        return None
    USER_AGENTS = [
        ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
         "https://3.cn/"),
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
            if "cfe.m.jd.com" in final or "risk_handler" in final:
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(final).query)
                ru = qs.get("returnurl", [""])[0]
                sku = _extract_sku_from_url(ru)
                if sku:
                    return {"ok": True, "sku": sku, "from": "http_risk_handler",
                            "final_url": ru}
                continue
            sku = _extract_sku_from_url(final)
            if sku:
                return {"ok": True, "sku": sku, "from": "http_followed",
                        "final_url": final}
            try:
                body = r.read(4096).decode("utf-8", "ignore")
            except Exception:
                body = ""
            if "京东验证" in body:
                continue
            m = re.search(r'<meta[^>]+http-equiv=["\']refresh["\'][^>]+url=([^"\' >]+)',
                          body, re.I)
            if m:
                target = urllib.parse.urljoin(final, m.group(1).replace("&amp;", "&"))
                sku = _extract_sku_from_url(target)
                if sku:
                    return {"ok": True, "sku": sku, "from": "http_meta_refresh",
                            "final_url": target}
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
    """从整段京东分享文案中提取第一个京东链接（3.cn / jd.com / u.jd.com 等）。"""
    if not text:
        return ""
    t = text.strip()
    urls = re.findall(r'https?://[^\s，。、；;：:（）()\[\]【】「」]+', t)
    for u in urls:
        if '3.cn' in u or 'jd.com' in u:
            return u
    m = re.search(r'(?:3\.cn|[\w.-]*jd\.com)[^\s，。、；;：:（）()\[\]【】「」]+', t)
    if m:
        frag = m.group(0)
        return frag if frag.startswith('http') else 'https://' + frag
    return t


def resolve_share_link(url):
    """解析京东分享短链 → 真实商品 SKU。优先纯 HTTP，浏览器兜底。"""
    s = (url or "").strip()
    if not s:
        return {"ok": False, "error": "链接为空"}
    s = extract_jd_url(s)
    r = _resolve_via_http(s)
    if r:
        return r
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
                    final_url = final_url or ""
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


# ---------------------------------------------------------------------------
# 登录态检测 / 登录页
# ---------------------------------------------------------------------------
def chrome_status():
    out = {"chrome_connected": False, "logged_in": False, "has_checkout": False,
           "has_payment": False, "detail": "", "active_account": get_active_account()["name"]}
    if _browser is None or not _browser.is_connected():
        out["detail"] = "浏览器未启动"
        return out
    out["chrome_connected"] = True
    try:
        ctx = get_context()
        pg = ctx.new_page()
        try:
            pg.goto("https://m.jd.com", timeout=20000)
            time.sleep(1.0)
            cookies = pg.context.cookies()
            names = [c["name"] for c in cookies]
            out["logged_in"] = "pt_key" in names
            if out["logged_in"]:
                save_state()                       # 登录成功后自动落盘登录态
        finally:
            try:
                pg.close()
            except Exception:
                pass
    except Exception as e:
        out["detail"] = f"登录态检测失败: {e}"
    try:
        out["has_checkout"] = find_page("trade.m.jd.com/pay") is not None
        out["has_payment"] = find_page("mpay.m.jd.com") is not None
    except Exception:
        pass
    return out


def open_login_page(sku=DEFAULT_SKU, qty=1):
    pay = f"https://trade.m.jd.com/pay?commlist={sku},,{qty},{sku},1,0,0"
    login_url = ("https://plogin.m.jd.com/login/login?appid=web&returnurl="
                 + urllib.parse.quote(pay, safe=""))
    ctx = get_context()
    page = ctx.new_page()
    tid = _tid(page)
    _pages[tid] = page
    try:
        page.goto(login_url, timeout=30000)
    except Exception:
        pass
    return {"ok": True, "message": "已打开京东登录页，请在窗口完成登录（含验证码）", "tid": tid}


# ---------------------------------------------------------------------------
# 结算页解析
# ---------------------------------------------------------------------------
def _extract_phone(text):
    if not text:
        return None
    m = re.search(r"(?<!\d)(1[3-9]\d{9})(?!\d)", text)
    return m.group(1) if m else None


def _is_risk_control(html, txt):
    sig = "活动异常火爆"
    if sig in (html or "") or sig in (txt or ""):
        return True
    if "Error_text" in (html or "") and "Error_btn" in (html or ""):
        return True
    return False


def _parse_checkout_html(html, txt, sku, qty):
    def html_to_text(h):
        h = re.sub(r"<style[^>]*>.*?</style>", " ", h, flags=re.S)
        h = re.sub(r"<script[^>]*>.*?</script>", " ", h, flags=re.S)
        text = re.sub(r"<[^>]+>", " ", h)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    src = (txt or "").strip() or html_to_text(html or "")
    src_spaced = re.sub(r"\s+", "", src)
    price = None
    m = re.search(r"合计[：:]?[¥￥]?(\d+\.\d{2})", src_spaced)
    if m:
        price = m.group(1)
    else:
        m = re.search(r"[¥￥](\d+\.\d{2})", src_spaced)
        if m:
            price = m.group(1)
    q = None
    m = re.search(r"[×xX](\d+)", src_spaced)
    if m:
        q = int(m.group(1))
    addr = None
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
                    addr = seg
                    break
    product = None
    m = re.search(r"京东自营(.{2,80}?)[¥￥]", src)
    if m:
        product = m.group(1).strip()
    if not product:
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
        "phone": _extract_phone(src) or _extract_phone(addr or ""),
        "text_snippet": src[:1500],
    }


def _parse_checkout_api(data, sku, qty):
    info = {"sku": sku, "qty_input": qty, "price": None,
            "qty_found": None, "product_name": None, "address_hint": None,
            "phone": None,
            "text_snippet": json.dumps(data, ensure_ascii=False)[:1500]}
    try:
        code = data.get("code", data.get("errcode"))
        if code not in (0, None) and "data" not in data:
            info["api_code"] = code
            return info
        d = data.get("data") or data
        product = None
        for k in ("skuName", "name", "wareName", "title", "goodsName"):
            if d.get(k):
                product = d[k]
                break
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
        if isinstance(items, list) and items:
            num = items[0].get("num") if isinstance(items[0], dict) else None
            if num is None:
                num = items[0].get("quantity") if isinstance(items[0], dict) else None
            if num is not None:
                info["qty_found"] = int(num)
        price = (d.get("orderPrice") or d.get("payPrice") or d.get("totalPrice")
                 or d.get("price") or (items[0].get("price") if isinstance(items, list) and items and isinstance(items[0], dict) else None))
        if price is not None:
            info["price"] = str(price)
        addr = d.get("address") or d.get("consigneeAddr") or d.get("addr")
        if isinstance(addr, dict):
            addr = " ".join(str(addr.get(k, "")) for k in
                           ("name", "mobile", "addr", "address", "detail", "title"))
        info["address_hint"] = addr
        info["phone"] = _extract_phone(addr or "")
    except Exception as e:
        info["api_error"] = str(e)
    return info


def checkout(sku=DEFAULT_SKU, qty=1):
    """打开/刷新结算页并解析商品信息（Playwright 版）。"""
    pay_url = f"https://trade.m.jd.com/pay?commlist={sku},,{qty},{sku},1,0,0"
    # 关掉旧的结算页（可能进入异常态），避免复用
    for p in list_pages():
        if p.get("type") == "page" and "trade.m.jd.com/pay" in (p.get("url") or "") and "mpay" not in (p.get("url") or ""):
            try:
                close_target(p["id"])
            except Exception:
                pass
    ctx = get_context()
    page = ctx.new_page()
    tid = _tid(page)
    _pages[tid] = page
    captured = {}

    def _on_resp(r):
        u = r.url
        if "functionId=" in u and any(fid in u for fid in CHECKOUT_API_IDS):
            try:
                captured["raw"] = r.body().decode("utf-8", "ignore")
            except Exception:
                pass

    page.on("response", _on_resp)
    html, txt, title = "", "", ""
    ready = False
    try:
        page.goto(pay_url, timeout=30000, wait_until="domcontentloaded")
    except Exception:
        pass
    for i in range(25):                       # 最多约 25s 等数据/风控
        time.sleep(1.0)
        try:
            title = page.title()
            html = page.evaluate("() => document.documentElement && document.documentElement.outerHTML") or ""
            txt = page.evaluate("() => document.body && document.body.innerText") or ""
        except Exception:
            pass
        if captured.get("raw"):
            ready = True
            break
        if _is_risk_control(html, txt):
            break
        if html and len(html) > 3000:
            hsp = re.sub(r"\s+", "", html)
            if ("京东自营" in html or "合计" in html) and ("¥" in hsp or "￥" in hsp):
                ready = True
                break
    info = None
    raw = captured.get("raw")
    if raw:
        try:
            parsed = json.loads(raw)
            info = _parse_checkout_api(parsed, sku, qty)
            info["source"] = "api"
        except Exception:
            info = None
    if info is None or not info.get("product_name"):
        info = _parse_checkout_html(html, txt, sku, qty)
        info["source"] = "dom"
    if not info.get("phone"):
        info["phone"] = _extract_phone(txt) or _extract_phone(html)
    info["title"] = title
    info["url"] = page.url
    info["ok"] = True
    info["ready"] = ready
    info["html_len"] = len(html)
    info["text_len"] = len(txt)
    info["risk_control"] = _is_risk_control(html, txt)
    if info["risk_control"]:
        info["product_name"] = "（账号被风控拦截）"
        info["price"] = None
        info["qty_found"] = None
        info["address_hint"] = "活动异常火爆，已优先接入快速通道，请返回上一页重新尝试"
    return info


def retry_checkout(sku=DEFAULT_SKU, qty=1):
    """风控/异常后重试：关闭旧结算页，重新打开并等待更久。"""
    pay_url = f"https://trade.m.jd.com/pay?commlist={sku},,{qty},{sku},1,0,0"
    for p in list_pages():
        if p.get("type") == "page" and "trade.m.jd.com/pay" in (p.get("url") or "") and "mpay" not in (p.get("url") or ""):
            try:
                close_target(p["id"])
            except Exception:
                pass
    time.sleep(2.0)
    return checkout(sku, qty)


def current_shop_page():
    """返回当前最相关的购物页（优先收银台，其次结算页）。"""
    for kw in ("mpay.m.jd.com", "trade.m.jd.com/pay"):
        p = find_page(kw)
        if p:
            return p
    return None


def submit_order(sku=DEFAULT_SKU, qty=1, ensure_checkout=True):
    """点击结算页底部 ActionBar_submit 提交按钮，轮询跳转到收银台。"""
    if ensure_checkout:
        try:
            checkout(sku, qty)
        except Exception as e:
            return {"ok": False, "error": f"打开结算页失败: {e}"}
    page = None
    for p in list_pages():
        if "trade.m.jd.com/pay" in (p.get("url") or "") and "mpay" not in (p.get("url") or ""):
            page = _pages.get(p["id"])
            break
    if page is None:
        return {"ok": False, "error": "未找到结算页，请先执行 checkout"}

    DOM_CLICK = """() => {
        const all=[...document.querySelectorAll('*')];
        let e=all.find(el=>{const c=(el.className||'').toString(); return el.tagName==='TARO-BUTTON-CORE' && c.includes('ActionBar_submit');});
        if(!e) e=all.find(el=>{const c=(el.className||'').toString(); return c.includes('ActionBar_submit');});
        if(!e) e=all.find(el=>{const c=(el.className||'').toString(); return c.includes('ActionBar_buttons') && (el.textContent||'').includes('在线支付');});
        if(!e) e=all.find(el=>{const t=el.tagName||''; const txt=(el.textContent||'').trim(); return (t==='TARO-BUTTON-CORE'||t==='BUTTON'||t==='A') && txt.includes('在线支付') && el.offsetParent!==null;});
        if(!e) return;
        const real=e.querySelector('button,[class*=btn],a')||e;
        try{real.click();}catch(_){e.click();}
    }"""
    try:
        page.locator('taro-button-core.ActionBar_submit').first.click(timeout=3000)
    except Exception:
        try:
            page.evaluate(DOM_CLICK)
        except Exception:
            return {"ok": False, "error": "未找到 ActionBar_submit 提交按钮"}

    time.sleep(1.5)                            # 等跳转发起
    order_id = None
    pay_url = ""
    for _ in range(40):                        # 40 * 0.7s ≈ 28s
        time.sleep(0.7)
        mp = find_page("mpay.m.jd.com")
        if mp:
            pay_url = mp.get("url", "")
            m = re.search(r"orderId=([0-9]+)", pay_url)
            if m:
                order_id = m.group(1)
            break
        try:
            url = page.url or ""
        except Exception:
            url = ""
        if "mpay.m.jd.com" in url:
            pay_url = url
            m = re.search(r"orderId=([0-9]+)", url)
            if m:
                order_id = m.group(1)
            break
    save_state()                               # 提交后保存登录态
    if order_id:
        return {"ok": True, "order_id": order_id, "payment_url": pay_url}
    return {"ok": False, "error": "点击后未检测到收银台跳转，请检查浏览器", "payment_url": pay_url}
