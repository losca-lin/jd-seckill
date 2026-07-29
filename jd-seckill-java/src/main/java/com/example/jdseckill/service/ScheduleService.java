package com.example.jdseckill.service;

import com.example.jdseckill.cdp.ChromeManager;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

import javax.annotation.Resource;
import java.text.ParseException;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Calendar;
import java.util.Date;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicLong;

/**
 * 定时抢购调度（等价 Python jd_gui_server 的 schedule_submit / _run_scheduled / tasks）。
 * 支持：预热(提前打开结算页) + 单次提交 + 循环到抢到为止；三处「抢到」分支均触发 Server 酱推送。
 */
@Service
public class ScheduleService {

    private static final Logger log = LoggerFactory.getLogger(ScheduleService.class);

    @Resource
    private ChromeManager chrome;
    @Resource
    private CheckoutService checkoutService;
    @Resource
    private SubmitService submitService;
    @Resource
    private NotifyService notifyService;

    private final Object schedLock = new Object();
    private final Map<String, Map<String, Object>> scheduled = new ConcurrentHashMap<>();
    private final AtomicLong taskId = new AtomicLong(0);

    public Map<String, Object> scheduleSubmit(Map<String, Object> data) {
        String sku = String.valueOf(data.getOrDefault("sku", "100342780502"));
        int qty = toInt(data.get("qty"), 1);
        int conc = Math.max(1, toInt(data.get("concurrency"), 1));
        int retries = Math.max(0, toInt(data.get("retries"), 0));
        boolean loop = Boolean.parseBoolean(String.valueOf(data.get("loop")));
        double interval = Math.max(0.5, toDouble(data.get("interval"), 2));
        int maxTries = Math.max(0, toInt(data.get("max_tries"), 0));
        int stopAfter = Math.max(0, toInt(data.get("stop_after_minutes"), 0));
        if (loop && maxTries == 0 && stopAfter == 0) {
            stopAfter = 10;
            log.warn("[schedule] loop 任务未显式设置 max_tries / stop_after_minutes，启用安全网：到点后最多刷 10 分钟自动停止");
        }
        boolean prep = Boolean.parseBoolean(String.valueOf(data.get("prep")));
        int prepSeconds = Math.max(1, toInt(data.get("prep_seconds"), 3));
        Date at;
        try {
            at = parseScheduleTime(String.valueOf(data.getOrDefault("at", "")));
        } catch (Exception e) {
            return map("ok", false, "error", e.getMessage());
        }
        if (at.getTime() <= System.currentTimeMillis()) {
            return map("ok", false, "error", "定时必须晚于当前时间");
        }
        if (loop) {
            conc = 1; // 循环本身就是多次提交，强制单笔
        }
        String tid = "T" + String.format("%03d", taskId.incrementAndGet());
        Map<String, Object> task = new LinkedHashMap<>();
        task.put("id", tid);
        task.put("sku", sku);
        task.put("qty", qty);
        task.put("concurrency", conc);
        task.put("retries", retries);
        task.put("at", at.getTime());
        task.put("at_str", new SimpleDateFormat("yyyy-MM-dd HH:mm:ss").format(at));
        task.put("loop", loop);
        task.put("interval", interval);
        task.put("max_tries", maxTries);
        task.put("stop_after_minutes", stopAfter);
        task.put("tries", 0);
        task.put("prep", prep);
        task.put("prep_seconds", prepSeconds);
        task.put("note", "");
        task.put("last_error", "");
        task.put("status", "pending");
        task.put("result", null);
        task.put("created", new SimpleDateFormat("HH:mm:ss").format(new Date()));
        scheduled.put(tid, task);
        new Thread(() -> runScheduled(task), "schedule-" + tid).start();
        String mode = loop
                ? "循环抢购（每 " + (int) interval + "s 一次，直到抢到；上限 " + (maxTries == 0 ? "∞" : maxTries)
                  + " 次，到点后最多刷 " + stopAfter + " 分钟）"
                : "单次提交（并发 " + conc + "，重试 " + retries + "）";
        return map("ok", true, "task_id", tid, "at", task.get("at_str"), "loop", loop,
                "message", "已安排在 " + task.get("at_str") + " " + mode);
    }

