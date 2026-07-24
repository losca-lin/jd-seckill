package com.example.jdseckill.cdp;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.java_websocket.client.WebSocketClient;
import org.java_websocket.handshake.ServerHandshake;

import java.net.URI;
import java.nio.file.Files;
import java.nio.file.Paths;
import java.util.Base64;
import java.util.HashMap;
import java.util.Map;
import java.util.Queue;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ConcurrentLinkedQueue;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;
import java.util.concurrent.atomic.AtomicLong;

/**
 * 单页面 CDP 客户端（等价 Python 版 CDPPage）。
 * 基于 Java-WebSocket 直连 page 的 DevTools WebSocket，1:1 移植 websocket-client 的 send/eval/navigate 逻辑。
 */
public class CdpClient extends WebSocketClient {

    private static final ObjectMapper MAPPER = new ObjectMapper();
    private final AtomicLong idGen = new AtomicLong(1);
    private final Map<Long, CompletableFuture<JsonNode>> pending = new ConcurrentHashMap<>();
    private final Queue<JsonNode> events = new ConcurrentLinkedQueue<>();

    public CdpClient(URI uri) {
        super(uri);
    }

    @Override
    public void onOpen(ServerHandshake handshakedata) {
        // 连接建立后由调用方显式 enable 域
    }

    @Override
    public void onMessage(String message) {
        try {
            JsonNode node = MAPPER.readTree(message);
            if (node.has("id")) {
                long id = node.get("id").asLong();
                CompletableFuture<JsonNode> f = pending.remove(id);
                if (f != null) {
                    f.complete(node);
                }
            } else {
                events.offer(node);
            }
        } catch (Exception ignored) {
            // 忽略无法解析的消息
        }
    }

    @Override
    public void onClose(int code, String reason, boolean remote) {
        // 关闭时取消所有挂起请求
        pending.forEach((id, f) -> f.cancel(true));
        pending.clear();
    }

    @Override
    public void onError(Exception ex) {
        // 记录但不中断
    }

    /** 发送一条 CDP 命令并等待响应（带超时）。 */
    public JsonNode send(String method, Map<String, Object> params) throws Exception {
        long id = idGen.getAndIncrement();
        Map<String, Object> req = new HashMap<>();
        req.put("id", id);
        req.put("method", method);
        req.put("params", params == null ? new HashMap<String, Object>() : params);
        CompletableFuture<JsonNode> f = new CompletableFuture<>();
        pending.put(id, f);
        send(MAPPER.writeValueAsString(req));
        try {
            return f.get(20, TimeUnit.SECONDS);
        } catch (TimeoutException e) {
            pending.remove(id);
            throw new RuntimeException("CDP 命令超时: " + method);
        }
    }

    /** 启用 Page / Runtime / Network 域（连接后必须调用）。 */
    public void enableDomains() throws Exception {
        send("Page.enable", null);
        send("Runtime.enable", null);
        send("Network.enable", null);
    }

    /** 执行 JS 表达式，返回 {type,value} 结果节点（等价 Python eval_js 的原始返回）。 */
    public JsonNode eval(String expression) throws Exception {
        Map<String, Object> p = new HashMap<>();
        p.put("expression", expression);
        p.put("returnByValue", true);
        p.put("awaitPromise", true);
        JsonNode r = send("Runtime.evaluate", p);
        return r.path("result").path("result");
    }

    /** 返回 JS 表达式的实际值节点（已 returnByValue 求值）。 */
    public JsonNode evalValue(String expression) throws Exception {
        return eval(expression).path("value");
    }

    /** 执行 JS 表达式并返回字符串值（便捷方法）。 */
    public String evalString(String expression) throws Exception {
        JsonNode v = evalValue(expression);
        if (v.isNull() || v.isMissingNode()) {
            return "";
        }
        return v.asText();
    }

    /** 执行 JS 表达式并返回布尔值。 */
    public boolean evalBool(String expression) throws Exception {
        return evalValue(expression).asBoolean(false);
    }

    /** 执行 JS 表达式并返回整数值。 */
    public int evalInt(String expression) throws Exception {
        return evalValue(expression).asInt(0);
    }

    /** 执行 JS 表达式并返回对象值（Map），非对象时返回 null。 */
    public Map<String, Object> evalMap(String expression) throws Exception {
        JsonNode v = evalValue(expression);
        if (v.isObject()) {
            return MAPPER.convertValue(v, Map.class);
        }
        return null;
    }

    /** 导航到指定 URL。 */
    public void navigate(String url) throws Exception {
        Map<String, Object> p = new HashMap<>();
        p.put("url", url);
        send("Page.navigate", p);
    }

    /** 重新加载当前页面（预热模式提交前刷新到最新态）。 */
    public void reload() throws Exception {
        Map<String, Object> p = new HashMap<>();
        p.put("ignoreCache", true);
        send("Page.reload", p);
    }

    /** 获取 cookies。 */
    public JsonNode getCookies() throws Exception {
        return send("Network.getCookies", null);
    }

    /** 截图并写入文件，返回 base64 数据；path 为 null 时仅返回 base64。 */
    public String captureScreenshot(String path) throws Exception {
        Map<String, Object> p = new HashMap<>();
        p.put("format", "png");
        p.put("captureBeyondViewport", false);
        JsonNode r = send("Page.captureScreenshot", p);
        String data = r.path("result").path("data").asText();
        if (path != null && !data.isEmpty()) {
            byte[] bytes = Base64.getDecoder().decode(data);
            Files.write(Paths.get(path), bytes);
        }
        return data;
    }

    /** 取最近一次事件（供监听逻辑消费）。 */
    public JsonNode pollEvent() {
        return events.poll();
    }

    public boolean hasEvent() {
        return !events.isEmpty();
    }
}
