package com.example.jdseckill.service;

import com.example.jdseckill.cdp.ChromeManager;
import com.example.jdseckill.cdp.CdpClient;
import com.example.jdseckill.cdp.PageSession;
import com.fasterxml.jackson.databind.JsonNode;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

import javax.annotation.Resource;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * 结算页流程（等价 Python jd_cdp.checkout / retry_checkout / _parse_*）。
 * 后台线程拦截结算数据接口 JSON，优先用接口数据，DOM 解析兜底。
 */
@Service
public class CheckoutService {

    private static final Logger log = LoggerFactory.getLogger(CheckoutService.class);

    private static final String[] CHECKOUT_API_IDS = {
            "balance_getCurrentOrder_m", "balance_getCart_m", "getCart",
            "balance_getCurrentOrder", "balance_getCart"
    };

    private static final Pattern RISK_SIG = Pattern.compile("活动异常火爆");
    private static final Pattern ERROR_TEXT = Pattern.compile("Error_text");
    private static final Pattern ERROR_BTN = Pattern.compile("Error_btn");
    private static final Pattern PRICE_TOTAL = Pattern.compile("合计[：:]?[¥￥]?(\\d+\\.\\d{2})");
    private static final Pattern PRICE_ANY = Pattern.compile("[¥￥](\\d+\\.\\d{2})");
    private static final Pattern QTY = Pattern.compile("[×xX](\\d+)");
    private static final Pattern ADDR_DEFAULT = Pattern.compile("默认(.{4,40}?)(?=\\s*京东自营|\\s*京东|\\s*[A-Za-z])");
    private static final Pattern PRODUCT = Pattern.compile("京东自营(.{2,80}?)[¥￥]");
    private static final Pattern PRODUCT_FALLBACK = Pattern.compile(
            "([\\u4e00-\\u9fa5A-Za-z0-9]{6,60}?(?:ml|g|套装|沐浴露|洗发|洗衣液|猫粮|猫条|湿厕纸)[\\u4e00-\\u9fa5A-Za-z0-9]*?)");

    @Resource
    private ChromeManager chrome;

