package com.example.jdseckill.controller;

import com.example.jdseckill.cdp.ChromeManager;
import com.example.jdseckill.service.CheckoutService;
import com.example.jdseckill.service.NotifyService;
import com.example.jdseckill.service.ScheduleService;
import com.example.jdseckill.service.SubmitService;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpHeaders;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RestController;

import javax.annotation.Resource;
import java.nio.charset.StandardCharsets;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * REST 接口（等价 Python jd_gui_server 的 Handler.do_POST）。
 * 所有浏览器操作统一通过 ChromeManager.run（单线程串行）执行。
 */
@RestController
public class ApiController {

    private static final Logger log = LoggerFactory.getLogger(ApiController.class);

    @Resource
    private ChromeManager chrome;
    @Resource
    private CheckoutService checkoutService;
    @Resource
    private SubmitService submitService;
    @Resource
    private ScheduleService scheduleService;
    @Resource
    private NotifyService notifyService;

    private boolean currentHeadless = true;

    private int toInt(Object v, int def) {
        try {
            return v == null ? def : Integer.parseInt(String.valueOf(v));
        } catch (Exception e) {
            return def;
        }
    }

    @PostMapping("/api/status")
    public Map<String, Object> status(@RequestBody(required = false) Map<String, Object> data) {
        final boolean force = data != null
                && Boolean.parseBoolean(String.valueOf(data.getOrDefault("force", false)));
        return chrome.run(() -> chrome.chromeStatus(force), 30);
    }

    @PostMapping("/api/launch_chrome")
    public Map<String, Object> launch(@RequestBody Map<String, Object> data) {
        boolean hl = Boolean.parseBoolean(String.valueOf(data.getOrDefault("headless", true)));
        Map<String, Object> v;
        if (currentHeadless != hl) {
            v = chrome.run(() -> chrome.restartDebugChrome(hl), 40);
        } else {
            v = chrome.run(() -> chrome.launchDebugChrome(hl), 40);
        }
        currentHeadless = hl;
        return v;
    }

    @PostMapping("/api/open_login")
    public Map<String, Object> openLogin(@RequestBody Map<String, Object> data) {
        String sku = String.valueOf(data.getOrDefault("sku", "100342780502"));
        int qty = toInt(data.get("qty"), 1);
        if (currentHeadless) {
            chrome.run(() -> chrome.restartDebugChrome(false), 40);
            currentHeadless = false;
        }
        return chrome.run(() -> chrome.openLoginPage(sku, qty), 40);
    }

    @PostMapping("/api/checkout")
    public Map<String, Object> checkout(@RequestBody Map<String, Object> data) {
        String sku = String.valueOf(data.getOrDefault("sku", "100342780502"));
        int qty = toInt(data.get("qty"), 1);
        return chrome.run(() -> checkoutService.checkout(sku, qty, false), 40);
    }

    @PostMapping("/api/checkout_retry")
    public Map<String, Object> checkoutRetry(@RequestBody Map<String, Object> data) {
        String sku = String.valueOf(data.getOrDefault("sku", "100342780502"));
        int qty = toInt(data.get("qty"), 1);
        return chrome.run(() -> checkoutService.retryCheckout(sku, qty), 40);
    }

    @PostMapping("/api/resolve")
    public Map<String, Object> resolve(@RequestBody Map<String, Object> data) {
        return chrome.run(() -> chrome.resolveShareLink(String.valueOf(data.getOrDefault("url", ""))), 40);
    }

    @PostMapping("/api/submit")
    public Map<String, Object> submit(@RequestBody Map<String, Object> data) {
        String sku = String.valueOf(data.getOrDefault("sku", "100342780502"));
        int qty = toInt(data.get("qty"), 1);
        int conc = Math.max(1, toInt(data.get("concurrency"), 1));
        int retries = Math.max(0, toInt(data.get("retries"), 0));
        log.info("[API] 立即抢购请求 sku={} qty={} concurrency={} retries={}", sku, qty, conc, retries);
        Map<String, Object> res;
        if (conc <= 1) {
            // 单次+重试：优先复用已有结算页（最快），失败再 ensure_checkout
            res = chrome.run(() -> {
                Map<String, Object> r = submitService.submitOrder(sku, qty, false, null);
                if (!Boolean.parseBoolean(String.valueOf(r.getOrDefault("ok", false)))) {
                    r = submitService.submitOrder(sku, qty, true, null);
                }
                return r;
            }, 80);
        } else {
            res = chrome.run(() -> {
                Map<String, Object> chk = checkoutService.checkout(sku, qty, false);
                if (!(Boolean) chk.getOrDefault("ok", false)) {
                    return map("ok", false, "error", "打开结算页失败: " + chk.get("error"));
                }
                for (int i = 0; i < conc; i++) {
                    if (i > 0) {
                        try {
                            Thread.sleep(150 * i);
                        } catch (InterruptedException ignored) {
                        }
                    }
                    Map<String, Object> r = submitService.submitOrder(sku, qty, false, null);
                    if (Boolean.parseBoolean(String.valueOf(r.getOrDefault("ok", false)))) {
                        return r;
                    }
                }
                return map("ok", false, "error", "并发提交均未成功");
            }, 80);
        }
        // 抢到即推微信（立即抢购也会触发，与定时任务一致）
        boolean ok = Boolean.parseBoolean(String.valueOf(res.getOrDefault("ok", false)));
        log.info("[API] 立即抢购结果 sku={} ok={} order_id={} error={}",
                sku, ok, res.get("order_id"), res.get("error"));
        if (ok) {
            Map<String, Object> task = new LinkedHashMap<>();
            task.put("sku", sku);
            task.put("qty", qty);
            task.put("at_str", "立即抢购");
            notifyService.notifySuccess(task, res);
        }
        return res;
    }