    public Map<String, Object> listTasks() {
        List<Map<String, Object>> items = new ArrayList<>();
        synchronized (schedLock) {
            for (Map<String, Object> t : scheduled.values()) {
                Map<String, Object> item = new LinkedHashMap<>(t);
                item.put("at", item.get("at_str"));
                items.add(item);
            }
        }
        items.sort((a, b) -> String.valueOf(b.get("at_str")).compareTo(String.valueOf(a.get("at_str"))));
        return map("ok", true, "tasks", items);
    }

    public Map<String, Object> cancelTask(String tid) {
        synchronized (schedLock) {
            Map<String, Object> t = scheduled.get(tid);
            if (t == null) {
                return map("ok", false, "error", "任务不存在");
            }
            if ("done".equals(t.get("status")) || "error".equals(t.get("status"))) {
                return map("ok", false, "error", "任务已结束，无法取消");
            }
            t.put("cancelled", true);
            t.put("status", "cancelled");
            return map("ok", true, "message", "已取消 " + tid);
        }
    }

    @SuppressWarnings("unchecked")
    private void runScheduled(Map<String, Object> task) {
        String tid = String.valueOf(task.get("id"));
        boolean loop = Boolean.parseBoolean(String.valueOf(task.get("loop")));
        double interval = toDouble(task.get("interval"), 2);
        int maxTries = toInt(task.get("max_tries"), 0);
        int stopAfter = toInt(task.get("stop_after_minutes"), 0);
        String sku = String.valueOf(task.get("sku"));
        int qty = toInt(task.get("qty"), 1);
        int conc = toInt(task.get("concurrency"), 1);
        int retries = toInt(task.get("retries"), 0);
        long at = ((Number) task.get("at")).longValue();
        log.info("[task {}] 启动 sku={} qty={} loop={} interval={}s maxTries={} conc={} retries={} at={}",
                tid, sku, qty, loop, interval, maxTries, conc, retries, task.get("at_str"));

        // 1) 预热 + 等待到点
        String prepTarget = null;
        if (Boolean.parseBoolean(String.valueOf(task.get("prep")))) {
            int prepSeconds = toInt(task.get("prep_seconds"), 3);
            long prepAt = at - prepSeconds * 1000L;
            long prepWait = prepAt - System.currentTimeMillis();
            log.info("[task {}] 预热模式：提前 {}s 打开结算页（剩余等待 {}ms）", tid, prepSeconds, Math.max(0, prepWait));
            if (prepWait > 0) {
                double pw = 0;
                while (pw < prepWait) {
                    if (cancelled(tid)) {
                        log.info("[task {}] 预热等待期间被取消", tid);
                        return;
                    }
                    double step = Math.min(1000, prepWait - pw);
                    sleep((long) step);
                    pw += step;
                }
            }
            try {
                Map<String, Object> pr = chrome.run(
                        () -> checkoutService.checkout(sku, qty, true), 40);
                if (Boolean.parseBoolean(String.valueOf(pr.getOrDefault("ok", false)))) {
                    prepTarget = String.valueOf(pr.get("target_id"));
                    log.info("[task {}] 预热结算页已打开 targetId={}", tid, prepTarget);
                } else {
                    log.warn("[task {}] 预热打开结算页失败: {}", tid, pr.get("error"));
                }
            } catch (Exception e) {
                prepTarget = null;
                log.warn("[task {}] 预热异常: {}", tid, e.getMessage());
            }
        }
        final String prepTargetFinal = prepTarget;
        long delay = at - System.currentTimeMillis();
        if (delay > 0) {
            log.info("[task {}] 距离开抢还有 {}ms，开始倒计时等待", tid, delay);
            double slept = 0;
            while (slept < delay) {
                if (cancelled(tid)) {
                    log.info("[task {}] 倒计时期间被取消", tid);
                    return;
                }
                double step = Math.min(1000, delay - slept);
                sleep((long) step);
                slept += step;
            }
        }
        if (cancelled(tid)) {
            return;
        }
        log.info("[task {}] 到点！开始提交", tid);
        long fireTime = System.currentTimeMillis();

        // 2) 单次模式
        if (!loop) {
            setStatus(tid, "running");
            Map<String, Object> res;
            if (prepTarget != null) {
                log.info("[task {}] 单次模式：复用预热页直接提交", tid);
                res = chrome.run(() -> submitService.submitOrder(sku, qty, false, prepTargetFinal), 40);
                if (!Boolean.parseBoolean(String.valueOf(res.getOrDefault("ok", false)))) {
                    log.warn("[task {}] 预热页提交失败，转为常规流程: {}", tid, res.get("error"));
                    res = chrome.run(() -> fire(sku, qty, conc, retries), 80);
                }
            } else {
                res = chrome.run(() -> fire(sku, qty, conc, retries), 80);
            }
            boolean ok = Boolean.parseBoolean(String.valueOf(res.getOrDefault("ok", false)));
            log.info("[task {}] 单次提交结束 ok={} order_id={} error={}", tid, ok, res.get("order_id"), res.get("error"));
            setStatus(tid, ok ? "done" : "error");
            setResult(tid, res);
            if (ok) {
                notifySuccess(task, res);
            }
            return;
        }

        // 3) 循环模式：先打预热一枪
        if (prepTarget != null) {
            setStatus(tid, "running");
            log.info("[task {}] 循环模式：先打预热一枪", tid);
            Map<String, Object> sp = chrome.run(() -> submitService.submitOrder(sku, qty, false, prepTargetFinal), 40);
            if (Boolean.parseBoolean(String.valueOf(sp.getOrDefault("ok", false)))) {
                log.info("[task {}] 预热一枪抢到！order_id={}", tid, sp.get("order_id"));
                synchronized (schedLock) {
                    Map<String, Object> t = scheduled.get(tid);
                    if (t != null) {
                        t.put("status", "done");
                        t.put("result", sp);
                        t.put("tries", 1);
                    }
                }
                notifySuccess(task, sp);
                return;
            } else {
                log.warn("[task {}] 预热一枪未中: {}，进入循环", tid, sp.get("error"));
            }
        }
        int tries = 0;
        int riskStreak = 0;
        String lastErr = "";
        while (true) {
            if (cancelled(tid)) {
                log.info("[task {}] 循环期间被取消", tid);
                return;
            }
            if (stopAfter > 0 && System.currentTimeMillis() - fireTime > stopAfter * 60000L) {
                log.warn("[task {}] 到点后已刷 {} 分钟仍未抢到，自动停止（共 {} 次）", tid, stopAfter, tries);
                synchronized (schedLock) {
                    Map<String, Object> t = scheduled.get(tid);
                    if (t != null) {
                        t.put("status", "error");
                        t.put("result", map("ok", false, "error",
                                "到点后已刷 " + stopAfter + " 分钟仍未抢到，自动停止", "tries", tries));
                    }
                }
                return;
            }
            tries++;
            setStatus(tid, "running");
            setTries(tid, tries);
            Map<String, Object> chk = chrome.run(() -> checkoutService.checkout(sku, qty, false), 40);
            String note;
            if (Boolean.parseBoolean(String.valueOf(chk.getOrDefault("ok", false)))) {
                if (Boolean.parseBoolean(String.valueOf(chk.getOrDefault("risk_control", false)))) {
                    riskStreak++;
                    double pause = Math.min(30.0, interval * 4 * riskStreak);
                    note = "⚠️ 账号被风控拦截，暂停 " + (int) pause + "s 后重试（已尝试 " + tries + " 次）";
                    lastErr = String.valueOf(chk.getOrDefault("text_snippet", chk.getOrDefault("error", "")));
                    log.warn("[task {}] 第{}次 风控拦截，暂停{}s | 文案: {}", tid, tries, (int) pause, lastErr);
                } else {
                    Map<String, Object> sub = chrome.run(() -> submitService.submitOrder(sku, qty, false, null), 40);
                    if (Boolean.parseBoolean(String.valueOf(sub.getOrDefault("ok", false)))) {
                        log.info("[task {}] 第{}次 抢到！order_id={}", tid, tries, sub.get("order_id"));
                        synchronized (schedLock) {
                            Map<String, Object> t = scheduled.get(tid);
                            if (t != null) {
                                t.put("status", "done");
                                t.put("result", sub);
                                t.put("tries", tries);
                            }
                        }
                        notifySuccess(task, sub);
                        return;
                    }
                    riskStreak = 0;
                    note = "第 " + tries + " 次未下单（" + sub.get("error") + "），" + (int) interval + "s 后重试";
                    lastErr = String.valueOf(sub.getOrDefault("error", ""));
                    log.info("[task {}] 第{}次 未下单: {}", tid, tries, sub.get("error"));
                }
            } else {
                riskStreak = 0;
                note = "第 " + tries + " 次结算页未打开（" + chk.get("error") + "），" + (int) interval + "s 后重试";
                lastErr = String.valueOf(chk.getOrDefault("error", ""));
                log.warn("[task {}] 第{}次 结算页未打开: {}", tid, tries, chk.get("error"));
            }
            setNote(tid, note, lastErr);
            if (maxTries > 0 && tries >= maxTries) {
                log.warn("[task {}] 已达最大尝试次数 {}，停止", tid, maxTries);
                synchronized (schedLock) {
                    Map<String, Object> t = scheduled.get(tid);
                    if (t != null) {
                        t.put("status", "error");
                        t.put("result", map("ok", false, "error", "已达最大尝试次数 " + maxTries, "tries", tries));
                    }
                }
                return;
            }
            sleep((long) (interval * 1000));
        }
    }

