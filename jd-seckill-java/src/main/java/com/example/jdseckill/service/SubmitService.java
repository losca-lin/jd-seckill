package com.example.jdseckill.service;

import com.example.jdseckill.cdp.ChromeManager;
import com.example.jdseckill.cdp.CdpClient;
import com.fasterxml.jackson.databind.JsonNode;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

import javax.annotation.Resource;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * 提交订单（等价 Python jd_cdp.submit_order）。
 * 只认「点击提交后新出现的」收银台 mpay 页面，避免把浏览器里残留的旧收银台误判为本单成功。
 */
@Service
public class SubmitService {

    private static final Logger log = LoggerFactory.getLogger(SubmitService.class);

    private static final Pattern ORDER_ID_URL = Pattern.compile("orderId=([0-9]+)");

    @Resource
    private ChromeManager chrome;
    @Resource
    private CheckoutService checkoutService;

    public Map<String, Object> submitOrder(String sku, int qty, boolean ensureCheckout, String targetId) {
        log.info("[submit] 开始 sku={} qty={} ensureCheckout={} targetId={}", sku, qty, ensureCheckout, targetId);
        try {
            if (ensureCheckout) {
                log.info("[submit] 需要重新打开结算页...");
                Map<String, Object> chk = checkoutService.checkout(sku, qty, false);
                if (!(Boolean) chk.getOrDefault("ok", false)) {
                    String err = "打开结算页失败: " + chk.get("error");
                    log.warn("[submit] {}", err);
                    return map("ok", false, "error", err);
                }
                log.info("[submit] 结算页已重新打开");
            }
            Map<String, Object> page;
            if (targetId != null && !targetId.isEmpty()) {
                page = findPageById(targetId);
            } else {
                page = chrome.findPage("trade.m.jd.com/pay");
            }
            if (page == null) {
                String err = "未找到结算页，请先执行 checkout";
                log.warn("[submit] {}", err);
                return map("ok", false, "error", err);
            }
            log.info("[submit] 命中结算页 id={} url={}", page.get("id"), page.get("url"));
            CdpClient pg = new CdpClient(java.net.URI.create(String.valueOf(page.get("webSocketDebuggerUrl"))));
            pg.connectBlocking(10, java.util.concurrent.TimeUnit.SECONDS);
            pg.enableDomains();
            if (targetId != null && !targetId.isEmpty()) {
                try {
                    log.info("[submit] 预热页 reload 刷新到最新态...");
                    pg.reload();
                    Thread.sleep(1500);
                } catch (Exception ignored) {
                }
            }
            // 记录点击前已存在的收银台 URL（baseline）
            java.util.Set<String> baseline = new java.util.HashSet<>();
            for (Map<String, Object> p : chrome.listPages()) {
                if (String.valueOf(p.get("url")).contains("mpay.m.jd.com")) {
                    baseline.add(String.valueOf(p.get("url")));
                }
            }
            log.info("[submit] baseline 已有收银台数量={}", baseline.size());
            Map<String, Object> btn = pg.evalMap("(() => {\n"
                    + "  let e=document.querySelector('taro-button-core[class*=\"ActionBar_submit\"]')\n"
                    + "    || document.querySelector('[class*=\"ActionBar_submit\"]');\n"
                    + "  if(!e){ const all=[...document.querySelectorAll('*')];\n"
                    + "    e=all.find(el=>{const c=(el.className||'').toString();const t=(el.textContent||'').trim();\n"
                    + "      return c.includes('ActionBar_buttons')&&t.includes('在线支付');});\n"
                    + "    if(!e) e=all.find(el=>{const t=(el.tagName||'');const txt=(el.textContent||'').trim();\n"
                    + "      return (t==='TARO-BUTTON-CORE'||t==='BUTTON'||t==='A')&&txt.includes('在线支付')&&el.offsetParent!==null;});\n"
                    + "  }\n"
                    + "  if(!e) return {ok:false};\n"
                    + "  e.scrollIntoView({block:'center'});\n"
                    + "  const r=e.getBoundingClientRect();\n"
                    + "  return {ok:true, x:r.left+r.width/2, y:r.top+r.height/2, w:r.width, h:r.height,\n"
                    + "    tag:e.tagName, cls:(e.className||'').toString().slice(0,60), txt:(e.textContent||'').trim().slice(0,30)};\n"
                    + "})()");
            if (btn == null || !(Boolean) btn.getOrDefault("ok", false)) {
                try {
                    pg.close();
                } catch (Exception ignored) {
                }
                String err = "未找到 ActionBar_submit 提交按钮（结算页可能未加载完 / 无货态 / 需勾选协议）";
                log.warn("[submit] {}", err);
                return map("ok", false, "error", err);
            }
            log.info("[submit] 找到提交按钮 tag={} cls={} txt=[{}] 坐标=({},{}))",
                    btn.get("tag"), btn.get("cls"), btn.get("txt"), btn.get("x"), btn.get("y"));
            int x = ((Number) btn.get("x")).intValue();
            int y = ((Number) btn.get("y")).intValue();
            log.info("[submit] 真实鼠标点击坐标 ({},{})", x, y);
            dispatchMouse(pg, x, y);

            boolean jumped = false;
            for (int i = 0; i < 8; i++) {
                Thread.sleep(150);
                Map<String, Object> mp = chrome.findPage("mpay.m.jd.com");
                if (mp != null && !baseline.contains(String.valueOf(mp.get("url")))) {
                    jumped = true;
                    break;
                }
            }
            if (!jumped) {
                log.warn("[submit] 鼠标点击后 1.2s 内未出现新收银台，改用 DOM .click() 兜底");
                // DOM .click() 兜底
                try {
                    pg.eval("(() => {\n"
                            + "  let e=document.querySelector('taro-button-core[class*=\"ActionBar_submit\"]')\n"
                            + "    || document.querySelector('[class*=\"ActionBar_submit\"]');\n"
                            + "  if(!e) return;\n"
                            + "  const real=e.querySelector('button, [class*=\"btn\"], a')||e;\n"
                            + "  try{ real.click(); }catch(_){ e.click(); }\n"
                            + "})()");
                } catch (Exception ignored) {
                }
            } else {
                log.info("[submit] 鼠标点击后已跳转到新收银台");
            }
            String orderId = null;
            String payUrl = "";
            for (int i = 0; i < 80; i++) {
                Thread.sleep(250);
                Map<String, Object> mp = chrome.findPage("mpay.m.jd.com");
                if (mp != null && !baseline.contains(String.valueOf(mp.get("url")))) {
                    payUrl = String.valueOf(mp.get("url"));
                    Matcher mm = ORDER_ID_URL.matcher(payUrl);
                    if (mm.find()) {
                        orderId = mm.group(1);
                    }
                    break;
                }
                String url = "";
                try {
                    url = pg.evalString("location.href");
                } catch (Exception ignored) {
                }
                if (url.contains("mpay.m.jd.com") && !baseline.contains(url)) {
                    payUrl = url;
                    Matcher mm = ORDER_ID_URL.matcher(url);
                    if (mm.find()) {
                        orderId = mm.group(1);
                    }
                    break;
                }
                if (!url.contains("trade.m.jd.com/pay")) {
                    try {
                        String t = pg.evalString("document.body.innerText");
                        Matcher mm = Pattern.compile("orderId[=:]\\s*([0-9]+)").matcher(t == null ? "" : t);
                        if (mm.find()) {
                            orderId = mm.group(1);
                            payUrl = url;
                            break;
                        }
                    } catch (Exception ignored) {
                    }
                }
                if (i % 10 == 0) {
                    log.info("[submit] 等待收银台/订单号... 第{}次 url={}", i, url);
                }
            }
            if (orderId == null) {
                try {
                    Map<String, Object> mp = chrome.findPage("mpay.m.jd.com");
                    if (mp != null && !baseline.contains(String.valueOf(mp.get("url")))) {
                        payUrl = String.valueOf(mp.get("url"));
                    }
                } catch (Exception ignored) {
                }
            }
            try {
                // 失败时额外抓一次结算页实际文案，便于排查“无货/风控/需勾选协议”
                if (orderId == null) {
                    String bodyText = pg.evalString("document.body.innerText");
                    log.warn("[submit] 20s 内未生成订单。结算页文案前 400 字: {}",
                            bodyText == null ? "(取不到)" : bodyText.replaceAll("\\s+", " ").substring(0, Math.min(400, bodyText.length())));
                }
            } catch (Exception ignored) {
            }
            try {
                pg.close();
            } catch (Exception ignored) {
            }
            if (orderId != null) {
                log.info("[submit] 成功！order_id={} payment_url={}", orderId, payUrl);
                return map("ok", true, "order_id", orderId, "payment_url", payUrl);
            }
            String err = "点击提交后未生成新订单（未跳转到收银台）。可能原因：秒杀已无库存 / 京东风控拦截 / 需先勾选购买协议。请查看浏览器结算页实际状态";
            log.warn("[submit] {} | payment_url={}", err, payUrl);
            return map("ok", false,
                    "error", err,
                    "payment_url", payUrl);
        } catch (Exception e) {
            log.error("[submit] submit_order 异常", e);
            return map("ok", false, "error", e.getMessage());
        }
    }

