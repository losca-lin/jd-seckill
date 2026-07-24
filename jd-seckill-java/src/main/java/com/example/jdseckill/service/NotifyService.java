package com.example.jdseckill.service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

import java.io.File;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.nio.file.Paths;
import java.util.HashMap;
import java.util.Map;
import java.util.Scanner;

/**
 * Server 酱微信推送（等价 Python notify + _notify_success）。
 * 复用 Python 版已配置的 jd_notify.json（位于上级 jd-seckill 目录）。
 */
@Service
public class NotifyService {

    private static final Logger log = LoggerFactory.getLogger(NotifyService.class);
    private final ObjectMapper mapper = new ObjectMapper();
    private String serverChanKey = "";

    public NotifyService() {
        load();
    }

    private String cfgPath() {
        String userDir = System.getProperty("user.dir");
        return Paths.get(userDir).getParent().resolve("jd_notify.json").toString();
    }

    private void load() {
        try {
            File f = new File(cfgPath());
            if (f.exists()) {
                JsonNode n = mapper.readTree(f);
                serverChanKey = n.path("serverchan_key").asText("");
            }
        } catch (Exception e) {
            log.warn("读取 jd_notify.json 失败: {}", e.getMessage());
        }
    }

    public synchronized void saveKey(String key) {
        serverChanKey = key == null ? "" : key.trim();
        try {
            ObjectNode o = mapper.createObjectNode();
            o.put("serverchan_key", serverChanKey);
            mapper.writerWithDefaultPrettyPrinter().writeValue(new File(cfgPath()), o);
        } catch (Exception e) {
            log.warn("写入 jd_notify.json 失败: {}", e.getMessage());
        }
    }

    public Map<String, Object> status() {
        Map<String, Object> m = new HashMap<>();
        m.put("ok", true);
        m.put("configured", !serverChanKey.isEmpty());
        if (serverChanKey.length() > 8) {
            m.put("serverchan_key", serverChanKey.substring(0, 4) + "****" + serverChanKey.substring(serverChanKey.length() - 4));
        } else if (!serverChanKey.isEmpty()) {
            m.put("serverchan_key", serverChanKey.substring(0, 1) + "****");
        } else {
            m.put("serverchan_key", "");
        }
        return m;
    }

    public Map<String, Object> notify(String title, String desp) {
        if (serverChanKey.isEmpty()) {
            Map<String, Object> m = new HashMap<>();
            m.put("ok", false);
            m.put("error", "未配置 serverchan_key");
            return m;
        }
        try {
            String url = "https://sctapi.ftqq.com/" + serverChanKey + ".send";
            String body = "title=" + URLEncoder.encode(title, "UTF-8")
                    + "&desp=" + URLEncoder.encode(desp, "UTF-8");
            HttpURLConnection conn = (HttpURLConnection) new URL(url).openConnection();
            conn.setRequestMethod("POST");
            conn.setDoOutput(true);
            conn.setRequestProperty("Content-Type", "application/x-www-form-urlencoded");
            conn.setConnectTimeout(10000);
            conn.setReadTimeout(10000);
            try (OutputStream os = conn.getOutputStream()) {
                os.write(body.getBytes(StandardCharsets.UTF_8));
            }
            String resp;
            try (Scanner sc = new Scanner(conn.getInputStream(), "UTF-8")) {
                resp = sc.useDelimiter("\\A").next();
            }
            boolean ok = resp.contains("\"code\":0") || resp.contains("\"errno\":0") || resp.contains("SUCCESS");
            Map<String, Object> m = new HashMap<>();
            m.put("ok", ok);
            m.put("raw", resp.length() > 200 ? resp.substring(0, 200) : resp);
            return m;
        } catch (Exception e) {
            log.warn("Server 酱推送异常: {}", e.getMessage());
            Map<String, Object> m = new HashMap<>();
            m.put("ok", false);
            m.put("error", e.getMessage());
            return m;
        }
    }

    public void notifySuccess(Map<String, Object> task, Map<String, Object> res) {
        String orderId = res.get("order_id") == null ? "" : String.valueOf(res.get("order_id"));
        String sku = (String) task.get("sku");
        Object qtyObj = task.get("qty");
        int qty = qtyObj instanceof Number ? ((Number) qtyObj).intValue() : 1;
        String atStr = (String) task.get("at_str");
        String desp = "🎉 抢到了！\n商品 SKU：" + sku + "\n数量：" + qty + "\n订单号：" + orderId
                + "\n定时：" + atStr + "\n请到京东 App「待支付」30 分钟内付款，否则订单自动取消。";
        notify("🎉 京东抢购成功", desp);
    }
}