    /** 实际开火：单次+重试，或并发先开结算页再顺序点击。 */
    private Map<String, Object> fire(String sku, int qty, int concurrency, int retries) {
        if (concurrency <= 1) {
            return doSubmit(sku, qty, retries);
        }
        log.info("[fire] 并发模式 conc={} qty={}：先打开结算页", concurrency, qty);
        try {
            Map<String, Object> chk = checkoutService.checkout(sku, qty, false);
            if (!(Boolean) chk.getOrDefault("ok", false)) {
                return map("ok", false, "error", "打开结算页失败: " + chk.get("error"));
            }
        } catch (Exception e) {
            return map("ok", false, "error", "打开结算页失败: " + e.getMessage());
        }
        for (int i = 0; i < concurrency; i++) {
            if (i > 0) {
                sleep((long) (150 * i));
            }
            log.info("[fire] 并发第 {} 发", i + 1);
            Map<String, Object> r = submitService.submitOrder(sku, qty, false, null);
            if (Boolean.parseBoolean(String.valueOf(r.getOrDefault("ok", false)))) {
                return r;
            }
        }
        return map("ok", false, "error", "并发提交均未成功");
    }

    private Map<String, Object> doSubmit(String sku, int qty, int retries) {
        Map<String, Object> last = null;
        int attempts = 1 + Math.max(0, retries);
        log.info("[doSubmit] 单发+重试 qty={} retries={}（共{}次）", qty, retries, attempts);
        for (int attempt = 0; attempt < attempts; attempt++) {
            Map<String, Object> r = submitService.submitOrder(sku, qty, attempt > 0, null);
            if (Boolean.parseBoolean(String.valueOf(r.getOrDefault("ok", false)))) {
                return r;
            }
            log.info("[doSubmit] 第{}次未成功: {}", attempt + 1, r.get("error"));
            last = r;
        }
        if (retries == 0 && last != null && !Boolean.parseBoolean(String.valueOf(last.getOrDefault("ok", false)))) {
            log.info("[doSubmit] 单发未中，最后再 ensure_checkout 重试一次");
            Map<String, Object> r = submitService.submitOrder(sku, qty, true, null);
            if (Boolean.parseBoolean(String.valueOf(r.getOrDefault("ok", false)))) {
                return r;
            }
            last = r;
        }
        return last == null ? map("ok", false, "error", "提交失败") : last;
    }

