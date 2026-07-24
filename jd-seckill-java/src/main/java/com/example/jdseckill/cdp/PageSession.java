package com.example.jdseckill.cdp;

/** 一个已打开的页面：targetId + 其 CDP 连接（等价 Python (tid, CDPPage)）。 */
public class PageSession {
    public final String targetId;
    public final CdpClient client;

    public PageSession(String targetId, CdpClient client) {
        this.targetId = targetId;
        this.client = client;
    }
}