    public Map<String, Object> checkout(String sku, int qty, boolean keepOpen) {
        try {
            String payUrl = "https://trade.m.jd.com/pay?commlist=" + sku + ",," + qty + "," + sku + ",1,0,0";
            // 关掉旧的结算页 tab（可能卡死），避免复用失效连接
            for (Map<String, Object> p : chrome.listPages()) {
                if ("page".equals(p.get("type"))
                        && String.valueOf(p.get("url")).contains("trade.m.jd.com/pay")
                        && !String.valueOf(p.get("url")).contains("mpay")) {
                    chrome.closeTarget(String.valueOf(p.get("id")));
                }
            }
            PageSession ps = chrome.openPage(payUrl, true);
            CdpClient pg = ps.client;
            // 独立连接监听 Network 事件，拦截结算数据接口 JSON 响应
            String lws = chrome.pageWsByTarget(ps.targetId, 12);
            CdpClient listen = null;
            if (lws != null) {
                listen = new CdpClient(java.net.URI.create(lws));
                listen.connectBlocking(10, java.util.concurrent.TimeUnit.SECONDS);
                listen.send("Network.enable", null);
            }
            ApiBody apiBody = new ApiBody();
            AtomicBoolean stop = new AtomicBoolean(false);
            final CdpClient listenFinal = listen;
            Thread listener = new Thread(() -> listenLoop(listenFinal, apiBody, stop), "checkout-listener");
            listener.setDaemon(true);
            listener.start();

            String html = "", txt = "", title = "";
            boolean ready = false;
            long deadline = System.currentTimeMillis() + 25000;
            Thread.sleep(400);
            while (System.currentTimeMillis() < deadline) {
                if (apiBody.data != null) {
                    ready = true;
                    break;
                }
                try {
                    title = pg.evalString("document.title");
                    html = pg.evalString("document.documentElement && document.documentElement.outerHTML");
                    txt = pg.evalString("document.body && document.body.innerText");
                } catch (Exception e) {
                    Thread.sleep(250);
                    continue;
                }
                if (isRiskControl(html, txt)) {
                    break;
                }
                if (html != null && html.length() > 3000) {
                    String hsp = html.replaceAll("\\s+", "");
                    if ((html.contains("京东自营") || html.contains("合计")) && (hsp.contains("¥") || hsp.contains("￥"))) {
                        ready = true;
                        break;
                    }
                }
                Thread.sleep(250);
            }
            stop.set(true);
            Thread.sleep(150);

            Map<String, Object> info;
            if (apiBody.data != null) {
                try {
                    JsonNode parsed = new com.fasterxml.jackson.databind.ObjectMapper().readTree(apiBody.data);
                    info = parseCheckoutApi(parsed, sku, qty);
                    info.put("source", "api");
                    info.put("api_func", apiBody.fid);
                } catch (Exception e) {
                    info = null;
                }
            } else {
                info = null;
            }
            if (info == null || info.get("product_name") == null) {
                info = parseCheckoutHtml(html, txt, sku, qty);
                info.put("source", "dom");
            }
            info.put("title", title);
            info.put("url", pg.evalString("location.href"));
            info.put("ok", true);
            info.put("ready", ready);
            info.put("html_len", html == null ? 0 : html.length());
            info.put("text_len", txt == null ? 0 : txt.length());
            if (isRiskControl(html, txt)) {
                info.put("risk_control", true);
                info.put("product_name", "（账号被风控拦截）");
                info.put("price", null);
                info.put("qty_found", null);
                info.put("address_hint", "活动异常火爆，已优先接入快速通道，请返回上一页重新尝试");
            } else {
                info.put("risk_control", false);
            }
            try {
                if (listen != null) {
                    listen.close();
                }
            } catch (Exception ignored) {
            }
            if (keepOpen) {
                Map<String, Object> r = new LinkedHashMap<>();
                r.put("ok", true);
                r.put("target_id", ps.targetId);
                r.put("url", payUrl);
                r.put("ready", ready);
                r.put("risk_control", isRiskControl(html, txt));
                return r;
            }
            try {
                pg.close();
            } catch (Exception ignored) {
            }
            return info;
        } catch (Exception e) {
            log.warn("checkout 异常: {}", e.getMessage());
            Map<String, Object> r = new LinkedHashMap<>();
            r.put("ok", false);
            r.put("error", e.getMessage());
            return r;
        }
    }

    public Map<String, Object> retryCheckout(String sku, int qty) {
        try {
            for (Map<String, Object> p : chrome.listPages()) {
                if ("page".equals(p.get("type"))
                        && String.valueOf(p.get("url")).contains("trade.m.jd.com/pay")
                        && !String.valueOf(p.get("url")).contains("mpay")) {
                    chrome.closeTarget(String.valueOf(p.get("id")));
                }
            }
            Thread.sleep(2000);
        } catch (Exception ignored) {
        }
        return checkout(sku, qty, false);
    }

