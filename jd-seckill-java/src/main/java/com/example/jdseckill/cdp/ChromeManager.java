package com.example.jdseckill.cdp;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

import java.io.BufferedReader;
import java.io.File;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.InetSocketAddress;
import java.net.Proxy;
import java.net.Socket;
import java.net.URI;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.Callable;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * 调试 Chrome 管理与低级 CDP 封装（等价 Python jd_cdp.py 的底层 + 账号 + 状态 + 解析）。
 * 所有浏览器操作统一提交到单线程 executor，避免 CDP 同一连接并发 recv 冲突。
 */
@Service
public class ChromeManager {

    private static final Logger log = LoggerFactory.getLogger(ChromeManager.class);
    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final int CDP_PORT = 9222;
    private static final int BROWSER_TIMEOUT = 30;

    // 单线程浏览器 worker：所有 CDP 操作串行执行
    private final ExecutorService browserExec = Executors.newSingleThreadExecutor(r -> {
        Thread t = new Thread(r, "browser-worker");
        t.setDaemon(true);
        return t;
    });

    private final Map<String, Object> loginCache = new HashMap<>();
    private String chromeUserData;
    private String accountsFile;

    public ChromeManager() {
        // 与运行中的 Python 版共享：父目录（jd-seckill）下的 chrome_jd / jd_accounts.json / jd_notify.json
        File parent = new File(System.getProperty("user.dir")).getParentFile();
        if (parent == null) {
            parent = new File(System.getProperty("user.dir"));
        }
        chromeUserData = new File(parent, "chrome_jd").getAbsolutePath();
        accountsFile = new File(parent, "jd_accounts.json").getAbsolutePath();
    }

    // ------------------------------------------------------------------
    // 单线程串行执行
    // ------------------------------------------------------------------
    public <T> T run(Callable<T> task, long timeoutSec) {
        Future<T> f = browserExec.submit(task);
        try {
            return f.get(timeoutSec, TimeUnit.SECONDS);
        } catch (Exception e) {
            f.cancel(true);
            if (e instanceof RuntimeException) {
                throw (RuntimeException) e;
            }
            throw new RuntimeException("浏览器操作失败/超时: " + e.getMessage(), e);
        }
    }

    // ------------------------------------------------------------------
    // HTTP 辅助（9222 走 NO_PROXY，避免公司代理把 localhost 绕出去）
    // ------------------------------------------------------------------
    private HttpURLConnection openConn(URL url) throws Exception {
        if ("127.0.0.1".equals(url.getHost()) || "localhost".equals(url.getHost())) {
            return (HttpURLConnection) url.openConnection(Proxy.NO_PROXY);
        }
        return (HttpURLConnection) url.openConnection();
    }

    private JsonNode httpGetJson(String url) throws Exception {
        HttpURLConnection conn = openConn(new URL(url));
        conn.setConnectTimeout(5000);
        conn.setReadTimeout(5000);
        try (BufferedReader br = new BufferedReader(
                new InputStreamReader(conn.getInputStream(), StandardCharsets.UTF_8))) {
            StringBuilder sb = new StringBuilder();
            String line;
            while ((line = br.readLine()) != null) {
                sb.append(line);
            }
            return MAPPER.readTree(sb.toString());
        } finally {
            conn.disconnect();
        }
    }

    // ------------------------------------------------------------------
    // 低级
    // ------------------------------------------------------------------
    public String getBrowserWs() throws Exception {
        JsonNode v = httpGetJson("http://127.0.0.1:" + CDP_PORT + "/json/version");
        return v.path("webSocketDebuggerUrl").asText();
    }

    @SuppressWarnings("unchecked")
    public List<Map<String, Object>> listPages() throws Exception {
        JsonNode arr = httpGetJson("http://127.0.0.1:" + CDP_PORT + "/json");
        List<Map<String, Object>> out = new ArrayList<>();
        if (arr.isArray()) {
            for (JsonNode n : arr) {
                out.add(MAPPER.convertValue(n, Map.class));
            }
        }
        return out;
    }

