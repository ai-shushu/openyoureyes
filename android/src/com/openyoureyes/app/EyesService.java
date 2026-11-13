package com.openyoureyes.app;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.hardware.Sensor;
import android.hardware.SensorEvent;
import android.hardware.SensorEventListener;
import android.hardware.SensorManager;
import android.net.wifi.WifiManager;
import android.os.Build;
import android.os.IBinder;
import android.os.PowerManager;
import android.util.Base64;

import org.json.JSONArray;
import org.json.JSONObject;

import java.net.URLEncoder;

/**
 * 后台常驻：熄屏也保持和电脑的连接。和网页版拍照页用同一套接口：
 *   长轮询 /api/phone/poll 领拍照请求和实时取景会话 → 拍照交 /api/snap；
 *   每分钟交一张封面 /api/phone/cover；实时取景时把画面一帧帧交 /api/live/frame。
 * 只有电脑上 Claude 请求（用户在电脑上允许）时才拍照；封面只给电脑设置页看。
 */
public class EyesService extends Service implements SensorEventListener {
    static volatile String status = "未启动";
    static volatile String computerName = "";
    static volatile String myName = "";
    static volatile boolean running;
    static volatile boolean certChanged;   // 电脑的证书和配对时不一样了：要重新配对

    private Cam cam;
    private Thread worker;
    private PowerManager.WakeLock wake;
    private WifiManager.WifiLock wifiLock;
    private SensorManager sensors;
    private final float[] gravity = new float[3];
    private volatile boolean haveGravity;

    private volatile String liveSid;      // 电脑正在看的取景会话
    private volatile boolean liveHigh;
    private volatile boolean busy;
    private long lastCover;
    private double coverReq = -1;   // 电脑设置页「刷新画面」的时间：变了就马上截一张新封面

    static void start(Context c) {
        Intent i = new Intent(c, EyesService.class);
        if (Build.VERSION.SDK_INT >= 26) c.startForegroundService(i); else c.startService(i);
    }

    static void stop(Context c) {
        c.stopService(new Intent(c, EyesService.class));
    }

    @Override
    public IBinder onBind(Intent intent) { return null; }