    private void listenLoop(CdpClient listen, ApiBody apiBody, AtomicBoolean stop) {
        if (listen == null) {
            return;
        }
        try {
            while (!stop.get()) {
                JsonNode m;
                try {
                    m = listen.pollEvent();
                } catch (Exception e) {
                    m = null;
                }
                if (m == null) {
                    Thread.sleep(100);
                    continue;
                }
                String meth = m.path("method").asText();
                if ("Network.requestWillBeSent".equals(meth)) {
                    String u = m.path("params").path("request").path("url").asText();
                    Matcher fm = Pattern.compile("functionId=([^&]+)").matcher(u);
                    if (fm.find() && contains(CHECKOUT_API_IDS, fm.group(1))) {
                        String rid = m.path("params").path("requestId").asText();
                        synchronized (apiBody) {
                            apiBody.fid = fm.group(1);
                            apiBody.url = u;
                            apiBody.reqIds.add(rid);
                        }
                    }
                } else if ("Network.loadingFinished".equals(meth)) {
                    String rid = m.path("params").path("requestId").asText();
                    boolean match;
                    synchronized (apiBody) {
                        match = apiBody.reqIds.contains(rid);
                    }
                    if (match) {
                        try {
                            JsonNode rb = listen.send("Network.getResponseBody",
                                    new HashMap<String, Object>() {{
                                        put("requestId", rid);
                                    }});
                            String b = rb.path("result").path("body").asText();
                            if (b != null && !b.isEmpty()) {
                                synchronized (apiBody) {
                                    apiBody.data = b;
                                }
                            }
                        } catch (Exception ignored) {
                        }
                    }
                }
            }
        } catch (Exception ignored) {
        }
    }

    private static boolean contains(String[] arr, String v) {
        for (String a : arr) {
            if (a.equals(v)) {
                return true;
            }
        }
        return false;
    }

    private static boolean isRiskControl(String html, String txt) {
        if (RISK_SIG.matcher(html == null ? "" : html).find()) {
            return true;
        }
        if (RISK_SIG.matcher(txt == null ? "" : txt).find()) {
            return true;
        }
        if (ERROR_TEXT.matcher(html == null ? "" : html).find()
                && ERROR_BTN.matcher(html == null ? "" : html).find()) {
            return true;
        }
        return false;
    }