    public Map<String, Object> findPage(String keyword) throws Exception {
        for (Map<String, Object> p : listPages()) {
            if ("page".equals(p.get("type")) && String.valueOf(p.get("url")).contains(keyword)) {
                return p;
            }
        }
        return null;
    }

    public String pageWsByTarget(String targetId, int tries) throws Exception {
        for (int i = 0; i < tries; i++) {
            for (Map<String, Object> p : listPages()) {
                if (targetId.equals(p.get("id")) && p.get("webSocketDebuggerUrl") != null) {
                    return String.valueOf(p.get("webSocketDebuggerUrl"));
                }
            }
            Thread.sleep(300);
        }
        return null;
    }

    public JsonNode browserSend(String method, Map<String, Object> params) throws Exception {
        String ws = getBrowserWs();
        CdpClient c = new CdpClient(new URI(ws));
        c.connectBlocking(10, TimeUnit.SECONDS);
        try {
            return c.send(method, params);
        } finally {
            try {
                c.close();
            } catch (Exception ignored) {
            }
        }
    }

    public String createTarget(String url, boolean background) throws Exception {
        Map<String, Object> params = new HashMap<>();
        params.put("url", url);
        if (background) {
            params.put("background", true);
        }
        JsonNode r = browserSend("Target.createTarget", params);
        return r.path("result").path("targetId").asText();
    }

    public void closeTarget(String targetId) {
        try {
            browserSend("Target.closeTarget", mapOf("targetId", targetId));
        } catch (Exception ignored) {
        }
    }

    public PageSession openPage(String url, boolean background) throws Exception {
        String tid = createTarget(url, background);
        String ws = pageWsByTarget(tid, 12);
        if (ws == null) {
            throw new RuntimeException("无法获取新标签页的 CDP 连接");
        }
        CdpClient client = new CdpClient(new URI(ws));
        client.connectBlocking(10, TimeUnit.SECONDS);
        client.enableDomains();
        return new PageSession(tid, client);
    }

    public boolean isPortOpen() {
        try (Socket s = new Socket()) {
            s.connect(new InetSocketAddress("127.0.0.1", CDP_PORT), 2000);
            return true;
        } catch (Exception e) {
            return false;
        }
    }

    // ------------------------------------------------------------------
    // 启动 / 关闭调试 Chrome（复用 chrome_jd 登录态）
    // ------------------------------------------------------------------
    public Map<String, Object> findChrome() {
        String[] candidates = {
                "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
                "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
                System.getenv("LOCALAPPDATA") + "\\Google\\Chrome\\Application\\chrome.exe",
                System.getenv("PROGRAMFILES") + "\\Google\\Chrome\\Application\\chrome.exe",
                System.getenv("PROGRAMFILES(X86)") + "\\Google\\Chrome\\Application\\chrome.exe",
        };
        for (String c : candidates) {
            if (c != null && new File(c).exists()) {
                return mapOf("ok", true, "path", c);
            }
        }
        return mapOf("ok", false, "error", "未找到 chrome.exe");
    }

