package com.openyoureyes.app;

import android.content.Context;
import android.content.SharedPreferences;
import android.net.ConnectivityManager;
import android.net.LinkAddress;
import android.net.LinkProperties;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.os.Build;
import android.os.SystemClock;
import android.util.Base64;

import org.json.JSONObject;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.Inet4Address;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.NetworkInterface;
import java.net.Proxy;
import java.net.Socket;
import java.net.URL;
import java.net.URLConnection;
import java.security.KeyStore;
import java.security.MessageDigest;
import java.security.SecureRandom;
import java.security.cert.CertificateFactory;
import java.security.cert.X509Certificate;
import java.util.Collections;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;

import javax.net.ssl.HttpsURLConnection;
import javax.net.ssl.SSLContext;
import javax.net.ssl.SSLSocketFactory;
import javax.net.ssl.TrustManagerFactory;

/**
 * 和电脑上的 Open Your Eyes 通信。
 *
 * 安全：配对时（从电脑设置页扫码进来的链接）拿到电脑根证书的 SHA-256 指纹，下载根证书核对一致后存下来；
 * 之后所有请求都走 HTTPS，并且只信任这一张根证书——不需要在手机系统里安装任何证书。
 * 每个请求带配对密钥（X-T9-Key），电脑据此认出这是用户自己扫码加入的手机。
 *
 * 网络：连电脑一律不走手机上的代理；手机开着 VPN 时，优先直接走 Wi-Fi（绕开 VPN），走不通再用系统默认的网络。
 */
final class Net {
    static final String PREFS = "t9";
    private static SSLSocketFactory sslFactory;
    private static String sslFor;