    @PostMapping("/api/submit_schedule")
    public Map<String, Object> submitSchedule(@RequestBody Map<String, Object> data) {
        try {
            return scheduleService.scheduleSubmit(data);
        } catch (Exception e) {
            return map("ok", false, "error", e.getMessage());
        }
    }

    @PostMapping("/api/submit_tasks")
    public Map<String, Object> submitTasks(@RequestBody Map<String, Object> data) {
        return scheduleService.listTasks();
    }

    @PostMapping("/api/submit_cancel")
    public Map<String, Object> submitCancel(@RequestBody Map<String, Object> data) {
        return scheduleService.cancelTask(String.valueOf(data.getOrDefault("task_id", "")));
    }

    @PostMapping("/api/notify_config")
    public Map<String, Object> notifyConfig(@RequestBody Map<String, Object> data) {
        if (Boolean.parseBoolean(String.valueOf(data.getOrDefault("test", false)))) {
            return notifyService.notify("🔔 测试推送", "如果你收到这条微信，说明手机提醒已配置成功。");
        }
        if (data.containsKey("serverchan_key")) {
            String key = String.valueOf(data.get("serverchan_key") == null ? "" : data.get("serverchan_key")).trim();
            notifyService.saveKey(key);
            return map("ok", true, "configured", !key.isEmpty());
        }
        return notifyService.status();
    }

    @PostMapping("/api/accounts")
    public Map<String, Object> accounts() {
        return chrome.run(() -> chrome.listAccounts(), 30);
    }

    @PostMapping("/api/account_switch")
    public Map<String, Object> accountSwitch(@RequestBody Map<String, Object> data) {
        return chrome.run(() -> chrome.switchAccount(String.valueOf(data.getOrDefault("name", "")), currentHeadless), 40);
    }

    @PostMapping("/api/account_add")
    public Map<String, Object> accountAdd(@RequestBody Map<String, Object> data) {
        return chrome.run(() -> chrome.addAccount(String.valueOf(data.getOrDefault("name", "")),
                String.valueOf(data.getOrDefault("note", ""))), 30);
    }

    @PostMapping("/api/account_logout")
    public Map<String, Object> accountLogout(@RequestBody Map<String, Object> data) {
        return chrome.run(() -> chrome.logoutAccount(String.valueOf(data.getOrDefault("name", ""))), 30);
    }

    @PostMapping("/api/debug_cookies")
    public Map<String, Object> debugCookies() {
        return chrome.run(() -> chrome.debugCookies(), 30);
    }

    @GetMapping({"/", "/index.html"})
    public ResponseEntity<String> index() {
        try {
            // 用类加载器读取 classpath 资源：在 fat jar 内也能正确定位 BOOT-INF/classes/static/index.html
            java.io.InputStream in = getClass().getClassLoader().getResourceAsStream("static/index.html");
            if (in == null) {
                return ResponseEntity.status(404)
                        .contentType(MediaType.valueOf("text/html; charset=utf-8"))
                        .body("<h1>index.html 未找到</h1>");
            }
            String html;
            try (java.io.InputStream sin = in) {
                java.io.ByteArrayOutputStream bos = new java.io.ByteArrayOutputStream();
                byte[] buf = new byte[8192];
                int n;
                while ((n = sin.read(buf)) != -1) {
                    bos.write(buf, 0, n);
                }
                html = new String(bos.toByteArray(), StandardCharsets.UTF_8);
            }
            return ResponseEntity.ok()
                    .header(HttpHeaders.CACHE_CONTROL, "no-store, no-cache, must-revalidate, max-age=0")
                    .header(HttpHeaders.PRAGMA, "no-cache")
                    .header(HttpHeaders.EXPIRES, "0")
                    .contentType(MediaType.valueOf("text/html; charset=utf-8"))
                    .body(html);
        } catch (Exception e) {
            return ResponseEntity.status(404)
                    .contentType(MediaType.valueOf("text/html; charset=utf-8"))
                    .body("<h1>index.html 未找到: " + e.getMessage() + "</h1>");
        }
    }

    private static Map<String, Object> map(Object... kv) {
        Map<String, Object> m = new java.util.LinkedHashMap<>();
        for (int i = 0; i < kv.length; i += 2) {
            m.put(String.valueOf(kv[i]), kv[i + 1]);
        }
        return m;
    }
}