    private void notifySuccess(Map<String, Object> task, Map<String, Object> res) {
        try {
            notifyService.notifySuccess(task, res);
        } catch (Exception e) {
            log.warn("推送成功提醒异常: {}", e.getMessage());
        }
    }

    // ---- 任务状态辅助 ----
    private boolean cancelled(String tid) {
        synchronized (schedLock) {
            Map<String, Object> t = scheduled.get(tid);
            if (t == null) {
                return false;
            }
            if (Boolean.parseBoolean(String.valueOf(t.getOrDefault("cancelled", false)))) {
                t.put("status", "cancelled");
                t.put("result", map("ok", false, "error", "已取消"));
                return true;
            }
            return false;
        }
    }

    private void setStatus(String tid, String status) {
        synchronized (schedLock) {
            Map<String, Object> t = scheduled.get(tid);
            if (t != null) {
                t.put("status", status);
            }
        }
    }

    private void setResult(String tid, Map<String, Object> res) {
        synchronized (schedLock) {
            Map<String, Object> t = scheduled.get(tid);
            if (t != null) {
                t.put("result", res);
            }
        }
    }

    private void setTries(String tid, int tries) {
        synchronized (schedLock) {
            Map<String, Object> t = scheduled.get(tid);
            if (t != null) {
                t.put("tries", tries);
            }
        }
    }