    @Override
    public void onCreate() {
        super.onCreate();
        cam = new Cam(this);
        PowerManager pm = (PowerManager) getSystemService(POWER_SERVICE);
        wake = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "openyoureyes:standby");
        wake.acquire();
        WifiManager wm = (WifiManager) getApplicationContext().getSystemService(WIFI_SERVICE);
        wifiLock = wm.createWifiLock(Build.VERSION.SDK_INT >= 29 ? 4 /* LOW_LATENCY */ : WifiManager.WIFI_MODE_FULL_HIGH_PERF, "open-your-eyes");
        wifiLock.acquire();
        sensors = (SensorManager) getSystemService(SENSOR_SERVICE);
        Sensor g = sensors.getDefaultSensor(Sensor.TYPE_GRAVITY);
        if (g == null) g = sensors.getDefaultSensor(Sensor.TYPE_ACCELEROMETER);
        if (g != null) sensors.registerListener(this, g, SensorManager.SENSOR_DELAY_NORMAL);
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        showNotification("正在连接电脑…");
        if (worker == null || !worker.isAlive()) {
            running = true;
            worker = new Thread(new Runnable() { public void run() { loop(); } }, "t9-poll");
            worker.start();
        }
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        running = false;
        liveSid = null;
        if (worker != null) worker.interrupt();
        try { sensors.unregisterListener(this); } catch (Exception ignored) { }
        try { if (wake.isHeld()) wake.release(); } catch (Exception ignored) { }
        try { if (wifiLock.isHeld()) wifiLock.release(); } catch (Exception ignored) { }
        cam.close();
        status = "已停止";
        super.onDestroy();
    }

    private void showNotification(String text) {
        NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        Notification.Builder b;
        if (Build.VERSION.SDK_INT >= 26) {
            nm.createNotificationChannel(new NotificationChannel("t9", "Open Your Eyes 待命", NotificationManager.IMPORTANCE_LOW));
            b = new Notification.Builder(this, "t9");
        } else {
            b = new Notification.Builder(this);
        }
        PendingIntent pi = PendingIntent.getActivity(this, 0, new Intent(this, MainActivity.class),
                Build.VERSION.SDK_INT >= 23 ? PendingIntent.FLAG_IMMUTABLE : 0);
        Notification n = b.setContentTitle("Open Your Eyes").setContentText(text).setSmallIcon(android.R.drawable.ic_menu_camera)
                .setContentIntent(pi).setOngoing(true).build();
        if (Build.VERSION.SDK_INT >= 29) startForeground(1, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_CAMERA);
        else startForeground(1, n);
    }

    private void setStatus(String s) {
        if (!s.equals(status)) {
            status = s;
            showNotification(s);
        }
    }

    // ------------------------------------------------------------------ 主循环

    private void loop() {
        int fails = 0;
        while (running) {
            if (!Net.paired(this)) { setStatus("还没配对：在电脑设置页点「添加机位」扫码"); sleep(3000); continue; }
            try {
                String state = busy ? "busy" : "ready";
                String path = "/api/phone/poll?app=1&vis=1&hold=3&state=" + state + "&facing=environment&torch=" + (cam.hasFlash() ? 1 : 0)
                        + "&id=" + Net.phoneId(this) + "&live=" + (liveSid == null ? "" : liveSid)
                        + "&name=" + URLEncoder.encode(Net.prefs(this).getString("rename", ""), "UTF-8");
                JSONObject j = Net.getJson(this, path, 10000);
                fails = 0;
                certChanged = false;
                JSONObject ports = j.optJSONObject("ports");   // 电脑端换了端口：跟着改
                if (ports != null) {
                    int hs = ports.optInt("https", 0), ht = ports.optInt("http", 0);
                    android.content.SharedPreferences p = Net.prefs(this);
                    if (hs > 0 && hs != p.getInt("port", 39758) || ht > 0 && ht != p.getInt("httpPort", 39757))
                        p.edit().putInt("port", hs).putInt("httpPort", ht).apply();
                }
                if (j.optString("name").length() > 0) {
                    myName = j.optString("name");
                    if (myName.equals(Net.prefs(this).getString("rename", ""))) Net.prefs(this).edit().remove("rename").apply();
                }
                computerName = Net.prefs(this).getString("host", "");
                if (j.optBoolean("pending")) { setStatus("等电脑允许：在电脑 Open Your Eyes 里点这台手机下面的「允许」"); sleep(2000); continue; }
                setStatus("待命中（" + myName + "）· 熄屏也能拍");
                handleLive(j.optJSONArray("live"));
                double cr = j.optDouble("coverReq", 0);
                if (coverReq >= 0 && cr > coverReq) lastCover = 0;
                coverReq = cr;
                JSONObject req = j.optJSONObject("request");
                if (req != null) { handleRequest(req); continue; }
                maybeCover();
            } catch (Exception e) {
                if (isCertError(e)) {   // 证书对不上（不是网络抖动）：电脑重新生成过证书，要重新配对
                    certChanged = true;
                    setStatus("电脑的安全证书变了：请在电脑上点「＋ 添加机位」重新扫码，或在这里点「查找电脑」");
                    sleep(5000);
                    continue;
                }
                fails++;
                setStatus("连不上电脑，正在重试…（确认电脑上 Open Your Eyes 开着、连的是同一个 Wi-Fi）");
                if (fails % 10 == 0) {   // 一直连不上：电脑可能换了地址，在本网段找一找
                    String ip = Net.rediscover(this);
                    if (ip != null) { fails = 0; continue; }
                }
                sleep(Math.min(10000, 1000L * fails));
            }
        }
    }

    private static boolean isCertError(Throwable e) {
        for (Throwable t = e; t != null; t = t.getCause()) {
            if (t instanceof java.security.cert.CertificateException || t instanceof java.security.cert.CertPathValidatorException) return true;
        }
        return false;
    }

    private void sleep(long ms) {
        try { Thread.sleep(ms); } catch (InterruptedException ignored) { }
    }

    // ------------------------------------------------------------------ 拍照

    private void handleRequest(JSONObject req) {
        busy = true;
        String saveLive = liveSid;
        liveSid = null;   // 暂停取景，摄像头让给拍照
        try {
            String id = req.getString("id"), light = req.optString("light", null);
            JSONObject meta = baseMeta(id);
            JSONObject body = new JSONObject();
            if ("clip".equals(req.optString("kind"))) {
                int frames = Math.max(2, Math.min(req.optInt("frames", 4), 8));
                double secs = req.optDouble("seconds", 2);
                JSONArray arr = new JSONArray();
                long gap = (long) (secs * 1000 / Math.max(1, frames - 1));
                for (int i = 0; i < frames; i++) {
                    long t0 = System.currentTimeMillis();
                    arr.put(Base64.encodeToString(cam.photo(light), Base64.NO_WRAP));
                    long w = gap - (System.currentTimeMillis() - t0);
                    if (w > 0 && i < frames - 1) sleep(w);
                }
                meta.put("clipSeconds", secs);
                body.put("frames", arr);
            } else {
                body.put("jpegBase64", Base64.encodeToString(cam.photo(light), Base64.NO_WRAP));
            }
            meta.put("light", new JSONObject().put("luma", cam.lastLuma()).put("torchAvailable", cam.hasFlash()));
            body.put("meta", meta);
            Net.postJson(this, "/api/snap", body, 60000);
        } catch (Exception e) {
            log("拍照失败：" + e.getMessage());
        } finally {
            busy = false;
            liveSid = saveLive;
        }
    }

    private JSONObject baseMeta(String requestId) throws Exception {
        JSONObject m = new JSONObject();
        m.put("requestId", requestId);
        m.put("phoneId", Net.phoneId(this));
        m.put("app", "android " + BuildInfo.VERSION);
        m.put("camera", new JSONObject().put("facing", "environment").put("device", Build.MANUFACTURER + " " + Build.MODEL));
        JSONObject p = pose();
        if (p != null) m.put("pose", p);
        return m;
    }

    /** 拍摄角度，算法与网页版拍照页一致（后置摄像头）。 */
    private JSONObject pose() {
        if (!haveGravity) return null;
        try {
            double n = Math.sqrt(gravity[0] * gravity[0] + gravity[1] * gravity[1] + gravity[2] * gravity[2]);
            if (n < 1e-3) return null;
            double gx = gravity[0] / n, gy = gravity[1] / n, gz = gravity[2] / n;
            double tilt = Math.toDegrees(Math.acos(Math.max(-1, Math.min(1, gz))));
            JSONObject p = new JSONObject();
            p.put("tiltFromDownDeg", r1(tilt));
            p.put("tiltFromLevelDeg", r1(90 - tilt));
            p.put("pitchFrontBackDeg", r1(Math.toDegrees(Math.asin(Math.max(-1, Math.min(1, gy))))));
            p.put("rollLeftRightDeg", r1(Math.toDegrees(Math.asin(Math.max(-1, Math.min(1, gx))))));
            p.put("horizonRollDeg", r1(Math.toDegrees(Math.atan2(gx, gy))));
            if (tilt < 45) { p.put("mode", "俯拍"); p.put("errDeg", r1(tilt)); }
            else if (tilt > 135) { p.put("mode", "仰拍"); p.put("errDeg", r1(180 - tilt)); }
            else { p.put("mode", "平视"); p.put("errDeg", r1(Math.abs(90 - tilt))); }
            return p;
        } catch (Exception e) {
            return null;
        }
    }

    private static double r1(double v) { return Math.round(v * 10) / 10.0; }

    // ------------------------------------------------------------------ 封面（每分钟一张小图，只给电脑设置页看）

    private void maybeCover() {
        long now = System.currentTimeMillis();
        if (liveSid != null || now - lastCover < 60000) return;
        lastCover = now;
        try {
            JSONObject b = new JSONObject();
            b.put("id", Net.phoneId(this));
            b.put("jpegBase64", Base64.encodeToString(cam.small(360), Base64.NO_WRAP));
            Net.postJson(this, "/api/phone/cover", b, 15000);
        } catch (Exception e) {
            log("封面失败：" + e.getMessage());
        }
    }

    // ------------------------------------------------------------------ 实时取景

    private Thread liveThread;

    private void handleLive(JSONArray live) {
        String sid = null;
        boolean high = false;
        if (live != null && live.length() > 0) {
            JSONObject s = live.optJSONObject(0);
            sid = s.optString("sid", null);
            high = "high".equals(s.optString("q"));
        }
        if (sid == null) { liveSid = null; return; }
        if (sid.equals(liveSid) && liveThread != null && liveThread.isAlive()) {
            if (high == liveHigh) return;
            liveSid = null;   // 电脑放大 / 缩小了画面：按新清晰度重开
            try { liveThread.join(3000); } catch (InterruptedException ignored) { }
        }
        liveHigh = high;
        liveSid = sid;
        final String mySid = sid;
        liveThread = new Thread(new Runnable() {
            public void run() {
                log("取景：开始（App 画面流）");
                try {
                    cam.live(liveHigh, liveHigh ? 12 : 8, new Cam.FrameSink() {
                        public boolean onFrame(byte[] jpeg) {
                            if (!mySid.equals(liveSid) || !running) return false;
                            if (jpeg == null) return true;
                            try {
                                int code = Net.post(EyesService.this, "/api/live/frame?sid=" + mySid + "&phone=" + Net.phoneId(EyesService.this),
                                        "image/jpeg", jpeg, 5000);
                                if (code == 410) { liveSid = null; return false; }   // 电脑那边结束了
                            } catch (Exception ignored) {
                            }
                            return true;
                        }
                    });
                } catch (Exception e) {
                    log("取景失败：" + e.getMessage());
                }
            }
        }, "t9-live");
        liveThread.start();
    }

    private void log(String msg) {
        try {
            Net.postJson(this, "/api/phone/log", new JSONObject().put("id", Net.phoneId(this)).put("msg", msg), 5000);
        } catch (Exception ignored) {
        }
    }

    @Override
    public void onSensorChanged(SensorEvent e) {
        if (e.sensor.getType() == Sensor.TYPE_GRAVITY) {
            System.arraycopy(e.values, 0, gravity, 0, 3);
        } else {   // 没有重力传感器：用加速度计做低通滤波
            for (int i = 0; i < 3; i++) gravity[i] = haveGravity ? 0.9f * gravity[i] + 0.1f * e.values[i] : e.values[i];
        }
        haveGravity = true;
    }

    @Override
    public void onAccuracyChanged(Sensor s, int a) { }
}