    public Map<String, Object> launchDebugChrome(boolean headless) throws Exception {
        if (isPortOpen()) {
            return mapOf("ok", true, "already_running", true,
                    "message", "调试 Chrome 已在 " + CDP_PORT + " 端口运行", "headless", headless);
        }
        Map<String, Object> fc = findChrome();
        if (!(Boolean) fc.get("ok")) {
            return mapOf("ok", false, "error", fc.get("error"));
        }
        String chrome = String.valueOf(fc.get("path"));
        new File(chromeUserData).mkdirs();
        List<String> flags = new ArrayList<>();
        flags.add(chrome);
        flags.add("--remote-debugging-port=" + CDP_PORT);
        flags.add("--remote-allow-origins=*");
        flags.add("--user-data-dir=" + chromeUserData);
        flags.add("--no-first-run");
        flags.add("--no-default-browser-check");
        if (headless) {
            flags.add("--headless=new");
            flags.add("--disable-gpu");
            flags.add("--no-startup-window");
            flags.add("--disable-backgrounding-occluded-windows");
            flags.add("--disable-renderer-backgrounding");
            flags.add("--disable-features=Translate,BackForwardCache");
        } else {
            flags.add("--background");
        }
        ProcessBuilder pb = new ProcessBuilder(flags);
        pb.redirectErrorStream(true);
        pb.redirectOutput(new File("chrome_debug.log"));
        pb.start();
        for (int i = 0; i < 25; i++) {
            Thread.sleep(400);
            if (isPortOpen()) {
                return mapOf("ok", true, "already_running", false,
                        "message", "调试 Chrome 已启动", "chrome_path", chrome,
                        "user_data_dir", chromeUserData, "headless", headless);
            }
        }
        return mapOf("ok", false, "error", "Chrome 已拉起但 9222 端口未在 10s 内响应");
    }

    public Map<String, Object> closeDebugChrome() {
        try {
            browserSend("Browser.close", null);
            return mapOf("ok", true);
        } catch (Exception e) {
            return mapOf("ok", false, "error", e.getMessage());
        }
    }

    public Map<String, Object> restartDebugChrome(boolean headless) throws Exception {
        closeDebugChrome();
        for (int i = 0; i < 20; i++) {
            Thread.sleep(300);
            if (!isPortOpen()) {
                break;
            }
        }
        return launchDebugChrome(headless);
    }

    // ------------------------------------------------------------------
    // 登录态探测（带缓存）
    // ------------------------------------------------------------------
    public synchronized void invalidateLoginCache() {
        loginCache.clear();
    }

    private boolean probeLoggedIn() throws Exception {
        String tid = createTarget("https://m.jd.com", true);
        String ws = pageWsByTarget(tid, 12);
        if (ws == null) {
            return false;
        }
        CdpClient pg = new CdpClient(new URI(ws));
        pg.connectBlocking(10, TimeUnit.SECONDS);
        pg.enableDomains();
        Thread.sleep(1500);
        JsonNode cookies = pg.getCookies();
        boolean has = false;
        for (JsonNode c : cookies.path("result").path("cookies")) {
            if ("pt_key".equals(c.path("name").asText())) {
                has = true;
                break;
            }
        }
        try {
            pg.close();
        } catch (Exception ignored) {
        }
        closeTarget(tid);
        return has;
    }

    public Map<String, Object> chromeStatus(boolean force) {
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("chrome_connected", false);
        out.put("logged_in", false);
        out.put("has_checkout", false);
        out.put("has_payment", false);
        out.put("detail", "");
        out.put("active_account", getActiveAccount().get("name"));
        try {
            listPages();
            out.put("chrome_connected", true);
        } catch (Exception e) {
            out.put("detail", "无法连接 9222: " + e.getMessage());
            invalidateLoginCache();
            return out;
        }
        boolean needProbe = force || loginCache.get("val") == null
                || (System.currentTimeMillis() - (Long) loginCache.getOrDefault("ts", 0L)) > 15000;
        if (needProbe) {
            try {
                boolean logged = probeLoggedIn();
                loginCache.put("val", logged);
                loginCache.put("ts", System.currentTimeMillis());
                out.put("logged_in", logged);
            } catch (Exception e) {
                out.put("detail", "登录态检测失败: " + e.getMessage());
            }
        } else {
            out.put("logged_in", loginCache.get("val"));
        }
        try {
            out.put("has_checkout", findPage("trade.m.jd.com/pay") != null);
            out.put("has_payment", findPage("mpay.m.jd.com") != null);
        } catch (Exception ignored) {
        }
        return out;
    }