    private Map<String, Object> parseCheckoutHtml(String html, String txt, String sku, int qty) {
        String src = (txt == null || txt.trim().isEmpty()) ? htmlToText(html) : txt.trim();
        String srcSpaced = src.replaceAll("\\s+", "");
        Map<String, Object> info = new LinkedHashMap<>();
        info.put("sku", sku);
        info.put("qty_input", qty);
        String price = null;
        Matcher m = PRICE_TOTAL.matcher(srcSpaced);
        if (m.find()) {
            price = m.group(1);
        } else {
            m = PRICE_ANY.matcher(srcSpaced);
            if (m.find()) {
                price = m.group(1);
            }
        }
        info.put("price", price);
        Integer q = null;
        m = QTY.matcher(srcSpaced);
        if (m.find()) {
            q = Integer.parseInt(m.group(1));
        }
        info.put("qty_found", q);
        String addr = null;
        m = ADDR_DEFAULT.matcher(src);
        if (m.find()) {
            addr = "默认" + m.group(1).trim();
        }
        if (addr == null) {
            for (String kw : new String[]{"路", "大厦", "小区", "栋", "号楼"}) {
                int i = src.indexOf(kw);
                if (i >= 0) {
                    String seg = src.substring(Math.max(0, i - 30), Math.min(src.length(), i + 20));
                    seg = seg.replaceAll("\\s+", " ").trim();
                    if (!seg.isEmpty()) {
                        addr = seg;
                        break;
                    }
                }
            }
        }
        info.put("address_hint", addr);
        String product = null;
        m = PRODUCT.matcher(src);
        if (m.find()) {
            product = m.group(1).trim();
        }
        if (product == null) {
            m = PRODUCT_FALLBACK.matcher(src);
            if (m.find()) {
                product = m.group(1).trim();
            }
        }
        info.put("product_name", product);
        info.put("text_snippet", src.length() > 1500 ? src.substring(0, 1500) : src);
        return info;
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> parseCheckoutApi(JsonNode data, String sku, int qty) {
        Map<String, Object> info = new LinkedHashMap<>();
        info.put("sku", sku);
        info.put("qty_input", qty);
        info.put("price", null);
        info.put("qty_found", null);
        info.put("product_name", null);
        info.put("address_hint", null);
        try {
            info.put("text_snippet", data.toString().length() > 1500
                    ? data.toString().substring(0, 1500) : data.toString());
            JsonNode code = data.get("code") != null ? data.get("code") : data.get("errcode");
            if (code != null && !code.isNull() && code.asInt() != 0 && data.get("data") == null) {
                info.put("api_code", code.asInt());
                return info;
            }
            JsonNode d = data.get("data") != null ? data.get("data") : data;
            String product = null;
            for (String k : new String[]{"skuName", "name", "wareName", "title", "goodsName"}) {
                if (d.get(k) != null && !d.get(k).isNull() && !d.get(k).asText().isEmpty()) {
                    product = d.get(k).asText();
                    break;
                }
            }
            JsonNode items = d.get("items") != null ? d.get("items")
                    : (d.get("cartList") != null ? d.get("cartList")
                    : (d.get("itemList") != null ? d.get("itemList")
                    : (d.get("itemsList") != null ? d.get("itemsList") : null)));
            if (items != null && items.isObject()) {
                items = items.get("items") != null ? items.get("items")
                        : (items.get("list") != null ? items.get("list") : null);
            }
            if (product == null && items != null && items.isArray() && items.size() > 0) {
                JsonNode it = items.get(0);
                if (it.isObject()) {
                    for (String k : new String[]{"skuName", "name", "wareName", "goodsName", "title"}) {
                        if (it.get(k) != null && !it.get(k).isNull() && !it.get(k).asText().isEmpty()) {
                            product = it.get(k).asText();
                            break;
                        }
                    }
                }
            }
            info.put("product_name", product);
            if (items != null && items.isArray() && items.size() > 0) {
                JsonNode it = items.get(0);
                if (it.isObject()) {
                    JsonNode num = it.get("num") != null ? it.get("num") : it.get("quantity");
                    if (num != null && !num.isNull()) {
                        info.put("qty_found", num.asInt());
                    }
                }
            }
            JsonNode price = d.get("orderPrice") != null ? d.get("orderPrice")
                    : (d.get("payPrice") != null ? d.get("payPrice")
                    : (d.get("totalPrice") != null ? d.get("totalPrice")
                    : (d.get("price") != null ? d.get("price") : null)));
            if (price == null && items != null && items.isArray() && items.size() > 0) {
                JsonNode it = items.get(0);
                if (it.isObject() && it.get("price") != null) {
                    price = it.get("price");
                }
            }
            if (price != null && !price.isNull()) {
                info.put("price", String.valueOf(price.asText()));
            }
            JsonNode addr = d.get("address") != null ? d.get("address")
                    : (d.get("consigneeAddr") != null ? d.get("consigneeAddr")
                    : (d.get("addr") != null ? d.get("addr") : null));
            if (addr != null && addr.isObject()) {
                StringBuilder sb = new StringBuilder();
                for (String k : new String[]{"name", "mobile", "addr", "address", "detail", "title"}) {
                    if (addr.get(k) != null) {
                        sb.append(addr.get(k).asText()).append(" ");
                    }
                }
                info.put("address_hint", sb.toString().trim());
            } else if (addr != null) {
                info.put("address_hint", addr.asText());
            }
        } catch (Exception e) {
            info.put("api_error", e.getMessage());
        }
        return info;
    }

    private static String htmlToText(String h) {
        if (h == null) {
            return "";
        }
        h = h.replaceAll("(?s)<style[^>]*>.*?</style>", " ");
        h = h.replaceAll("(?s)<script[^>]*>.*?</script>", " ");
        h = h.replaceAll("<[^>]+>", " ");
        h = h.replaceAll("\\s+", " ").trim();
        return h;
    }

    /** 拦截到的结算数据接口响应体。 */
    private static class ApiBody {
        String data = null;
        String fid = null;
        String url = null;
        final Set<String> reqIds = ConcurrentHashMap.newKeySet();
    }
}
