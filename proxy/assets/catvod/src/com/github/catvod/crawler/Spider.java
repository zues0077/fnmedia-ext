package com.github.catvod.crawler;
import android.content.Context;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * catvod Spider 抽象基类桩（供 spider jar 在 bridge 内继承）。
 *
 * 背景：bridge 用 DexClassLoader 加载第三方 spider jar 时，父加载器里若没有
 * com.github.catvod.crawler.Spider，就会去 jar 内找；但加固源（DexGuard）的 jar
 * 里基类可能被遮蔽/版本不一致，导致 NoSuchMethodError。因此 bridge 内必须提供一个
 * 签名与官方 catvod-api 契约一致的基类，作为统一的父类。
 *
 * 关键约束：
 * 1. 方法签名必须与官方基类对齐（返回类型 / static / 参数），否则 jar 内 spider
 *    调用时报 NoSuchMethodError（此前缺 safeDns()/action()/manualVideoCheck():boolean
 *    等导致 spider 源系统性不可用）。
 * 2. 实现用最简等价物，不引入 catvod 官方整套 net/utils 栈（Net/OkDns/Proxy/Runtime
 *    等依赖 androidx/guava，build 脚本只打包 gson/okhttp/okio/orgjson/kotlin）。
 *    safeDns() 在 NAS 环境无需 DoH 防污染，直接返回 okhttp3.Dns.SYSTEM。
 * 3. 保留旧的 proxyLocal()（无参）以兼容仍用旧签名的 jar；同时提供官方新签名。
 */
public abstract class Spider {

    /* ---------------- 生命周期 ---------------- */
    public void init(Context context) {}
    public void init(Context context, String extend) {}
    public void destroy() {}

    /* ---------------- 网络 / DNS（静态，jar 内 spider 直接调用） ---------------- */
    /** 官方：static Dns safeDns() => OkHttp.dns()。NAS 无需 DoH，返回系统 DNS。 */
    public static okhttp3.Dns safeDns() { return okhttp3.Dns.SYSTEM; }

    /** 官方旧版：static OkHttpClient client()。返回一个宽松默认客户端。 */
    public static okhttp3.OkHttpClient client() {
        return new okhttp3.OkHttpClient.Builder()
                .followRedirects(true)
                .followSslRedirects(true)
                .build();
    }

    /* ---------------- 内容接口（与官方签名一致） ---------------- */
    public String homeContent(boolean filter) { return "{\"class\":[]}"; }
    public String homeVideoContent() { return "{\"list\":[]}"; }
    public String categoryContent(String tid, String pg, boolean filter, HashMap<String,String> extend) { return "{\"list\":[]}"; }
    public String detailContent(List<String> ids) { return "{\"list\":[]}"; }
    public String searchContent(String key, boolean quick) { return "{\"list\":[]}"; }
    public String searchContent(String key, boolean quick, String pg) { return searchContent(key, quick); }
    public String playerContent(String flag, String id, List<String> vipFlags) { return "{}"; }
    public String liveContent(String url) { return ""; }

    /* ---------------- 视频格式 / 手动校验（官方签名） ---------------- */
    public boolean isVideoFormat(String url) { return false; }
    public boolean isVideoFormat(String url, java.util.HashMap<String,String> headers) { return isVideoFormat(url); }
    /** 官方：boolean manualVideoCheck()。旧桩是 void，签名错，需修正。 */
    public boolean manualVideoCheck() { return false; }

    /* ---------------- 本地代理（官方：Object[] proxy(Map)；旧桩：proxyLocal() 无参） ---------------- */
    /** 官方新签名：Object[] proxy(Map<String,String> params)。 */
    public Object[] proxy(Map<String,String> params) {
        return new Object[]{404, "text/plain", new java.io.ByteArrayInputStream(new byte[0])};
    }
    /** 旧签名兼容（保留，避免旧 jar 调用 NoSuchMethodError）。 */
    public Object proxyLocal() { return null; }
    public Object[] proxyLocal(Map<String,String> params) { return proxy(params); }

    /* ---------------- 自定义动作 / 弹幕 / 代理 URL ---------------- */
    /** 官方：String action(String action)。收藏/登录等 action 卡片走这里。 */
    public String action(String action) { return "{}"; }

    /** 官方：danmakuUrl 相关（无副作用占位）。 */
    public String danmakuUrl(String id) { return ""; }
    public static String danmakuUrl(String siteKey, String id) { return ""; }

    /** 官方：proxyUrl 相关（无副作用占位）。 */
    public String proxyUrl() { return ""; }
    public String proxyUrl(Map<String,String> params) { return ""; }
    public String proxyUrl(Map<String,String> params, boolean local) { return ""; }
}