    public Map<String, Object> debugCookies() {
        try {
            String tid = createTarget("https://m.jd.com", true);
            String ws = pageWsByTarget(tid, 15);
            if (ws == null) {
                return mapOf("ok", false, "error", "无法连接探针页", "cookies", new ArrayList<>());
            }
            CdpClient pg = new CdpClient(new URI(ws));
            pg.connectBlocking(10, TimeUnit.SECONDS);
            pg.enableDomains();
            Thread.sleep(1500);
            List<Map<String, Object>> out = new ArrayList<>();
            boolean hasPt = false;
            for (JsonNode c : pg.getCookies().path("result").path("cookies")) {
                Map<String, Object> m = new HashMap<>();
                m.put("domain", c.path("domain").asText());
                m.put("name", c.path("name").asText());
                m.put("path", c.path("path").asText());
                m.put("httpOnly", c.path("httpOnly").asBoolean());
                out.add(m);
                if ("pt_key".equals(c.path("name").asText())) {
                    hasPt = true;
                }
            }
            try {
                pg.close();
            } catch (Exception ignored) {
            }
            closeTarget(tid);
            Map<String, Object> r = new LinkedHashMap<>();
            r.put("ok", true);
            r.put("count", out.size());
            r.put("has_pt_key", hasPt);
            r.put("cookies", out);
            return r;
        } catch (Exception e) {
            return mapOf("ok", false, "error", e.getMessage(), "cookies", new ArrayList<>());
        }
    }

    // ------------------------------------------------------------------
    // 打开登录页 / 解析分享短链
    // ------------------------------------------------------------------
    public Map<String, Object> openLoginPage(String sku, int qty) throws Exception {
        String pay = "https://trade.m.jd.com/pay?commlist=" + sku + ",," + qty + "," + sku + ",1,0,0";
        String loginUrl = "https://plogin.m.jd.com/login/login?appid=web&returnurl="
                + java.net.URLEncoder.encode(pay, "UTF-8");
        PageSession ps = openPage(loginUrl, false);
        Thread.sleep(1000);
        String url = ps.client.evalString("location.href");
        try {
            ps.client.close();
        } catch (Exception ignored) {
        }
        invalidateLoginCache();
        return mapOf("ok", true, "target_id", ps.targetId, "url", url);
    }

    private static final Pattern SKU_URL = Pattern.compile("/(?:product|item(?:\\.m)?)/(\\d{6,})");
    private static final Pattern SKU_COMM = Pattern.compile("commlist=(\\d{6,})");
    private static final Pattern SKU_RAW = Pattern.compile("\\d{6,}");

    private String extractSkuFromUrl(String u) {
        if (u == null) {
            return null;
        }
        Matcher m = SKU_URL.matcher(u);
        if (m.find()) {
            return m.group(1);
        }
        m = SKU_COMM.matcher(u);
        if (m.find()) {
            return m.group(1);
        }
        return null;
    }

    private String extractJdUrl(String text) {
        if (text == null) {
            return "";
        }
        String t = text.trim();
        Matcher m = Pattern.compile("https?://[^\\s，。、；;：:（）()\\[\\]【】「」]+").matcher(t);
        while (m.find()) {
            String u = m.group(0);
            if (u.contains("3.cn") || u.contains("jd.com")) {
                return u;
            }
        }
        Matcher m2 = Pattern.compile("(?:3\\.cn|[\\w.-]*jd\\.com)[^\\s，。、；;：:（）()\\[\\]【】「」]+").matcher(t);
        if (m2.find()) {
            String frag = m2.group(0);
            return frag.startsWith("http") ? frag : "https://" + frag;
        }
        return t;
    }