    private void setNote(String tid, String note, String lastErr) {
        synchronized (schedLock) {
            Map<String, Object> t = scheduled.get(tid);
            if (t != null) {
                t.put("note", note);
                t.put("last_error", lastErr);
            }
        }
    }

    // ---- 工具 ----
    private static Date parseScheduleTime(String s) throws ParseException {
        s = (s == null ? "" : s.trim()).replace("\"", "");
        Date now = new Date();
        String[] fmts = {"yyyy-MM-dd HH:mm:ss", "yyyy-MM-dd HH:mm",
                "yyyy/MM/dd HH:mm:ss", "yyyy/MM/dd HH:mm"};
        for (String f : fmts) {
            try {
                return new SimpleDateFormat(f).parse(s);
            } catch (ParseException ignored) {
            }
        }
        for (String f : new String[]{"HH:mm:ss", "HH:mm"}) {
            try {
                Date t = new SimpleDateFormat(f).parse(s);
                Calendar c = Calendar.getInstance();
                c.setTime(now);
                Calendar ct = Calendar.getInstance();
                ct.setTime(t);
                c.set(Calendar.HOUR_OF_DAY, ct.get(Calendar.HOUR_OF_DAY));
                c.set(Calendar.MINUTE, ct.get(Calendar.MINUTE));
                c.set(Calendar.SECOND, ct.get(Calendar.SECOND));
                c.set(Calendar.MILLISECOND, 0);
                if (c.getTime().before(now)) {
                    c.add(Calendar.DATE, 1);
                }
                return c.getTime();
            } catch (ParseException ignored) {
            }
        }
        throw new ParseException("无法解析时间，支持格式示例：2026-07-22 20:00:00 或 20:00", 0);
    }

    private static void sleep(long ms) {
        try {
            Thread.sleep(ms);
        } catch (InterruptedException ignored) {
        }
    }

    private static int toInt(Object v, int def) {
        if (v == null) {
            return def;
        }
        try {
            return Integer.parseInt(String.valueOf(v));
        } catch (Exception e) {
            return def;
        }
    }

    private static double toDouble(Object v, double def) {
        if (v == null) {
            return def;
        }
        try {
            return Double.parseDouble(String.valueOf(v));
        } catch (Exception e) {
            return def;
        }
    }

    private static Map<String, Object> map(Object... kv) {
        Map<String, Object> m = new LinkedHashMap<>();
        for (int i = 0; i < kv.length; i += 2) {
            m.put(String.valueOf(kv[i]), kv[i + 1]);
        }
        return m;
    }
}
