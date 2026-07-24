"""
京东抢购 GUI 服务（本地网页界面）。

运行：python jd_gui_server.py  →  浏览器打开 http://127.0.0.1:8899
依赖：仅标准库 + websocket-client（已在 venv 中）。
底层下单逻辑见 jd_cdp.py（原生 CDP 方案，已验证可靠）。
原脚本 jd_direct_order.py / raw_click.py / open_login.py 等保持不变。

新增能力（用户要求）：
- ④ 提交订单：线程池执行 + 固定时间点定时提交 + 页面可配置参数（SKU/数量/并发数/重试/定时时间）
- ① 启动调试 Chrome 按钮（未连接时一键拉起）
- 已移除 ⑤确认付款、⑥页面预览（用户在手机端付款）
"""
import atexit
import json
import os
import queue
import threading
import time
import concurrent.futures
import urllib.request
import urllib.parse
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import jd_cdp as jd
import logging

# ---- 请求级日志：同时写文件 jd_gui.log 与 stdout，便于排查 ----
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler("jd_gui.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("jd_gui")


# ---- 手机提醒（Server 酱 / 方糖）：抢到后推送微信 ----
# 配置存 jd_notify.json（不入库），结构：{"serverchan_key": "SCTxxxxx"}
NOTIFY_CFG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jd_notify.json")
NOTIFY_CFG = {"serverchan_key": ""}


def _load_notify_cfg():



    global NOTIFY_CFG
    try:
        if os.path.exists(NOTIFY_CFG_PATH):
            with open(NOTIFY_CFG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            if isinstance(cfg, dict):
                NOTIFY_CFG.update({k: cfg[k] for k in ("serverchan_key",) if k in cfg})
    except Exception as e:
        log.warning("读取 jd_notify.json 失败：%s", e)


def _save_notify_cfg(cfg):
    global NOTIFY_CFG
    NOTIFY_CFG.update({k: cfg.get(k, NOTIFY_CFG.get(k, "")) for k in ("serverchan_key",)})
    try:
        with open(NOTIFY_CFG_PATH, "w", encoding="utf-8") as f:
            json.dump(NOTIFY_CFG, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning("写入 jd_notify.json 失败：%s", e)


def notify(title, text):
    """抢到后调用，推送微信（Server 酱）。失败静默不影响主流程。"""
    key = NOTIFY_CFG.get("serverchan_key", "").strip()
    if not key:
        return {"ok": False, "error": "未配置 serverchan_key"}
    try:
        url = f"https://sctapi.ftqq.com/{key}.send"
        data = urllib.parse.urlencode({"title": title, "desp": text}).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        # sctapi.ftqq.com 是公网，走系统默认代理出网（本机代理环境变量）
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8", "ignore")
        try:
            j = json.loads(body)
            ok = bool(j.get("code") == 0 or j.get("data"))
        except Exception:
            ok = ("ok" in body)
        log.info("Server 酱推送 %s：%s", "成功" if ok else "失败", body[:120])
        return {"ok": ok, "raw": body[:200]}
    except Exception as e:
        log.warning("Server 酱推送异常：%s", e)
        return {"ok": False, "error": str(e)}


_load_notify_cfg()


def _notify_success(task, res):
    """抢到后组装并推送微信提醒。"""
    order_id = res.get("order_id") or res.get("orderId") or ""
    sku = task.get("sku", "")
    qty = task.get("qty", 1)
    at_str = task.get("at_str", "")
    desp = (
        f"🎉 抢到了！\n"
        f"商品 SKU：{sku}\n"
        f"数量：{qty}\n"
        f"订单号：{order_id}\n"
        f"定时：{at_str}\n"
        f"请到京东 App「待支付」30 分钟内付款，否则订单自动取消。"
    )
    try:
        notify("🎉 京东抢购成功", desp)
    except Exception as e:
        log.warning("推送成功提醒异常：%s", e)


def _summ(v):
    """把大响应 dict 压成关键字段，避免日志刷屏。"""
    if isinstance(v, dict):
        keys = ("ok", "error", "sku", "from", "status",
                "chrome_connected", "logged_in", "has_checkout", "has_payment",
                "order_id")
        return {k: v.get(k) for k in keys if k in v}
    return v


PORT = 8899

# 线程池：执行实际的提交动作（短任务）。定时等待用独立 daemon 线程，不占池。
EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=8)
# 定时任务登记表
SCHED_LOCK = threading.Lock()
SCHEDULED = {}
_TASK_ID = [0]
# 当前调试 Chrome 模式（True=后台无窗口），用于自动切换
CURRENT_HEADLESS = {"val": True}

# ---- Playwright 同步 API 单线程 worker（greenlet 不支持跨线程切换）----
# 所有浏览器操作统一交给该 worker 执行；HTTP 请求线程通过 browser_call 提交并等待结果。
_BROWSER_Q = queue.Queue()


def _browser_worker_loop():
    while True:
        task = _BROWSER_Q.get()
        if task is None:
            break
        result_q, func, args, kwargs = task
        try:
            result_q.put((True, func(*args, **kwargs)))
        except Exception as e:
            import traceback
            result_q.put((False, {"ok": False, "error": str(e),
                                  "trace": traceback.format_exc()[-500:]}))


_BROWSER_THREAD = threading.Thread(target=_browser_worker_loop, daemon=True, name="browser-worker")
_BROWSER_THREAD.start()


def browser_call(func, *args, timeout=35, **kwargs):
    """把 func(*args, **kwargs) 放到浏览器专属线程执行并等待结果。"""
    result_q = queue.Queue()
    _BROWSER_Q.put((result_q, func, args, kwargs))
    try:
        ok, value = result_q.get(timeout=timeout)
    except queue.Empty:
        return {"ok": False, "error": "操作超时"}
    if not ok:
        return value
    return value


def _shutdown_browser():
    """进程退出前：在 worker 线程里安全地保存登录态并关闭浏览器。"""
    try:
        browser_call(jd.close_debug_chrome, timeout=15)
    except Exception:
        pass


# 用本文件自己的安全关闭逻辑替换 jd_cdp 中可能跨线程的 atexit 处理器
try:
    atexit.unregister(jd._atexit_save)
except Exception:
    pass
atexit.register(_shutdown_browser)


# ---------------------------------------------------------------------------
# 定时提交相关
# ---------------------------------------------------------------------------
def parse_schedule_time(s):
    """解析用户时间串 → datetime。支持：
       2026-07-22 20:00:00 / 2026-07-22 20:00
       2026/07/22 20:00:00
       20:00 / 20:00:00（默认今天，已过则顺延明天）
    """
    s = (s or "").strip()
    now = datetime.now()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            t = datetime.strptime(s, fmt).time()
        except ValueError:
            continue
        d = now.replace(hour=t.hour, minute=t.minute, second=t.second, microsecond=0)
        if d < now:
            d += timedelta(days=1)
        return d
    raise ValueError("无法解析时间，支持格式示例：2026-07-22 20:00:00 或 20:00")


def _do_submit(sku, qty, retries=0):
    """单次提交（带重试）。优先复用已有结算页直接点击，失败再兜底刷新结算页。

    立即提交 常见路径：用户已点过「打开并核对」，结算页已存在，直接点击即可，
    避免每次 submit 都重新 checkout（耗时可达 ~25s）。
    """
    last = None
    attempts = 1 + max(0, retries)
    for attempt in range(attempts):
        try:
            # 第 0 次先尝试复用已有结算页（最快）；后续/兜底再 ensure_checkout
            r = jd.submit_order(sku, qty, ensure_checkout=(attempt > 0))
            if r.get("ok"):
                return r
            last = r
        except Exception as e:
            last = {"ok": False, "error": str(e)}
        if attempt < retries:
            time.sleep(0.5)
    # retries=0 且快路径失败时，兜底再尝试一次刷新结算页
    if retries == 0 and last and not last.get("ok"):
        try:
            r = jd.submit_order(sku, qty, ensure_checkout=True)
            if r.get("ok"):
                return r
            last = r
        except Exception as e:
            last = {"ok": False, "error": str(e)}
    return last or {"ok": False, "error": "提交失败"}


def _fire(sku, qty, concurrency, retries):
    """实际开火：并发数<=1 走单次+重试；>1 先开一次结算页再在 worker 线程内顺序快速点击。"""
    if concurrency <= 1:
        return _do_submit(sku, qty, retries)
    try:
        jd.checkout(sku, qty)
    except Exception as e:
        return {"ok": False, "error": f"打开结算页失败: {e}"}
    results = []
    for i in range(concurrency):
        if i > 0:
            time.sleep(0.15 * i)  # 错峰，避免同一瞬间重复提交
        try:
            r = jd.submit_order(sku, qty, ensure_checkout=False)
        except Exception as e:
            r = {"ok": False, "error": str(e)}
        results.append(r)
        if r.get("ok"):
            return r
    return {"ok": False, "error": "并发提交均未成功", "details": results}


def _run_scheduled(task):
    """定时线程：睡到目标时间 → 开火（单次或循环到抢到为止）。可被取消标记中断。"""
    tid = task["id"]
    loop = task.get("loop", False)
    interval = max(0.5, float(task.get("interval", 2)))
    max_tries = int(task.get("max_tries", 0))  # 0 = 无限循环直到抢到/取消

    # 1) 预热 + 等待到点
    prep_target = None
    if task.get("prep"):
        prep_seconds = float(task.get("prep_seconds", 3))
        prep_at = task["at"] - timedelta(seconds=prep_seconds)
        prep_wait = (prep_at - datetime.now()).total_seconds()
        if prep_wait > 0:
            pw = 0.0
            while pw < prep_wait:
                with SCHED_LOCK:
                    if SCHEDULED.get(tid, {}).get("cancelled"):
                        SCHEDULED[tid]["status"] = "cancelled"
                        SCHEDULED[tid]["result"] = {"ok": False, "error": "已取消"}
                        return
                step = min(1.0, prep_wait - pw)
                time.sleep(step)
                pw += step
        # 提前打开并保留结算页，到点直接提交，压低开抢瞬间延迟
        try:
            pr = browser_call(jd.checkout, task["sku"], task["qty"], keep_open=True, timeout=40)
            if pr.get("ok"):
                prep_target = pr.get("target_id")
        except Exception:
            prep_target = None
    delay = (task["at"] - datetime.now()).total_seconds()
    if delay > 0:
        slept = 0.0
        while slept < delay:
            with SCHED_LOCK:
                if SCHEDULED.get(tid, {}).get("cancelled"):
                    SCHEDULED[tid]["status"] = "cancelled"
                    SCHEDULED[tid]["result"] = {"ok": False, "error": "已取消"}
                    return
            step = min(1.0, delay - slept)
            time.sleep(step)
            slept += step
    with SCHED_LOCK:
        if SCHEDULED.get(tid, {}).get("cancelled"):
            SCHEDULED[tid]["status"] = "cancelled"
            SCHEDULED[tid]["result"] = {"ok": False, "error": "已取消"}
            return

    # 2) 单次模式（与原逻辑一致，优先用预热页）
    if not loop:
        with SCHED_LOCK:
            SCHEDULED[tid]["status"] = "running"
        if prep_target:
            res = browser_call(jd.submit_order, task["sku"], task["qty"], False,
                               target_id=prep_target, timeout=40)
            if not res.get("ok"):
                res = browser_call(_fire, task["sku"], task["qty"], task["concurrency"],
                                   task["retries"], timeout=80)
        else:
            res = browser_call(_fire, task["sku"], task["qty"], task["concurrency"],
                               task["retries"], timeout=80)
        with SCHED_LOCK:
            SCHEDULED[tid]["status"] = "done" if res.get("ok") else "error"
            SCHEDULED[tid]["result"] = res
        if res.get("ok"):
            _notify_success(task, res)
        return

    # 3) 循环模式：先打预热一枪（复用提前打开的结算页），失败再进入常规循环
    if prep_target:
        with SCHED_LOCK:
            SCHEDULED[tid]["status"] = "running"
        sp = browser_call(jd.submit_order, task["sku"], task["qty"], False,
                          target_id=prep_target, timeout=40)
        if sp.get("ok"):
            with SCHED_LOCK:
                SCHEDULED[tid]["status"] = "done"
                SCHEDULED[tid]["result"] = sp
                SCHEDULED[tid]["tries"] = 1
            _notify_success(task, sp)
            return
    # 常规循环：到点后每 interval 秒一次 checkout+submit，直到抢到/取消/达上限
    tries = 0
    risk_streak = 0
    last_err = ""
    while True:
        with SCHED_LOCK:
            if SCHEDULED.get(tid, {}).get("cancelled"):
                SCHEDULED[tid]["status"] = "cancelled"
                SCHEDULED[tid]["result"] = {"ok": False, "error": "已取消"}
                return
        tries += 1
        with SCHED_LOCK:
            SCHEDULED[tid]["tries"] = tries
            SCHEDULED[tid]["status"] = "running"
        # 每轮先打开结算页并读取风控状态（checkout 自带 risk_control 检测）
        chk = browser_call(jd.checkout, task["sku"], task["qty"], timeout=40)
        if chk.get("ok"):
            if chk.get("risk_control"):
                # 京东风控：拉长暂停，避免加重风控
                risk_streak += 1
                pause = min(30.0, interval * 4 * risk_streak)
                note = f"⚠️ 账号被风控拦截，暂停 {pause:.0f}s 后重试（已尝试 {tries} 次）"
                last_err = chk.get("text_snippet") or chk.get("error") or ""
            else:
                # 未风控 → 真实提交（复用刚打开的结算页）
                sub = browser_call(jd.submit_order, task["sku"], task["qty"], False, timeout=40)
                if sub.get("ok"):
                    with SCHED_LOCK:
                        SCHEDULED[tid]["status"] = "done"
                        SCHEDULED[tid]["result"] = sub
                        SCHEDULED[tid]["tries"] = tries
                    _notify_success(task, sub)
                    return
                risk_streak = 0
                pause = interval
                note = f"第 {tries} 次未下单（{sub.get('error', '')}），{interval:.0f}s 后重试"
                last_err = sub.get("error") or ""
        else:
            # 结算页都没打开（网络抖动/风控）
            risk_streak = 0
            pause = interval
            note = f"第 {tries} 次结算页未打开（{chk.get('error', '')}），{interval:.0f}s 后重试"
            last_err = chk.get("error") or ""
        with SCHED_LOCK:
            SCHEDULED[tid]["note"] = note
            SCHEDULED[tid]["last_error"] = last_err
        if max_tries > 0 and tries >= max_tries:
            with SCHED_LOCK:
                SCHEDULED[tid]["status"] = "error"
                SCHEDULED[tid]["result"] = {"ok": False, "error": f"已达最大尝试次数 {max_tries}", "tries": tries}
            return
        time.sleep(pause)


def schedule_submit(data):
    sku = str(data.get("sku", "100342780502"))
    qty = int(data.get("qty", 1))
    conc = max(1, int(data.get("concurrency", 1)))
    retries = max(0, int(data.get("retries", 0)))
    loop = bool(data.get("loop", False))
    interval = max(0.5, float(data.get("interval", 2)))
    max_tries = max(0, int(data.get("max_tries", 0)))
    prep = bool(data.get("prep", False))
    prep_seconds = max(1, int(data.get("prep_seconds", 3)))
    at = parse_schedule_time(data.get("at", ""))
    if at <= datetime.now():
        raise ValueError("定时必须晚于当前时间")
    if loop:
        # 循环模式本身就是「多次提交」，强制单笔（并发>1 会在每轮生成多订单）
        conc = 1
    with SCHED_LOCK:
        _TASK_ID[0] += 1
        tid = f"T{_TASK_ID[0]:03d}"
        task = {
            "id": tid, "sku": sku, "qty": qty, "concurrency": conc,
            "retries": retries, "at": at, "loop": loop,
            "interval": interval, "max_tries": max_tries, "tries": 0,
            "prep": prep, "prep_seconds": prep_seconds,
            "note": "", "last_error": "",
            "at_str": at.strftime("%Y-%m-%d %H:%M:%S"),
            "status": "pending", "result": None,
            "created": datetime.now().strftime("%H:%M:%S"),
        }
        SCHEDULED[tid] = task
    threading.Thread(target=_run_scheduled, args=(task,), daemon=True).start()
    if loop:
        mode = f"循环抢购（每 {interval:.0f}s 一次，直到抢到；上限 {max_tries or '∞'} 次）"
    else:
        mode = f"单次提交（并发 {conc}，重试 {retries}）"
    return {"ok": True, "task_id": tid, "at": task["at_str"], "loop": loop,
            "message": f"已安排在 {task['at_str']} {mode}"}


def list_tasks():
    with SCHED_LOCK:
        items = []
        for t in SCHEDULED.values():
            item = dict(t)
            item["at"] = t["at"].strftime("%Y-%m-%d %H:%M:%S")
            items.append(item)
        items.sort(key=lambda x: x["at_str"], reverse=True)
        return {"ok": True, "tasks": items}


def cancel_task(tid):
    with SCHED_LOCK:
        t = SCHEDULED.get(tid)
        if not t:
            return {"ok": False, "error": "任务不存在"}
        if t["status"] in ("done", "error"):
            return {"ok": False, "error": "任务已结束，无法取消"}
        t["cancelled"] = True
        t["status"] = "cancelled"
        return {"ok": True, "message": f"已取消 {tid}"}


# ---------------------------------------------------------------------------
# 前端页面
# ---------------------------------------------------------------------------
HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<meta http-equiv="Cache-Control" content="no-store, no-cache, must-revalidate" />
<meta http-equiv="Pragma" content="no-cache" />
<meta http-equiv="Expires" content="0" />
<title>京东抢购助手 v2026-07-24.1</title>
<style>
  :root{
    --bg:#f5f5f7; --card:#ffffff; --ink:#1d1d1f; --ink-2:#6e6e73; --line:#e8e8ed;
    --blue:#0071e3; --blue-press:#0077ed; --red:#ff3b30; --red-press:#ff453a;
    --green:#34c759; --gray-6:#8e8e93; --radius:18px;
    --shadow:0 1px 3px rgba(0,0,0,.04), 0 10px 30px rgba(0,0,0,.05);
  }
  *{ box-sizing:border-box; }
  body{ margin:0; font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","SF Pro Display","PingFang SC","Helvetica Neue",sans-serif;
        background:var(--bg); color:var(--ink); -webkit-font-smoothing:antialiased; line-height:1.5; }
  .wrap{ max-width:760px; margin:0 auto; padding:28px 18px 60px; }
  .app-header{ display:flex; align-items:center; gap:13px; margin:4px 0 22px; }
  .app-header .logo{ width:42px; height:42px; border-radius:12px; background:linear-gradient(135deg,#ff7a7a,#e1251b);
        display:flex; align-items:center; justify-content:center; color:#fff; font-weight:700; font-size:21px;
        box-shadow:0 6px 16px rgba(225,37,27,.28); }
  .app-header h1{ font-size:23px; font-weight:700; letter-spacing:-.02em; margin:0; }
  .app-header .sub{ font-size:13px; color:var(--ink-2); margin-top:2px; }
  .card{ background:var(--card); border-radius:var(--radius); padding:20px; margin-bottom:16px; box-shadow:var(--shadow); }
  .card h2{ font-size:17px; font-weight:600; margin:0 0 14px; letter-spacing:-.01em; display:flex; align-items:center; }
  .step-badge{ display:inline-flex; align-items:center; justify-content:center; min-width:22px; height:22px; padding:0 7px;
        border-radius:50%; background:var(--blue); color:#fff; font-size:12px; font-weight:600; margin-right:10px; }
  .row{ display:flex; gap:10px; flex-wrap:wrap; align-items:center; }
  .grid2{ display:grid; grid-template-columns:repeat(2,1fr); gap:16px; }
  @media(max-width:520px){ .grid2{ grid-template-columns:1fr; } }
  .field{ display:flex; flex-direction:column; gap:6px; }
  .field label{ font-size:13px; font-weight:500; color:var(--ink-2); }
  .field label .tag{ font-size:11px; font-weight:400; padding:2px 6px; border-radius:4px; margin-left:4px; display:inline-block; }
  .field label .tag.required{ background:var(--red); color:#fff; }
  .field label .tag.hint{ background:#e5e5ea; color:var(--ink-2); }
  input[type=text]{ padding:11px 14px; border:1px solid var(--line); border-radius:12px; font-size:15px; flex:1; min-width:120px;
        background:#fbfbfd; outline:none; transition:border-color .2s, box-shadow .2s, background .2s; }
  input[type=text]:focus{ border-color:var(--blue); box-shadow:0 0 0 4px rgba(0,113,227,.12); background:#fff; }
  button{ font-family:inherit; background:var(--blue); color:#fff; border:none; border-radius:980px; padding:11px 20px;
        font-size:15px; font-weight:500; cursor:pointer; transition:transform .12s, background .2s, box-shadow .2s; }
  button:hover{ background:var(--blue-press); }
  button:active{ transform:scale(.97); }
  button.ghost{ background:#f0f0f3; color:var(--ink); }
  button.ghost:hover{ background:#e6e6ea; }
  button.danger{ background:var(--red); }
  button.danger:hover{ background:var(--red-press); }
  button:disabled{ opacity:.45; cursor:not-allowed; }
  .switch{ display:inline-flex; align-items:center; gap:6px; cursor:pointer; color:var(--ink-2); font-size:13px; }
  .status{ display:flex; gap:8px; flex-wrap:wrap; margin-bottom:6px; }
  .badge{ font-size:12.5px; font-weight:500; padding:5px 13px; border-radius:980px; background:#f0f0f3; color:var(--ink-2); transition:all .2s; }
  .badge.on{ background:rgba(52,199,89,.15); color:#1c7c34; }
  .badge.off{ background:rgba(255,59,48,.13); color:#c9342b; }
  pre{ white-space:pre-wrap; word-break:break-all; background:#fafafa; border:1px solid var(--line); border-radius:12px;
        padding:11px; font-size:13px; max-height:260px; overflow:auto; margin:8px 0 0; }
  .kv{ font-size:14px; line-height:1.9; }
  .kv b{ color:var(--red); }
  .ok{ color:var(--green); }
  .err{ color:var(--red); }
  .muted{ color:var(--ink-2); font-size:12.5px; }
  #resolve-hint{ white-space:pre-wrap; word-break:break-all; line-height:1.5; max-height:200px; overflow:auto; }
  #resolve-hint.err{ color:var(--red); }
  #resolve-hint.ok{ color:var(--green); }
  .note{ font-size:12.5px; color:#8a6d3b; background:#fff8e9; border:1px solid #f3e2bd; padding:10px 12px; border-radius:12px; margin-top:12px; line-height:1.6; }
  .seg{ display:inline-flex; background:#e8e8ed; border-radius:980px; padding:3px; gap:2px; flex-wrap:wrap; max-width:100%; }
  .seg button{ border:none; background:transparent; color:var(--ink-2); border-radius:980px; padding:7px 16px;
        font-size:14px; font-weight:500; cursor:pointer; transition:all .2s; }
  .seg button:hover{ color:var(--ink); }
  .seg button.active{ background:#fff; color:var(--ink); box-shadow:0 1px 4px rgba(0,0,0,.12); }
  .seg button.add{ color:var(--blue); font-weight:600; }
  ul.tasks{ margin:6px 0 0; padding-left:18px; }
  ul.tasks li{ margin:3px 0; font-size:13px; }
  .footer{ text-align:center; margin:14px 0 4px; }
  button.sm{ padding:7px 14px; font-size:13px; }
  .status-actions{ margin-top:12px; flex-wrap:wrap; }
  .buy-btn{ font-size:16px; padding:12px 26px; }
</style>
</head>
<body>
<div class="wrap">
  <div class="app-header">
    <div class="logo">京</div>
    <div>
      <h1>京东抢购助手</h1>
      <div class="sub">调试 Chrome 驱动 · 多账号 · 一键抢购</div>
    </div>
  </div>

  <!-- 状态栏：自动轮询，无需手动刷新 -->
  <div class="card">
    <div class="status" id="status">
      <span class="badge" id="b-account">账号：检测中…</span>
      <span class="badge" id="b-chrome">Chrome：检测中…</span>
      <span class="badge" id="b-login">登录态：检测中…</span>
      <span class="badge" id="b-checkout">结算页：检测中…</span>
    </div>
    <div class="row status-actions">
      <button class="ghost sm" id="btn-launch" onclick="launchChrome()">启动调试 Chrome</button>
      <button class="ghost sm" id="btn-login" onclick="openLogin()" style="display:none;">去登录</button>
      <label class="switch"><input type="checkbox" id="headlessChk" checked onchange="onHeadlessChange()"> 后台无窗口</label>
      <span class="muted" id="status-ts"></span>
    </div>
    <pre id="act-log" class="muted">—</pre>
  </div>

  <!-- 账号切换 -->
  <div class="card">
    <h2>账号</h2>
    <div class="row" style="align-items:center;">
      <div class="seg" id="account-seg"></div>
      <button class="ghost" onclick="addAccount()">+ 新增</button>
      <button class="danger ghost" onclick="logoutAccount()">注销登录</button>
    </div>
    <div class="muted" id="account-hint" style="margin-top:10px;">正在读取账号…</div>
  </div>

  <!-- 抢购面板（原 ②③ ④ 合并为一步） -->
  <div class="card">
    <h2>🚀 抢购</h2>
    <div class="muted">粘贴分享链接自动解析 SKU，或手动填 SKU。点「立即抢购」会<b>自动打开结算页并下单</b>（无需先单独核对）。</div>
    <div class="row" style="margin-top:12px;">
      <input type="text" id="shareLink" placeholder="粘贴京东分享短链 / 商品页链接，自动解析 SKU">
      <button class="ghost" onclick="resolveLink()">解析链接</button>
    </div>
    <div class="muted" id="resolve-hint" style="margin-top:6px;">支持 3.cn 短链 / item.jd.com 链接 / 直接填 SKU 数字</div>
    <div class="grid2" style="margin-top:16px;">
      <div class="field">
        <label for="sku4">商品 SKU <span class="tag required">必填</span></label>
        <input type="text" id="sku4" value="100342780502" title="京东商品编号，填数字">
      </div>
      <div class="field">
        <label for="qty4">购买数量</label>
        <input type="text" id="qty4" value="1" title="购买件数">
      </div>
      <div class="field">
        <label for="conc4">并发数 <span class="tag hint">建议保持 1</span></label>
        <input type="text" id="conc4" value="1" title="同一时刻连续点击次数。>1 会生成多笔订单且极易触发京东风控，一般保持 1">
      </div>
      <div class="field">
        <label for="retry4">失败重试次数 <span class="tag hint">一般 0</span></label>
        <input type="text" id="retry4" value="0" title="首次提交失败后重试几次。>0 可能延长总耗时，一般保持 0">
      </div>
    </div>
    <div class="row" style="margin-top:14px;">
      <button class="danger buy-btn" id="btn-buy" onclick="doSubmit()">🚀 立即抢购</button>
      <button class="ghost" onclick="doCheckout()">仅打开结算页核对</button>
    </div>
    <div class="row" style="margin-top:10px;">
      <input type="text" id="at4" placeholder="定时时间，如 20:00">
      <button class="ghost" onclick="scheduleSubmit()">⏰ 定时抢购</button>
    </div>
    <label class="switch" style="margin-top:10px;">
      <input type="checkbox" id="loopChk" onchange="onLoopChange()"> 循环到抢到为止（到点后每间隔多次提交，直到抢到）
    </label>
    <div class="row" id="loop-opts" style="margin-top:8px; align-items:center; display:none;">
      <span class="muted">循环间隔</span>
      <input type="text" id="interval4" value="2" style="flex:0 0 64px;" title="每轮提交之间的最小间隔秒数；被京东风控时会自动拉长暂停">
      <span class="muted">秒（京东风控时自动拉长暂停）</span>
    </div>
    <label class="switch" style="margin-top:10px;">
      <input type="checkbox" id="prepChk"> 预热（开抢前提前打开结算页，整点直接提交，延迟最低）
    </label>
    <div class="row" style="margin-top:8px; align-items:center;">
      <span class="muted">提前</span>
      <input type="text" id="prepSec4" value="3" style="flex:0 0 56px;" title="开抢前多少秒提前打开结算页">
      <span class="muted">秒预热</span>
    </div>
    <div class="note">⚠️ 立即抢购会真实提交订单并跳转收银台（<b>会产生订单需支付</b>）。并发数&gt;1 可能生成多笔订单；定时抢购到点由后台自动执行，关掉页面也不影响。</div>
    <div id="checkout-result" class="kv" style="margin-top:12px;"></div>
    <pre id="out-submit">—</pre>
  </div>

  <!-- 手机提醒（Server 酱） -->
  <div class="card">
    <h2>📱 手机提醒（Server 酱）</h2>
    <div class="muted">抢到后会推送微信提醒。注册 <a href="https://sct.ftqq.com" target="_blank" rel="noreferrer">Server 酱</a> 拿到 SendKey（SCT 开头），填下方保存即可。每次抢到自动推送到你微信。</div>
    <div class="row" style="margin-top:10px;">
      <input type="text" id="scKey" placeholder="Server 酱 SendKey，如 SCTxxxxx">
      <button class="ghost" onclick="saveNotify()">保存</button>
      <button class="ghost" onclick="testNotify()">测试推送</button>
    </div>
    <div class="muted" id="notify-hint" style="margin-top:6px;"></div>
  </div>

  <!-- 定时任务 -->
  <div class="card">
    <h2>定时任务</h2>
    <div class="row" style="margin-top:4px;">
      <button class="ghost sm" onclick="refreshTasks()">刷新任务</button>
    </div>
    <ul class="tasks" id="tasks" style="margin-top:8px;"><li class="muted">无</li></ul>
  </div>

  <div class="muted footer">原脚本 jd_direct_order.py / raw_click.py 已保留，可命令行使用。</div>
</div>

<script>
function setBadge(id, text, on, off){
  const el = document.getElementById(id);
  el.textContent = text;
  el.className = 'badge ' + (on ? 'on' : (off ? 'off' : ''));
}
async function refreshStatus(){
  try{
    const r = await fetch('/api/status', {method:'POST'});
    const d = await r.json();
    jd_chrome_ok = !!d.chrome_connected;
    jd_logged_in = !!d.logged_in;
    setBadge('b-chrome', 'Chrome：'+(d.chrome_connected?'已连接':'未连接'), d.chrome_connected, !d.chrome_connected);
    setBadge('b-login', '登录态：'+(d.logged_in?'已登录':'未登录'), d.logged_in, !d.logged_in);
    setBadge('b-checkout', '结算页：'+(d.has_checkout?'已打开':'未打开'), d.has_checkout, !d.has_checkout);
    if(d.active_account){
      jd_active_account = d.active_account;
      setBadge('b-account', '当前账号：'+d.active_account, d.logged_in, !d.logged_in);
    }
    updateStatusActions(d);
    const ts=document.getElementById('status-ts');
    if(ts) ts.textContent='状态更新于 '+new Date().toLocaleTimeString();
    if(d.detail) console.log(d.detail);
  }catch(e){
    const ts=document.getElementById('status-ts');
    if(ts) ts.textContent='状态获取失败：'+e;
  }
}

// 根据状态上下文自动显隐「启动 Chrome / 去登录」按钮，减少多余操作
function updateStatusActions(d){
  const launch=document.getElementById('btn-launch');
  const login=document.getElementById('btn-login');
  if(!launch||!login) return;
  // Chrome 未连接时才需要「启动调试 Chrome」；只要没登录就始终显示「去登录」入口
  launch.style.display = d.chrome_connected ? 'none' : '';
  login.style.display  = d.logged_in ? 'none' : '';
}
// 切换「后台无窗口」即重启调试 Chrome 切换窗口模式（登录态保留在 Chrome profile / user-data-dir，无需 states/）
function onHeadlessChange(){ launchChrome(); }

async function api(path, body){
  // 用 text() 拿原始响应体（无论后端返回 JSON / HTML 错误页 / 被中间设备改写的内容）
  const r = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body||{})});
  const raw = await r.text();
  let d;
  try { d = JSON.parse(raw); }
  catch(e){
    // 响应不是合法 JSON——很可能是中间设备（代理/扩展）改写或拦截
    throw {__raw_response:true, status:r.status, content_type:r.headers.get('content-type')||'', body:raw.slice(0,500)};
  }
  // 把原始响应体附带进去（调试用，前端可以打印）
  d.__raw = raw.slice(0,500);
  d.__status = r.status;
  return d;
}
function val(id){ return document.getElementById(id).value.trim(); }
function sleep(ms){ return new Promise(r=>setTimeout(r,ms)); }
// 下单前守卫：未登录则自动打开登录页并轮询等待完成，避免跳过登录直接 checkout 失败
async function ensureLoggedIn(o){
  let st;
  // 先查缓存状态（前端每 3s 自动轮询保持缓存新鲜，命中时毫秒级返回）；
  // 只有缓存显示未登录时才强制探测（真实探测要开标签页，约 2~3s）
  try { st = await api('/api/status'); } catch(e){ st = {logged_in:false}; }
  if(!st.logged_in){
    try { st = await api('/api/status', {force:true}); } catch(e){}
  }
  if(st && st.logged_in){ jd_logged_in = true; return true; }
  if(o) o.textContent = '未登录：正在打开登录页，请在弹出的浏览器窗口完成扫码 / 短信登录…';
  try { await api('/api/open_login', {sku:getSku(), qty:getQty()}); }
  catch(e){ if(o){ o.className='err'; o.textContent='打开登录页失败：'+e; } return false; }
  if(o) o.textContent = '登录页已打开，等待你完成登录（最多 120 秒）…';
  for(let i=0;i<60;i++){
    await sleep(2000);
    try { st = await api('/api/status', {force:true}); } catch(e){ continue; }
    if(st.logged_in){ jd_logged_in = true; if(o) o.textContent='✅ 登录成功，继续抢购…'; refreshStatus(); return true; }
  }
  jd_logged_in = false;
  if(o){ o.className='err'; o.textContent='⏱️ 登录等待超时（120 秒）。请确认已在浏览器窗口完成登录后重试；也可直接点状态栏「去登录」。'; }
  return false;
}
let jd_chrome_ok = false;  // 调试 Chrome 是否连接，供解析前预判
let jd_headless = true;    // 当前后台无窗口模式（与界面勾选同步）
let jd_active_account = ""; // 当前激活的京东账号名
let jd_logged_in = false;  // 当前账号是否已登录京东（供下单前守卫判断）
// SKU / 数量统一从 ④ 提交订单区读取（单一数据源，避免重复输入）
function getSku(){ return val('sku4') || '100342780502'; }
function getQty(){ return parseInt(val('qty4') || '1'); }
// ---- 多账号切换 ----
async function loadAccounts(){
  try{
    const d = await api('/api/accounts', {});
    const seg = document.getElementById('account-seg');
    seg.innerHTML = '';
    (d.accounts||[]).forEach(a=>{
      const b = document.createElement('button');
      b.textContent = a.name;
      if(a.name === d.active) b.className = 'active';
      b.onclick = ()=> switchAccount(a.name);
      seg.appendChild(b);
    });
    const hint = document.getElementById('account-hint');
    let s = '当前账号：' + (d.active||'—');
    const cur = (d.accounts||[]).find(a=>a.name===d.active);
    if(cur && cur.note) s += '（'+cur.note+'）';
    s += ' ｜ 切换为毫秒级（不重启浏览器），登录态由 Chrome profile（user-data-dir）按账号隔离保存';
    hint.textContent = s;
    jd_active_account = d.active;
  }catch(e){ console.log(e); }
}
async function switchAccount(name){
  if(name === jd_active_account){ return; }
  const hint = document.getElementById('account-hint');
  hint.textContent = '正在切换到「'+name+'」…（重启调试 Chrome）';
  // 先把分段控件里目标标记为 active，给出即时反馈
  document.querySelectorAll('#account-seg button').forEach(b=>{
    b.classList.toggle('active', b.textContent === name);
  });
  try{
    const d = await api('/api/account_switch', {name: name});
    if(d.ok){
      hint.textContent = '已切换到「'+name+'」，正在加载该账号登录态…';
      setTimeout(()=>{ refreshStatus(); loadAccounts(); }, 1800);
    } else {
      hint.textContent = '切换失败：' + (d.error||'未知错误');
      loadAccounts();
    }
  }catch(e){ hint.textContent = '切换错误：'+e; loadAccounts(); }
}
async function addAccount(){
  const name = prompt('新账号名称（如：小号A）：');
  if(!name) return;
  const note = prompt('备注（可选，可填该账号的京东昵称）：','') || '';
  try{
    const d = await api('/api/account_add', {name: name, note: note});
    if(d.ok){ alert(d.message || ('已新增 '+name)); loadAccounts(); }
    else alert('添加失败：' + (d.error||''));
  }catch(e){ alert('错误：'+e); }
}
async function logoutAccount(){
  if(!confirm('确定注销当前账号「'+(jd_active_account||'默认账号')+'」的登录态吗？\n（仅清除本地保存的登录凭据，不影响京东账号本身）')) return;
  const hint = document.getElementById('account-hint');
  hint.textContent = '正在注销「'+(jd_active_account||'')+'」…';
  try{
    const d = await api('/api/account_logout', {name: jd_active_account});
    if(d.ok){ alert(d.message||'已注销'); refreshStatus(); loadAccounts(); openLogin(); }
    else alert('注销失败：'+(d.error||''));
  }catch(e){ alert('错误：'+e); }
}
async function resolveLink(){
  let link = val('shareLink').replace(/\r?\n/g, ' ').trim();  // 仅合并换行并去首尾空白，保留中间空格（避免把商品名/编号粘到 URL 上）
  const hint = document.getElementById('resolve-hint');
  if(!link){ alert('请先粘贴分享链接'); return; }
  hint.className = 'muted';
  hint.textContent = '解析中…（纯 HTTP 跟跳转，约 1 秒内）';
  try{
    const d = await api('/api/resolve', {url: link});
    if(!d.ok){
      let msg = '❌ 解析失败：'+(d.error||'未知错误');
      const dbg = [];
      if(d.from) dbg.push('来源: '+d.from);
      if(d.trace) dbg.push('堆栈: '+d.trace);
      if(d.final_url) dbg.push('最终URL: '+d.final_url);
      if(d.api_func) dbg.push('接口: '+d.api_func);
      // 关键诊断：如果原始响应体里没有 error 字符串，说明被中间设备改写过
      if(d.__raw && (!d.error || d.__raw.indexOf(d.error)===-1)){
        dbg.push('⚠️ 原始响应与 error 字段不一致（可能被代理/扩展改写）:');
        dbg.push(d.__raw);
      }
      if(d.__status) dbg.push('HTTP 状态: '+d.__status);
      if(dbg.length) msg += '\n—— 调试信息 ——\n'+dbg.join('\n');
      hint.className = 'muted err';
      hint.textContent = msg;
      return;
    }
    document.getElementById('sku4').value = d.sku;
    let ok = '✅ 已解析 SKU：'+d.sku+'（已填入上方）';
    if(d.from) ok += '\n来源: '+d.from;
    if(d.final_url) ok += '\n商品页: '+d.final_url;
    hint.className = 'muted ok';
    hint.textContent = ok;
  }catch(e){
    hint.className = 'muted err';
    if(e && e.__raw_response){
      hint.textContent = '❌ 响应不是合法 JSON（可能被代理/扩展拦截改写）\nHTTP 状态: '+e.status+'\nContent-Type: '+e.content_type+'\n原始响应体:\n'+e.body;
    } else {
      hint.textContent = '❌ 解析请求异常：'+e+'\n（请检查 GUI 服务是否在运行）';
    }
  }
}
async function launchChrome(){
  const o = document.getElementById('act-log');
  if(o){ o.className=''; o.textContent='启动调试 Chrome 中…'; }
  const headless = document.getElementById('headlessChk').checked;
  try{
    const d = await api('/api/launch_chrome', {headless: headless});
    if(o) o.textContent = JSON.stringify(d, null, 2);
    if(d.ok){ jd_headless = headless; setTimeout(refreshStatus, 1500); }
  }catch(e){ if(o){ o.className='err'; o.textContent='错误：'+e; } }
}
async function openLogin(){
  const o = document.getElementById('act-log');
  if(o){ o.className=''; o.textContent='准备登录（必要时先以窗口模式启动 Chrome）…'; }
  try{
    // 登录需肉眼扫码 → 确保 Chrome 已启动且为窗口模式（可见）
    let st = {chrome_connected:false};
    try { st = await api('/api/status'); } catch(e){}
    if(!st.chrome_connected){
      document.getElementById('headlessChk').checked = false;  // 取消后台无窗口，保证登录窗口可见
      await api('/api/launch_chrome', {headless:false});
      await sleep(2000);
    }
    // open_login 后端会自动切到窗口模式并打开登录页
    const d = await api('/api/open_login', {sku:getSku(), qty:getQty()});
    if(o) o.textContent = '✅ 已打开登录页，请在弹出的 Chrome 窗口完成扫码 / 短信登录；登录成功后状态栏「登录态」会变「已登录」。';
    refreshStatus();
  }catch(e){ if(o){ o.className='err'; o.textContent='错误：'+e; } }
}
async function doCheckout(){
  const box = document.getElementById('checkout-result');
  box.innerHTML = '打开中…';
  try{
    if(!await ensureLoggedIn(box)) return;  // 未登录先登录，再读结算页
    const d = await api('/api/checkout', {sku:getSku(), qty:getQty()});
    renderCheckout(box, d);
    refreshStatus();
  }catch(e){ box.innerHTML = '<span class="err">错误：'+e+'</span>'; }
}
async function doCheckoutRetry(){
  const box = document.getElementById('checkout-result');
  box.innerHTML = '重试中（已等风控冷却 2s）…';
  try{
    const d = await api('/api/checkout_retry', {sku:getSku(), qty:getQty()});
    renderCheckout(box, d);
    refreshStatus();
  }catch(e){ box.innerHTML = '<span class="err">错误：'+e+'</span>'; }
}
function renderCheckout(box, d){
  if(!d.ok){ box.innerHTML = '<span class="err">失败：'+(d.error||'')+'</span>'; return; }
  if(d.risk_control){
    let h = '<div class="note" style="color:var(--red);background:#fdeaea;border-color:#f5c6c6;">'
          + '⚠️ <b>账号被京东风控拦截</b>：页面显示「活动异常火爆，已优先接入快速通道」。<br>'
          + '这不是程序问题，是京东对频繁访问的临时限制。请：<br>'
          + '1）暂停点击，等待 1~5 分钟让风控解除；<br>'
          + '2）解除后点「重试（风控冷却后）」或「打开并核对」再读；<br>'
          + '3）不要短时间内反复打开结算页，会加重风控。<br>'
          + '（可在手机京东 App 正常操作下单）</div>';
    box.innerHTML = h;
    return;
  }
  let h = '';
  h += '商品：<b>'+(d.product_name||'(未解析到名称)')+'</b><br>';
  h += 'SKU：'+d.sku+' ｜ 数量(输入/页内)：'+d.qty_input+' / '+(d.qty_found??'—')+'<br>';
  h += '价格：<b>'+(d.price?('¥'+d.price):'(未解析)')+'</b><br>';
  if(d.address_hint) h += '地址：'+d.address_hint+'<br>';
  if(d.phone) h += '手机号：<b>'+d.phone+'</b><br>';
  h += '<details style="margin-top:8px;"><summary class="muted">查看结算页原文</summary><pre>'+((d.text_snippet||'').replace(/</g,'&lt;'))+'</pre></details>';
  box.innerHTML = h;
}
async function doSubmit(){
  const o = document.getElementById('out-submit');
  const btn = document.getElementById('btn-buy');
  const box = document.getElementById('checkout-result');
  if(btn) btn.disabled = true;
  if(o){ o.className=''; o.textContent='准备抢购（先确认登录态）…'; }
  try{
    // 0) 登录守卫：未登录先打开登录页并等待完成，再继续
    if(!await ensureLoggedIn(o)) return;
    // 1) 先解析结算页：让用户看清「买的是什么」，同时检测京东风控
    const c = await api('/api/checkout', {sku:getSku(), qty:getQty()});
    if(box) renderCheckout(box, c);
    if(c.risk_control){
      if(o) o.textContent = '⚠️ 账号被京东风控拦截，已停止提交（见上方说明）。请等待风控解除后再试，切勿连续点击。';
      return; // 不提交，避免加重风控
    }
    // 2) 商品信息正常 → 真实提交（复用刚打开的结算页，无需再开一次）
    if(o) o.textContent='抢购中（自动提交订单）…';
    const d = await api('/api/submit', {
      sku: getSku(),
      qty: getQty(),
      concurrency: parseInt(val('conc4')||'1'),
      retries: parseInt(val('retry4')||'0')
    });
    if(o) o.textContent = JSON.stringify(d, null, 2);
    refreshStatus(); refreshTasks();
  }catch(e){ if(o){ o.className='err'; o.textContent='错误：'+e; } }
  finally{
    if(btn){
      // 3 秒冷却：避免短时间连点反复打开结算页，触发京东风控
      setTimeout(()=>{ btn.disabled=false; }, 3000);
    }
  }
}
async function scheduleSubmit(){
  const at = val('at4');
  if(!at){ alert('请先填写定时时间，如 20:00 或 2026-07-22 20:00:00'); return; }
  const o = document.getElementById('out-submit');
  o.className=''; o.textContent='安排中…';
  const loop = document.getElementById('loopChk').checked;
  const interval = parseFloat(val('interval4')||'2') || 2;
  try{
    const d = await api('/api/submit_schedule', {
      sku: val('sku4')||'100342780502',
      qty: parseInt(val('qty4')||'1'),
      concurrency: parseInt(val('conc4')||'1'),
      retries: parseInt(val('retry4')||'0'),
      at: at, loop: loop, interval: interval,
      prep: document.getElementById('prepChk').checked,
      prep_seconds: parseInt(val('prepSec4')||'3')
    });
    o.textContent = JSON.stringify(d, null, 2);
    refreshTasks();
  }catch(e){ o.className='err'; o.textContent='错误：'+e; }
}
function onLoopChange(){
  const box = document.getElementById('loop-opts');
  if(box) box.style.display = document.getElementById('loopChk').checked ? 'flex' : 'none';
}
function renderTasks(d){
  const el = document.getElementById('tasks');
  if(!d.ok || !d.tasks || !d.tasks.length){ el.innerHTML='<li class="muted">无</li>'; return; }
  let h='';
  for(const t of d.tasks){
    const st=t.status;
    const color = st==='done'?'var(--green)':(st==='error'?'var(--red)':(st==='cancelled'?'#999':'var(--blue)'));
    const loopTag = t.loop ? ' [循环]' : '';
    h += '<li>#'+t.id+loopTag+' @ '+t.at_str+' ['+t.sku+' ×'+t.qty+'] <b style="color:'+color+'">'+st+'</b>';
    if(t.loop && (t.status==='running' || t.status==='pending')){ h += ' 已尝试 '+(t.tries||0)+' 次'; }
    if(t.loop && t.note){ h += '<br><span class="muted" style="font-size:12px">'+t.note+'</span>'; }
    if(t.status==='pending' || t.status==='running'){
      h += ' <a href="#" onclick="cancelSubmit(\''+t.id+'\');return false;" style="color:var(--red)">取消</a>';
    }
    if(t.result && t.result.order_id){ h += ' 单号:'+t.result.order_id; }
    h += '</li>';
  }
  el.innerHTML = h;
}
async function refreshTasks(){
  try{ const d = await api('/api/submit_tasks', {}); renderTasks(d); }
  catch(e){ console.log(e); }
}
async function saveNotify(){
  const k = (document.getElementById('scKey').value||'').trim();
  const h = document.getElementById('notify-hint');
  try{
    const d = await api('/api/notify_config', {serverchan_key: k});
    h.textContent = d.ok ? (d.configured ? '✅ 已保存，抢到会推微信' : '已清空配置') : ('❌ '+JSON.stringify(d));
  }catch(e){ h.textContent = '错误：'+e; }
}
async function testNotify(){
  const h = document.getElementById('notify-hint');
  h.textContent = '推送中…';
  try{
    const d = await api('/api/notify_config', {test: true});
    h.textContent = d.ok ? '📱 测试推送已发送，请查收微信' : ('❌ '+(d.error||JSON.stringify(d)));
  }catch(e){ h.textContent = '错误：'+e; }
}
async function loadNotify(){
  try{
    const d = await api('/api/notify_config', {});
    if(d.ok && d.configured){
      document.getElementById('notify-hint').textContent = '当前已配置：'+d.serverchan_key;
    }
  }catch(e){}
}
async function cancelSubmit(id){
  if(!id) return;
  if(!confirm('取消定时任务 '+id+'？')) return;
  try{
    const d = await api('/api/submit_cancel', {task_id:id});
    alert(d.ok ? '已取消' : ('失败: '+(d.error||'')));
    refreshTasks();
  }catch(e){ alert('错误：'+e); }
}
refreshStatus();
refreshTasks();
loadAccounts();
loadNotify();
// 状态与任务自动轮询，无需手动刷新
setInterval(refreshStatus, 3000);
setInterval(refreshTasks, 5000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj=None, html=None):
        self.send_response(code)
        # 强制不缓存：避免改完代码后浏览器还在跑旧 JS
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        if html is not None:
            self.send_header("Content-Type", "text/html; charset=utf-8")
            body = html.encode("utf-8")
        else:
            self.send_header("Content-Type", "application/json; charset=utf-8")
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        from urllib.parse import urlparse
        path = urlparse(self.path).path  # 去掉 query string，避免 `/?nocache=...` 走到 404
        if path in ("", "/", "/index.html"):
            self._send(200, html=HTML)
        else:
            self._send(404, {"error": "not found", "path": path})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw or b"{}")
        except Exception:
            data = {}

        def run(func, *a, timeout=35):
            # 所有浏览器操作统一交给单线程 Playwright worker，避免 greenlet 跨线程错误
            return browser_call(func, *a, timeout=timeout)

        try:
            if self.path == "/api/status":
                v = run(jd.chrome_status, bool(data.get("force", False)))
            elif self.path == "/api/launch_chrome":
                hl = bool(data.get("headless", True))
                if CURRENT_HEADLESS["val"] != hl:
                    # 模式变了 → 重启切换（登录态保留在 Chrome profile / user-data-dir，无需 states/）
                    v = run(jd.restart_debug_chrome, hl)
                else:
                    v = run(jd.launch_debug_chrome, hl)
                CURRENT_HEADLESS["val"] = hl
            elif self.path == "/api/open_login":
                # 登录需肉眼操作验证码 → 若当前无窗口，先切到窗口模式
                if CURRENT_HEADLESS["val"]:
                    run(jd.restart_debug_chrome, False)
                    CURRENT_HEADLESS["val"] = False
                v = run(jd.open_login_page, data.get("sku", "100342780502"),
                        int(data.get("qty", 1)))
            elif self.path == "/api/checkout":
                v = run(jd.checkout, data.get("sku", "100342780502"),
                        int(data.get("qty", 1)))
            elif self.path == "/api/checkout_retry":
                # 风控/异常后重试：重开结算页（建议先等风控冷却，勿频繁点）
                v = run(jd.retry_checkout, data.get("sku", "100342780502"),
                        int(data.get("qty", 1)), timeout=40)
            elif self.path == "/api/resolve":
                v = run(jd.resolve_share_link, data.get("url", ""))
            elif self.path == "/api/submit":
                # 线程池提交，可能含并发/重试，给更长超时
                v = run(_fire,
                        data.get("sku", "100342780502"),
                        int(data.get("qty", 1)),
                        int(data.get("concurrency", 1)),
                        int(data.get("retries", 0)),
                        timeout=80)
            elif self.path == "/api/submit_schedule":
                try:
                    v = schedule_submit(data)
                except Exception as e:
                    v = {"ok": False, "error": str(e)}
            elif self.path == "/api/submit_tasks":
                v = list_tasks()
            elif self.path == "/api/submit_cancel":
                v = cancel_task(data.get("task_id"))
            elif self.path == "/api/notify_config":
                if data.get("test"):
                    v = notify("🔔 测试推送", "如果你收到这条微信，说明手机提醒已配置成功。")
                elif "serverchan_key" in data:
                    key = (data.get("serverchan_key") or "").strip()
                    _save_notify_cfg({"serverchan_key": key})
                    v = {"ok": True, "configured": bool(key)}
                else:
                    key = NOTIFY_CFG.get("serverchan_key", "")
                    v = {"ok": True, "configured": bool(key),
                         "serverchan_key": (key[:4] + "****" + key[-4:]) if len(key) > 8
                                         else (key[:1] + "****" if key else "")}
            elif self.path == "/api/accounts":
                v = run(jd.list_accounts)
            elif self.path == "/api/account_switch":
                v = run(jd.switch_account, data.get("name", ""),
                        CURRENT_HEADLESS["val"])
            elif self.path == "/api/account_add":
                v = run(jd.add_account, data.get("name", ""), data.get("note", ""))
            elif self.path == "/api/account_logout":
                v = run(jd.logout_account, data.get("name", ""))
            elif self.path == "/api/debug_cookies":
                v = run(jd.debug_cookies)
            else:
                self._send(404, {"error": "no route"})
                return
            log.info("POST %s | %s", self.path, _summ(v))
            self._send(200, v)
        except Exception as e:
            log.exception("POST %s 异常", self.path)
            self._send(500, {"ok": False, "error": str(e)})

    def log_message(self, fmt, *args):
        try:
            msg = fmt % args if args else fmt
        except Exception:
            msg = " ".join(map(str, (fmt,) + args))
        log.info("HTTP %s", msg)


if __name__ == "__main__":
    print(f"京东抢购 GUI 已启动： http://127.0.0.1:{PORT}")
    print("依赖调试 Chrome 运行在 127.0.0.1:9222。Ctrl+C 停止。")
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    srv.serve_forever()