    public Map<String, Object> resolveShareLink(String inUrl) {
        try {
            String s = (inUrl == null ? "" : inUrl).trim();
            if (s.isEmpty()) {
                return mapOf("ok", false, "error", "链接为空");
            }
            s = extractJdUrl(s);
            // 已含 SKU
            String sku = extractSkuFromUrl(s);
            if (sku != null) {
                return mapOf("ok", true, "sku", sku, "from", "url_direct");
            }
            if (SKU_RAW.matcher(s).matches()) {
                return mapOf("ok", true, "sku", s, "from", "raw_sku");
            }
            // 纯 HTTP 跟 302 / meta refresh
            Map<String, Object> r = resolveViaHttp(s);
            if (r != null) {
                return r;
            }
            // 浏览器兜底（少数需 JS 二次跳转）
            if (!s.contains("jd.com") && !s.contains("3.cn") && !s.contains("u.jd.com")) {
                return mapOf("ok", false, "error", "无法识别的链接/文本");
            }
            String tid = createTarget(s, true);
            String ws = pageWsByTarget(tid, 12);
            if (ws == null) {
                return mapOf("ok", false, "error", "无法打开分享链接");
            }
            CdpClient pg = new CdpClient(new URI(ws));
            pg.connectBlocking(10, TimeUnit.SECONDS);
            pg.enableDomains();
            String finalUrl = "";
            try {
                for (int i = 0; i < 20; i++) {
                    Thread.sleep(800);
                    try {
                        finalUrl = pg.evalString("location.href");
                    } catch (Exception ignored) {
                    }
                    Matcher mm = SKU_URL.matcher(finalUrl);
                    if (mm.find()) {
                        try {
                            pg.close();
                        } catch (Exception ignored) {
                        }
                        closeTarget(tid);
                        return mapOf("ok", true, "sku", mm.group(1), "from", "browser", "final_url", finalUrl);
                    }
                }
            } finally {
                try {
                    pg.close();
                } catch (Exception ignored) {
                }
                closeTarget(tid);
            }
            return mapOf("ok", false, "error", "短链未跳转到商品页");
        } catch (Exception e) {
            return mapOf("ok", false, "error", "解析失败: " + e.getMessage());
        }
    }

    private Map<String, Object> resolveViaHttp(String s) {
        String[] uas = {
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        + "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
                        + "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 "
                        + "Mobile/15E148 Safari/604.1",
        };
        for (String ua : uas) {
            try {
                HttpURLConnection conn = openConn(new URL(s));
                conn.setInstanceFollowRedirects(false);
                conn.setConnectTimeout(8000);
                conn.setReadTimeout(8000);
                conn.setRequestProperty("User-Agent", ua);
                int code = conn.getResponseCode();
                String finalUrl = conn.getHeaderField("Location");
                if (finalUrl == null) {
                    finalUrl = conn.getURL().toString();
                }
                conn.disconnect();
                if (finalUrl != null && (finalUrl.contains("cfe.m.jd.com") || finalUrl.contains("risk_handler"))) {
                    Matcher mm = Pattern.compile("returnurl=([^&]+)").matcher(finalUrl);
                    if (mm.find()) {
                        String ru = java.net.URLDecoder.decode(mm.group(1), "UTF-8");
                        String sk = extractSkuFromUrl(ru);
                        if (sk != null) {
                            return mapOf("ok", true, "sku", sk, "from", "http_risk_handler", "final_url", ru);
                        }
                    }
                    continue;
                }
                String sk = extractSkuFromUrl(finalUrl);
                if (sk != null) {
                    return mapOf("ok", true, "sku", sk, "from", "http_followed", "final_url", finalUrl);
                }
            } catch (Exception ignored) {
            }
        }
        return null;
    }

    // ------------------------------------------------------------------
    // 多账号（每个京东账号 = 一个独立 Chrome profile 目录）
    // ------------------------------------------------------------------
    @SuppressWarnings("unchecked")
    public Map<String, Object> loadAccounts() {
        try {
            File f = new File(accountsFile);
            if (!f.exists()) {
                return defaultAccounts();
            }
            Map<String, Object> d = MAPPER.readValue(f, Map.class);
            if (!(d.get("accounts") instanceof List) || ((List<?>) d.get("accounts")).isEmpty()) {
                return defaultAccounts();
            }
            List<Map<String, Object>> accs = (List<Map<String, Object>>) d.get("accounts");
            List<String> names = new ArrayList<>();
            for (Map<String, Object> a : accs) {
                names.add(String.valueOf(a.get("name")));
            }
            if (!names.contains(d.get("active"))) {
                d.put("active", names.get(0));
            }
            return d;
        } catch (Exception e) {
            return defaultAccounts();
        }
    }