    static SharedPreferences prefs(Context c) {
        return c.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    static boolean paired(Context c) {
        SharedPreferences p = prefs(c);
        return p.getString("host", null) != null && p.getString("ca", null) != null;
    }

    static String phoneId(Context c) {
        SharedPreferences p = prefs(c);
        String id = p.getString("id", null);
        if (id == null) {
            String abc = "abcdefghijkmnpqrstuvwxyz23456789";
            SecureRandom r = new SecureRandom();
            StringBuilder b = new StringBuilder("a");
            for (int i = 0; i < 11; i++) b.append(abc.charAt(r.nextInt(abc.length())));
            id = b.toString();
            p.edit().putString("id", id).apply();
        }
        return id;
    }

    static String userAgent() {
        return "OpenYourEyesApp/" + BuildInfo.VERSION + " (Linux; Android " + Build.VERSION.RELEASE + "; " + Build.MODEL + ")";
    }

    static String base(Context c) {
        SharedPreferences p = prefs(c);
        return "https://" + p.getString("host", "") + ":" + p.getInt("port", 39758);
    }

    static X509Certificate parseCert(byte[] data) throws Exception {
        return (X509Certificate) CertificateFactory.getInstance("X.509").generateCertificate(new ByteArrayInputStream(data));
    }

    static String sha256Hex(byte[] data) throws Exception {
        byte[] d = MessageDigest.getInstance("SHA-256").digest(data);
        StringBuilder b = new StringBuilder();
        for (byte x : d) b.append(String.format("%02x", x));
        return b.toString();
    }

    private static synchronized SSLSocketFactory ssl(Context c) throws Exception {
        String ca = prefs(c).getString("ca", "");
        if (sslFactory != null && ca.equals(sslFor)) return sslFactory;
        X509Certificate cert = parseCert(Base64.decode(ca, Base64.DEFAULT));
        KeyStore ks = KeyStore.getInstance(KeyStore.getDefaultType());
        ks.load(null, null);
        ks.setCertificateEntry("t9", cert);
        TrustManagerFactory tmf = TrustManagerFactory.getInstance(TrustManagerFactory.getDefaultAlgorithm());
        tmf.init(ks);
        SSLContext ctx = SSLContext.getInstance("TLS");
        ctx.init(null, tmf.getTrustManagers(), null);
        sslFactory = ctx.getSocketFactory();
        sslFor = ca;
        return sslFactory;
    }

    // ---------------------------------------------------------------- 走哪条网络

    private static Network routeNet;
    private static String routeFor;
    private static long routeAt;

    private static ConnectivityManager cm(Context c) {
        return (ConnectivityManager) c.getApplicationContext().getSystemService(Context.CONNECTIVITY_SERVICE);
    }

    /** 手机的 Wi-Fi（或有线）网络，不含 VPN；没有返回 null。 */
    static Network lanNetwork(Context c) {
        try {
            ConnectivityManager m = cm(c);
            for (Network n : m.getAllNetworks()) {
                NetworkCapabilities nc = m.getNetworkCapabilities(n);
                if (nc == null || nc.hasTransport(NetworkCapabilities.TRANSPORT_VPN)) continue;
                if (nc.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) || nc.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET)) return n;
            }
        } catch (Exception ignored) {
        }
        return null;
    }

    /** 手机现在开着 VPN（包括代理软件的 VPN 模式）吗。 */
    static boolean vpnOn(Context c) {
        try {
            ConnectivityManager m = cm(c);
            for (Network n : m.getAllNetworks()) {
                NetworkCapabilities nc = m.getNetworkCapabilities(n);
                if (nc != null && nc.hasTransport(NetworkCapabilities.TRANSPORT_VPN)) return true;
            }
        } catch (Exception ignored) {
        }
        return false;
    }

    /** 连不上电脑时给用户看的话：开着 VPN 就直说。 */
    static String cantReach(Context c, String usual) {
        if (vpnOn(c)) return "连不上电脑：手机开着 VPN / 代理，挡住了局域网。请先关掉它，或在它的设置里打开「绕过局域网」，再试。";
        return usual;
    }

    /** 连电脑用哪条网络：Wi-Fi 能直接连上电脑就用 Wi-Fi，否则用系统默认（null）。结果记 60 秒。 */
    private static synchronized Network route(Context c, String host, int port) {
        String key = host + ":" + port;
        long now = SystemClock.elapsedRealtime();
        if (key.equals(routeFor) && now - routeAt < 60000) return routeNet;
        Network n = lanNetwork(c), use = null;
        if (n != null) {
            Socket s = null;
            try {
                s = n.getSocketFactory().createSocket();
                s.connect(new InetSocketAddress(host, port), 1500);
                use = n;
            } catch (Exception ignored) {
            } finally {
                try { if (s != null) s.close(); } catch (Exception ignored) { }
            }
        }
        routeNet = use;
        routeFor = key;
        routeAt = now;
        return use;
    }

    /** 连接出错后调用：下次重新判断走哪条网络。 */
    static synchronized void resetRoute() {
        routeFor = null;
    }

    /** 打开一个到电脑的连接：不走代理，能走 Wi-Fi 就走 Wi-Fi。 */
    static URLConnection connect(Context c, URL u) throws Exception {
        Network n = route(c, u.getHost(), u.getPort() > 0 ? u.getPort() : u.getDefaultPort());
        return n != null ? n.openConnection(u, Proxy.NO_PROXY) : u.openConnection(Proxy.NO_PROXY);
    }

    static HttpURLConnection open(Context c, String path, int timeoutMs) throws Exception {
        HttpsURLConnection h = (HttpsURLConnection) connect(c, new URL(base(c) + path));
        h.setSSLSocketFactory(ssl(c));
        h.setConnectTimeout(5000);
        h.setReadTimeout(timeoutMs);
        h.setUseCaches(false);
        h.setRequestProperty("User-Agent", userAgent());
        h.setRequestProperty("X-T9-Key", prefs(c).getString("key", ""));
        return h;
    }

    static byte[] readAll(InputStream in) throws Exception {
        ByteArrayOutputStream o = new ByteArrayOutputStream();
        byte[] buf = new byte[16384];
        int n;
        while ((n = in.read(buf)) > 0) o.write(buf, 0, n);
        in.close();
        return o.toByteArray();
    }

    static JSONObject getJson(Context c, String path, int timeoutMs) throws Exception {
        HttpURLConnection h = open(c, path, timeoutMs);
        try {
            InputStream in = h.getResponseCode() < 400 ? h.getInputStream() : h.getErrorStream();
            return new JSONObject(new String(readAll(in), "UTF-8"));
        } finally {
            h.disconnect();
        }
    }

    static int post(Context c, String path, String type, byte[] body, int timeoutMs) throws Exception {
        HttpURLConnection h = open(c, path, timeoutMs);
        try {
            h.setDoOutput(true);
            h.setRequestMethod("POST");
            h.setRequestProperty("Content-Type", type);
            h.setFixedLengthStreamingMode(body.length);
            OutputStream o = h.getOutputStream();
            o.write(body);
            o.close();
            int code = h.getResponseCode();
            try {
                readAll(code < 400 ? h.getInputStream() : h.getErrorStream());
            } catch (Exception ignored) {
            }
            return code;
        } finally {
            h.disconnect();
        }
    }

    static int postJson(Context c, String path, JSONObject body, int timeoutMs) throws Exception {
        return post(c, path, "application/json", body.toString().getBytes("UTF-8"), timeoutMs);
    }

    /** 配对：下载电脑根证书（HTTP），核对指纹后存下来。成功返回 null，否则返回给用户看的原因。 */
    static String pair(Context c, String host, int httpPort, int httpsPort, String key, String fp) {
        try {
            HttpURLConnection h = (HttpURLConnection) connect(c, new URL("http://" + host + ":" + httpPort + "/ca.crt"));
            h.setConnectTimeout(5000);
            h.setReadTimeout(8000);
            byte[] pem = readAll(h.getInputStream());
            h.disconnect();
            X509Certificate cert = parseCert(pem);
            String got = sha256Hex(cert.getEncoded());
            if (fp != null && fp.length() > 0 && !got.equalsIgnoreCase(fp)) {
                return "电脑证书和二维码里的不一致，为了安全没有连接。请回到电脑设置页重新扫码。";
            }
            prefs(c).edit()
                    .putString("host", host).putInt("httpPort", httpPort).putInt("port", httpsPort)
                    .putString("key", key).putString("fp", got)
                    .putString("ca", Base64.encodeToString(cert.getEncoded(), Base64.NO_WRAP))
                    .putBoolean("enabled", true).apply();
            return null;
        } catch (Exception e) {
            resetRoute();
            return cantReach(c, "连不上电脑（" + e.getClass().getSimpleName() + "）：确认手机和电脑连的是同一个 Wi-Fi，电脑上 Open Your Eyes 开着。");
        }
    }

    /** 电脑换了地址（比如路由器重新分配）：在本机所在的网段里找一台根证书指纹一致的 Open Your Eyes。 */
    static String rediscover(final Context c) {
        String fp = prefs(c).getString("fp", "");
        if (fp.isEmpty()) return null;
        JSONObject j = scan(c, fp, prefs(c).getInt("httpPort", 39757));
        if (j == null) return null;
        String ip = j.optString("_ip");
        prefs(c).edit().putString("host", ip).putInt("port", j.optInt("httpsPort", prefs(c).getInt("port", 39758))).apply();
        return ip;
    }

    /**
     * 不经过扫码，直接在局域网里找电脑并连接（浏览器没能拉起 App 时用）。
     * 没有配对密钥，电脑会把这台手机列为「新手机请求加入」，用户在电脑上点「允许」后才能用。
     */
    static String findAndPair(Context c) {
        int hp = 39757;
        JSONObject j = scan(c, "", hp);
        if (j == null) return cantReach(c, "没找到电脑：确认电脑上 Open Your Eyes 开着，手机和电脑连的是同一个 Wi-Fi（不要用访客网络）。");
        return pair(c, j.optString("_ip"), hp, j.optInt("httpsPort", 39758), "", j.optString("caSha256"));
    }

    /**
     * 扫描本机所在网段，找 /config.json 带根证书指纹的 Open Your Eyes；fp 非空时只认指纹一致的那台。
     * 先直接走 Wi-Fi 找（绕开 VPN）；VPN 不让绕开时，再用系统默认网络找一遍。
     */
    private static JSONObject scan(Context c, final String fp, final int httpPort) {
        Network wifi = lanNetwork(c);
        JSONObject j = scanVia(c, wifi, fp, httpPort);
        if (j == null && wifi != null) j = scanVia(c, null, fp, httpPort);
        return j;
    }

    /** 本机在局域网里的网段前缀（如 192.168.1.）：优先看 Wi-Fi 的地址，不看 VPN 的。 */
    private static String lanPrefix(Context c, Network wifi) {
        try {
            LinkProperties lp = wifi != null ? cm(c).getLinkProperties(wifi) : null;
            if (lp != null) {
                for (LinkAddress la : lp.getLinkAddresses()) {
                    InetAddress a = la.getAddress();
                    if (a instanceof Inet4Address && a.isSiteLocalAddress()) {
                        String s = a.getHostAddress();
                        return s.substring(0, s.lastIndexOf('.') + 1);
                    }
                }
            }
        } catch (Exception ignored) {
        }
        String prefix = null;
        try {
            for (NetworkInterface ni : Collections.list(NetworkInterface.getNetworkInterfaces())) {
                if (!ni.isUp() || ni.isLoopback()) continue;
                String nm = ni.getName();
                if (nm.startsWith("tun") || nm.startsWith("ppp") || nm.startsWith("ipsec")) continue;   // VPN 的虚拟网卡
                for (InetAddress a : Collections.list(ni.getInetAddresses())) {
                    if (a instanceof Inet4Address && a.isSiteLocalAddress()) {
                        String s = a.getHostAddress();
                        prefix = s.substring(0, s.lastIndexOf('.') + 1);
                    }
                }
            }
        } catch (Exception ignored) {
        }
        return prefix;
    }

    private static JSONObject scanVia(Context c, final Network via, final String fp, final int httpPort) {
        String prefix = lanPrefix(c, via != null ? via : lanNetwork(c));
        if (prefix == null) return null;
        final AtomicReference<JSONObject> found = new AtomicReference<>();
        ExecutorService pool = Executors.newFixedThreadPool(32);
        for (int i = 1; i < 255; i++) {
            final String ip = prefix + i;
            pool.execute(new Runnable() {
                public void run() {
                    if (found.get() != null) return;
                    try {
                        URL u = new URL("http://" + ip + ":" + httpPort + "/config.json");
                        HttpURLConnection h = (HttpURLConnection) (via != null ? via.openConnection(u, Proxy.NO_PROXY) : u.openConnection(Proxy.NO_PROXY));
                        h.setConnectTimeout(400);
                        h.setReadTimeout(800);
                        JSONObject j = new JSONObject(new String(readAll(h.getInputStream()), "UTF-8"));
                        h.disconnect();
                        String got = j.optString("caSha256");
                        if (got.length() == 64 && (fp.isEmpty() || fp.equalsIgnoreCase(got))) {
                            j.put("_ip", ip);
                            found.compareAndSet(null, j);
                        }
                    } catch (Exception ignored) {
                    }
                }
            });
        }
        pool.shutdown();
        try {
            pool.awaitTermination(20, TimeUnit.SECONDS);
        } catch (InterruptedException ignored) {
        }
        return found.get();
    }
}