    private void dispatchMouse(CdpClient pg, int x, int y) {
        try {
            pg.send("Input.dispatchMouseEvent", kv("type", "mouseMoved", "x", x, "y", y, "button", "left"));
            pg.send("Input.dispatchMouseEvent", kv("type", "mousePressed", "x", x, "y", y, "button", "left", "clickCount", 1));
            pg.send("Input.dispatchMouseEvent", kv("type", "mouseReleased", "x", x, "y", y, "button", "left", "clickCount", 1));
        } catch (Exception ignored) {
        }
    }

    private Map<String, Object> findPageById(String targetId) throws Exception {
        for (Map<String, Object> p : chrome.listPages()) {
            if (targetId.equals(p.get("id"))) {
                return p;
            }
        }
        return null;
    }

    private static Map<String, Object> map(Object... kv) {
        Map<String, Object> m = new LinkedHashMap<>();
        for (int i = 0; i < kv.length; i += 2) {
            m.put(String.valueOf(kv[i]), kv[i + 1]);
        }
        return m;
    }

    private static Map<String, Object> kv(Object... kv) {
        Map<String, Object> m = new LinkedHashMap<>();
        for (int i = 0; i < kv.length; i += 2) {
            m.put(String.valueOf(kv[i]), kv[i + 1]);
        }
        return m;
    }
}