    private Map<String, Object> defaultAccounts() {
        Map<String, Object> d = new LinkedHashMap<>();
        d.put("active", "默认账号");
        List<Map<String, Object>> accs = new ArrayList<>();
        Map<String, Object> a = new LinkedHashMap<>();
        a.put("name", "默认账号");
        a.put("profile", "chrome_jd");
        a.put("note", "");
        accs.add(a);
        d.put("accounts", accs);
        return d;
    }

    public void saveAccounts(Map<String, Object> d) {
        try {
            MAPPER.writerWithDefaultPrettyPrinter().writeValue(new File(accountsFile), d);
        } catch (Exception e) {
            log.warn("写入 jd_accounts.json 失败: {}", e.getMessage());
        }
    }

    @SuppressWarnings("unchecked")
    public Map<String, Object> getActiveAccount() {
        Map<String, Object> d = loadAccounts();
        String active = String.valueOf(d.get("active"));
        List<Map<String, Object>> accs = (List<Map<String, Object>>) d.get("accounts");
        for (Map<String, Object> a : accs) {
            if (active.equals(a.get("name"))) {
                return a;
            }
        }
        return accs.get(0);
    }

    public Map<String, Object> listAccounts() {
        Map<String, Object> d = loadAccounts();
        Map<String, Object> r = new LinkedHashMap<>();
        r.put("ok", true);
        r.put("active", d.get("active"));
        r.put("accounts", d.get("accounts"));
        return r;
    }

    public Map<String, Object> addAccount(String name, String note) {
        name = (name == null ? "" : name).trim();
        if (name.isEmpty()) {
            return mapOf("ok", false, "error", "账号名不能为空");
        }
        Map<String, Object> d = loadAccounts();
        @SuppressWarnings("unchecked")
        List<Map<String, Object>> accs = (List<Map<String, Object>>) d.get("accounts");
        for (Map<String, Object> a : accs) {
            if (name.equals(a.get("name"))) {
                return mapOf("ok", false, "error", "账号名已存在");
            }
        }
        String base = "chrome_jd_" + name.replaceAll("[^0-9A-Za-z一-鿿]+", "_").replaceAll("^_|_$", "");
        if (base.isEmpty()) {
            base = "chrome_jd_acct";
        }
        String profile = base;
        java.util.Set<String> existing = new java.util.HashSet<>();
        for (Map<String, Object> a : accs) {
            existing.add(String.valueOf(a.get("profile")));
        }
        int i = 1;
        while (existing.contains(profile)) {
            profile = base + "_" + i;
            i++;
        }
        Map<String, Object> a = new LinkedHashMap<>();
        a.put("name", name);
        a.put("profile", profile);
        a.put("note", note == null ? "" : note);
        accs.add(a);
        saveAccounts(d);
        return mapOf("ok", true, "name", name, "profile", profile,
                "message", "已新增账号「" + name + "」，对应 Chrome 档案 " + profile);
    }

    public Map<String, Object> switchAccount(String name, boolean headless) throws Exception {
        Map<String, Object> d = loadAccounts();
        @SuppressWarnings("unchecked")
        List<Map<String, Object>> accs = (List<Map<String, Object>>) d.get("accounts");
        Map<String, Object> acc = null;
        for (Map<String, Object> a : accs) {
            if (name.equals(a.get("name"))) {
                acc = a;
                break;
            }
        }
        if (acc == null) {
            return mapOf("ok", false, "error", "账号不存在");
        }
        d.put("active", name);
        saveAccounts(d);
        closeDebugChrome();
        for (int i = 0; i < 20; i++) {
            Thread.sleep(300);
            if (!isPortOpen()) {
                break;
            }
        }
        invalidateLoginCache();
        // 用该账号 profile 重启
        String profileDir = new File(new File(System.getProperty("user.dir")).getParentFile(),
                String.valueOf(acc.get("profile"))).getAbsolutePath();
        Map<String, Object> r = launchWithProfile(headless, profileDir);
        r.put("name", name);
        return r;
    }

    private Map<String, Object> launchWithProfile(boolean headless, String profileDir) throws Exception {
        if (isPortOpen()) {
            return mapOf("ok", true, "already_running", true, "headless", headless);
        }
        Map<String, Object> fc = findChrome();
        if (!(Boolean) fc.get("ok")) {
            return mapOf("ok", false, "error", fc.get("error"));
        }
        String chrome = String.valueOf(fc.get("path"));
        new File(profileDir).mkdirs();
        List<String> flags = new ArrayList<>();
        flags.add(chrome);
        flags.add("--remote-debugging-port=" + CDP_PORT);
        flags.add("--remote-allow-origins=*");
        flags.add("--user-data-dir=" + profileDir);
        flags.add("--no-first-run");
        flags.add("--no-default-browser-check");
        if (headless) {
            flags.add("--headless=new");
            flags.add("--disable-gpu");
            flags.add("--no-startup-window");
        } else {
            flags.add("--background");
        }
        ProcessBuilder pb = new ProcessBuilder(flags);
        pb.redirectErrorStream(true);
        pb.redirectOutput(new File("chrome_debug.log"));
        pb.start();
        for (int i = 0; i < 25; i++) {
            Thread.sleep(400);
            if (isPortOpen()) {
                return mapOf("ok", true, "already_running", false, "headless", headless, "user_data_dir", profileDir);
            }
        }
        return mapOf("ok", false, "error", "Chrome 已拉起但 9222 端口未在 10s 内响应");
    }

    public Map<String, Object> logoutAccount(String name) {
        Map<String, Object> d = loadAccounts();
        String target = (name == null || name.isEmpty()) ? String.valueOf(d.get("active")) : name;
        @SuppressWarnings("unchecked")
        List<Map<String, Object>> accs = (List<Map<String, Object>>) d.get("accounts");
        Map<String, Object> acc = null;
        for (Map<String, Object> a : accs) {
            if (target.equals(a.get("name"))) {
                acc = a;
                break;
            }
        }
        if (acc == null) {
            return mapOf("ok", false, "error", "账号不存在");
        }
        String profileDir = new File(new File(System.getProperty("user.dir")).getParentFile(),
                String.valueOf(acc.get("profile"))).getAbsolutePath();
        if (target.equals(d.get("active"))) {
            try {
                closeDebugChrome();
            } catch (Exception ignored) {
            }
            for (int i = 0; i < 20; i++) {
                try {
                    Thread.sleep(300);
                } catch (Exception ignored) {
                }
                if (!isPortOpen()) {
                    break;
                }
            }
        }
        List<String> removed = new ArrayList<>();
        for (String fn : new String[]{"Cookies", "Cookies-journal"}) {
            File p = new File(profileDir, fn);
            if (p.exists()) {
                if (p.delete()) {
                    removed.add(fn);
                }
            }
        }
        invalidateLoginCache();
        Map<String, Object> r = new LinkedHashMap<>();
        r.put("ok", true);
        r.put("account", acc.get("name"));
        r.put("removed", removed);
        r.put("message", "已清除「" + acc.get("name") + "」的京东登录态，请重新登录");
        return r;
    }

    // ------------------------------------------------------------------
    // 工具
    // ------------------------------------------------------------------
    @SuppressWarnings("unchecked")
    private static Map<String, Object> mapOf(Object... kv) {
        Map<String, Object> m = new LinkedHashMap<>();
        for (int i = 0; i < kv.length; i += 2) {
            m.put(String.valueOf(kv[i]), kv[i + 1]);
        }
        return m;
    }

}
