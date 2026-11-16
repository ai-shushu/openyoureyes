package com.openyoureyes.app;

import android.Manifest;
import android.app.Activity;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.graphics.Typeface;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.PowerManager;
import android.provider.Settings;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.View;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

/**
 * 主界面：显示连接状态。配对靠电脑设置页的二维码 → 手机浏览器里的教程页 → 「用 App 连接」按钮（openyoureyes://pair?...）。
 */
public class MainActivity extends Activity {
    private TextView state, info, hint;
    private Button toggle, battery, brand, find, update;
    // 底部两个标签：「状态」（文字）和「画面」（全屏摄像头画面，上方信息和网页版拍照页一样）
    private View statusPage, cameraPage, tabBar;
    private TextView tabStatus, tabCamera;
    private android.widget.ImageView preview;
    private TextView hudDot, hudTitle, hudStatus, hudConn, hudGuide, hudPose, hudLight, hudWait;
    private LevelView level;
    private boolean showing;   // 正在「画面」页
    private int shownSeq = -1;
    private long hudAt;
    private boolean resumeCamera;
    private volatile String newVersion;   // 电脑上带着的 App 比这台手机上的新：显示「点这里更新」
    private final Handler ui = new Handler();
    private String pairError;
    private boolean asking;   // 正在弹摄像头授权框：别重复弹

    private static void set(TextView t, String s) {   // 只在文字变了时更新，界面不必每秒重排
        if (!s.contentEquals(t.getText())) t.setText(s);
    }

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        int pad = dp(20);
        LinearLayout col = new LinearLayout(this);
        col.setOrientation(LinearLayout.VERTICAL);
        col.setPadding(pad, pad * 2, pad, pad);
        col.setBackgroundColor(Color.rgb(13, 17, 22));

        // 顶部一行：品牌名 + 版本号
        LinearLayout head = new LinearLayout(this);
        head.setOrientation(LinearLayout.HORIZONTAL);
        head.setGravity(Gravity.BOTTOM);
        TextView title = text(26, Color.WHITE);
        title.setTypeface(Typeface.DEFAULT_BOLD);
        title.setText("Open Your Eyes");
        head.addView(title);
        TextView ver = text(14, Color.rgb(139, 151, 165));
        ver.setText("  v" + BuildInfo.VERSION);
        ver.setPadding(0, 0, 0, dp(4));
        head.addView(ver);
        head.setPadding(0, 0, 0, dp(10));
        col.addView(head);
        TextView sub = text(14, Color.rgb(139, 151, 165));
        sub.setText("让这台手机当 AI 智能体的眼睛。只有你在电脑上允许时才拍照，照片只传到你自己的电脑。");
        col.addView(sub);

        state = text(20, Color.rgb(53, 196, 106));
        state.setPadding(0, dp(24), 0, dp(6));
        col.addView(state);
        info = text(14, Color.rgb(200, 208, 216));
        col.addView(info);


        find = button("在局域网里查找电脑");
        find.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {
                set(state, "正在查找电脑…（约 10 秒）");
                pairError = null;
                new Thread(new Runnable() {
                    public void run() {
                        final String err = Net.findAndPair(MainActivity.this);
                        ui.post(new Runnable() { public void run() { pairError = err; if (err == null) startIfReady(); refresh(); } });
                    }
                }).start();
            }
        });
        col.addView(find);
        update = button("");
        update.setVisibility(View.GONE);
        update.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {   // 从电脑下载新版安装包，系统会弹出安装界面
                android.content.SharedPreferences p = Net.prefs(MainActivity.this);
                startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse("http://" + p.getString("host", "") + ":" +
                        p.getInt("httpPort", 39757) + "/app/openyoureyes.apk")));
            }
        });
        col.addView(update);
        battery = button("允许后台运行（防止熄屏后被系统关掉）");
        battery.setOnClickListener(new View.OnClickListener() { public void onClick(View v) { askBattery(); } });
        col.addView(battery);
        brand = button("自启动设置（" + BrandHelp.label() + "）");
        brand.setOnClickListener(new View.OnClickListener() { public void onClick(View v) { BrandHelp.open(MainActivity.this); } });
        col.addView(brand);
        toggle = button("");
        toggle.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {
                boolean on = EyesService.running;
                Net.prefs(MainActivity.this).edit().putBoolean("enabled", !on).apply();
                if (on) EyesService.stop(MainActivity.this); else startIfReady();
                refresh();
            }
        });
        col.addView(toggle);

        hint = text(13, Color.rgb(139, 151, 165));
        hint.setPadding(0, dp(18), 0, 0);
        col.addView(hint);

        ScrollView sv = new ScrollView(this);
        sv.setBackgroundColor(Color.rgb(13, 17, 22));
        sv.addView(col);
        statusPage = sv;
        cameraPage = buildCameraPage();
        cameraPage.setVisibility(View.GONE);

        android.widget.FrameLayout pages = new android.widget.FrameLayout(this);
        pages.addView(statusPage);
        pages.addView(cameraPage);
        LinearLayout bar = new LinearLayout(this);
        bar.setOrientation(LinearLayout.HORIZONTAL);
        bar.setBackgroundColor(Color.rgb(20, 25, 32));
        tabStatus = tab("状态");
        tabCamera = tab("画面");
        tabStatus.setOnClickListener(new View.OnClickListener() { public void onClick(View v) { setShowing(false); } });
        tabCamera.setOnClickListener(new View.OnClickListener() { public void onClick(View v) { setShowing(true); } });
        bar.addView(tabCamera, new LinearLayout.LayoutParams(0, dp(54), 1));
        bar.addView(tabStatus, new LinearLayout.LayoutParams(0, dp(54), 1));
        tabBar = bar;
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(Color.rgb(13, 17, 22));
        View line = new View(this);
        line.setBackgroundColor(Color.rgb(38, 49, 64));
        root.addView(pages, new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, 0, 1));
        root.addView(line, new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, 1));
        root.addView(bar, new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT));
        setContentView(root);
        markTabs();
        handlePair(getIntent());
    }

    @Override
    protected void onNewIntent(Intent i) {
        super.onNewIntent(i);
        handlePair(i);
    }

    @Override
    protected void onResume() {
        super.onResume();
        startIfReady();
        checkUpdate();
        tick.run();
        if (resumeCamera) { resumeCamera = false; setShowing(true); }
    }

    @Override
    protected void onPause() {
        super.onPause();
        resumeCamera = showing;
        setShowing(false);   // 离开 App 就停掉画面，省电；回来时还在「画面」页
        ui.removeCallbacks(tick);
    }

    private final Runnable tick = new Runnable() {
        public void run() { refresh(); ui.postDelayed(this, 1000); }
    };

    private void setShowing(boolean on) {
        if (on && !EyesService.running) on = false;
        boolean was = showing;
        showing = on;
        EyesService.localPreview = on;
        if (!on) EyesService.previewFrame = null;
        statusPage.setVisibility(on ? View.GONE : View.VISIBLE);
        cameraPage.setVisibility(on ? View.VISIBLE : View.GONE);
        markTabs();
        ui.removeCallbacks(frameTick);
        if (on) {
            if (!was) { shownSeq = -1; preview.setImageDrawable(null); }
            getWindow().addFlags(android.view.WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);   // 看画面时别自己熄屏
            EyesService.previewNote = "";
            new Thread(new Runnable() { public void run() { EyesService.previewChanged(); } }).start();
            ui.post(frameTick);
        } else {
            getWindow().clearFlags(android.view.WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        }
    }

    private void markTabs() {
        tabStatus.setTextColor(showing ? Color.rgb(139, 151, 165) : Color.WHITE);
        tabCamera.setTextColor(showing ? Color.WHITE : Color.rgb(139, 151, 165));
        tabStatus.setTypeface(showing ? Typeface.DEFAULT : Typeface.DEFAULT_BOLD);
        tabCamera.setTypeface(showing ? Typeface.DEFAULT_BOLD : Typeface.DEFAULT);
    }

    private TextView tab(String name) {
        TextView t = text(15, Color.rgb(139, 151, 165));
        t.setText(name);
        t.setGravity(Gravity.CENTER);
        return t;
    }

    /** 「画面」页：全屏摄像头画面 + 上方一层半透明信息（和网页版拍照页一样：状态、提示、拍摄角度、光线、水平仪）。 */
    private View buildCameraPage() {
        android.widget.FrameLayout f = new android.widget.FrameLayout(this);
        f.setBackgroundColor(Color.BLACK);
        preview = new android.widget.ImageView(this);
        preview.setScaleType(android.widget.ImageView.ScaleType.FIT_CENTER);
        f.addView(preview, new android.widget.FrameLayout.LayoutParams(-1, -1));
        hudWait = text(14, Color.rgb(139, 151, 165));
        hudWait.setText("正在打开摄像头…");
        f.addView(hudWait, new android.widget.FrameLayout.LayoutParams(-2, -2, Gravity.CENTER));

        LinearLayout hud = new LinearLayout(this);
        hud.setOrientation(LinearLayout.HORIZONTAL);
        hud.setPadding(dp(12), dp(10), dp(12), dp(22));
        hud.setBackground(new android.graphics.drawable.GradientDrawable(android.graphics.drawable.GradientDrawable.Orientation.TOP_BOTTOM,
                new int[]{Color.argb(160, 0, 0, 0), Color.argb(0, 0, 0, 0)}));
        LinearLayout l = new LinearLayout(this);
        l.setOrientation(LinearLayout.VERTICAL);
        LinearLayout line1 = new LinearLayout(this);
        line1.setOrientation(LinearLayout.HORIZONTAL);
        line1.setGravity(Gravity.CENTER_VERTICAL);
        hudDot = new TextView(this);
        hudDot.setText("●");
        hudDot.setTextSize(TypedValue.COMPLEX_UNIT_SP, 11);
        line1.addView(hudDot);
        hudTitle = text(13, Color.WHITE);
        hudTitle.setText("  后置  ");
        line1.addView(hudTitle);
        hudStatus = text(13, Color.rgb(183, 194, 206));
        line1.addView(hudStatus, new LinearLayout.LayoutParams(0, -2, 1));
        hudConn = text(11, Color.rgb(139, 151, 165));
        hudConn.setPadding(dp(7), dp(1), dp(7), dp(1));
        android.graphics.drawable.GradientDrawable pill = new android.graphics.drawable.GradientDrawable();
        pill.setColor(Color.argb(90, 0, 0, 0));
        pill.setCornerRadius(dp(9));
        hudConn.setBackground(pill);
        line1.addView(hudConn);
        l.addView(line1);
        hudGuide = text(14, Color.rgb(110, 231, 160));
        hudGuide.setTypeface(Typeface.DEFAULT_BOLD);
        hudGuide.setPadding(0, dp(4), 0, dp(2));
        l.addView(hudGuide);
        hudPose = text(12, Color.rgb(183, 194, 206));
        l.addView(hudPose);
        hudLight = text(12, Color.rgb(183, 194, 206));
        l.addView(hudLight);
        hud.addView(l, new LinearLayout.LayoutParams(0, -2, 1));
        level = new LevelView(this);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(dp(54), dp(54));
        lp.leftMargin = dp(10);
        hud.addView(level, lp);
        f.addView(hud, new android.widget.FrameLayout.LayoutParams(-1, -2, Gravity.TOP));
        TextView tip = text(10, Color.argb(200, 183, 194, 206));
        tip.setText("非必要时建议切到「状态」页，手机更省电、不易发热");
        tip.setSingleLine(true);
        tip.setEllipsize(android.text.TextUtils.TruncateAt.END);
        tip.setGravity(Gravity.CENTER);
        tip.setShadowLayer(dp(2), 0, 0, Color.BLACK);
        tip.setPadding(dp(12), 0, dp(12), dp(8));
        f.addView(tip, new android.widget.FrameLayout.LayoutParams(-1, -2, Gravity.BOTTOM));
        return f;
    }

    private static final int OK = Color.rgb(53, 196, 106), MID = Color.rgb(255, 179, 107), BAD = Color.rgb(255, 107, 91);

    private static int cls(double err) { return err <= 2 ? OK : err <= 8 ? MID : BAD; }

    /** 上方信息：每 0.3 秒刷新一次。 */
    private void renderHud() {
        boolean conn = EyesService.running && EyesService.status.startsWith("待命");
        boolean busy = EyesService.busyNow();
        hudDot.setTextColor(busy ? MID : conn ? OK : Color.rgb(107, 118, 132));
        set(hudStatus, busy ? "正在拍照" : conn ? "待命" : "连接中");
        set(hudConn, conn ? "已连接电脑" : "未连接电脑");
        hudConn.setTextColor(conn ? OK : BAD);
        set(hudGuide, conn ? "✓ 放着就行：对 AI 说「睁眼」，它会先问你，你允许后自动拍"
                : "连不上电脑：确认电脑上的 Open Your Eyes 开着，手机和电脑连的是同一个 Wi-Fi");
        hudGuide.setTextColor(conn ? Color.rgb(110, 231, 160) : MID);
        org.json.JSONObject p = EyesService.poseNow();
        if (p != null) {
            double err = p.optDouble("errDeg", 0);
            android.text.SpannableString sp = new android.text.SpannableString(p.optString("mode") + " · 偏 " + Math.round(err) + "°");
            int at = sp.toString().indexOf("偏 ") + 2;
            sp.setSpan(new android.text.style.ForegroundColorSpan(cls(err)), at, sp.length(), 0);
            sp.setSpan(new android.text.style.StyleSpan(Typeface.BOLD), at, sp.length(), 0);
            hudPose.setText(sp);
            boolean flat = "平视".equals(p.optString("mode"));
            level.set(flat ? p.optDouble("horizonRollDeg") : p.optDouble("rollLeftRightDeg"),
                      flat ? -p.optDouble("tiltFromLevelDeg") : -p.optDouble("pitchFrontBackDeg"), err);
        } else {
            set(hudPose, "姿态：等待传感器…");
        }
        int luma = EyesService.lumaNow();
        if (luma >= 0) {
            String lv = luma < 45 ? "太暗" : luma < 85 ? "偏暗" : luma > 205 ? "过亮" : "合适";
            android.text.SpannableString sp = new android.text.SpannableString("☀ 光线 " + lv + " " + Math.round(luma / 2.55));
            sp.setSpan(new android.text.style.ForegroundColorSpan("合适".equals(lv) ? OK : "太暗".equals(lv) ? BAD : MID), 5, sp.length(), 0);
            sp.setSpan(new android.text.style.StyleSpan(Typeface.BOLD), 5, sp.length(), 0);
            hudLight.setText(sp);
        } else {
            set(hudLight, "");
        }
    }

    /** 水平仪：气泡偏离中心表示镜头没有正对（和网页版一样）。 */
    static class LevelView extends View {
        private final android.graphics.Paint pen = new android.graphics.Paint(android.graphics.Paint.ANTI_ALIAS_FLAG);
        private float bx, by;
        private int color = Color.WHITE;

        LevelView(android.content.Context c) { super(c); }

        void set(double x, double y, double err) {
            float k = 1.6f, lim = 20f, d = getResources().getDisplayMetrics().density;
            bx = Math.max(-lim, Math.min(lim, (float) x * k)) * d;
            by = Math.max(-lim, Math.min(lim, (float) y * k)) * d;
            color = err <= 2 ? OK : err > 8 ? BAD : Color.WHITE;
            invalidate();
        }

        @Override
        protected void onDraw(android.graphics.Canvas cv) {
            float w = getWidth(), h = getHeight(), cx = w / 2, cy = h / 2, d = getResources().getDisplayMetrics().density;
            pen.setStyle(android.graphics.Paint.Style.STROKE);
            pen.setStrokeWidth(1.5f * d);
            pen.setColor(Color.argb(115, 255, 255, 255));
            cv.drawCircle(cx, cy, Math.min(w, h) / 2 - d, pen);
            pen.setStrokeWidth(d);
            pen.setColor(Color.argb(90, 255, 255, 255));
            cv.drawLine(cx, 8 * d, cx, h - 8 * d, pen);
            cv.drawLine(8 * d, cy, w - 8 * d, cy, pen);
            cv.drawCircle(cx, cy, 8 * d, pen);
            pen.setStyle(android.graphics.Paint.Style.FILL);
            pen.setColor(color);
            cv.drawCircle(cx + bx, cy + by, 6 * d, pen);
        }
    }

    /** 显示中：每 0.1 秒看一眼有没有新的一帧。 */
    private final Runnable frameTick = new Runnable() {
        public void run() {
            if (!showing) return;
            byte[] f = EyesService.previewFrame;
            int seq = EyesService.previewSeq;
            if (f != null && seq != shownSeq) {
                shownSeq = seq;
                android.graphics.Bitmap bm = android.graphics.BitmapFactory.decodeByteArray(f, 0, f.length);
                if (bm != null) { preview.setImageBitmap(bm); hudWait.setVisibility(View.GONE); }
            } else if (f == null) {
                hudWait.setVisibility(View.VISIBLE);
                String nt = EyesService.previewNote;
                set(hudWait, EyesService.busyNow() ? "正在拍照…" : nt.isEmpty() ? "正在打开摄像头…" : nt + "…");
            }
            long now = System.currentTimeMillis();
            if (now - hudAt > 300) { hudAt = now; renderHud(); }
            ui.postDelayed(this, 100);
        }
    };

    private void handlePair(Intent i) {
        final Uri u = i == null ? null : i.getData();
        if (u == null || !"openyoureyes".equals(u.getScheme())) return;
        state.setText("正在连接电脑…");
        new Thread(new Runnable() {
            public void run() {
                String host = u.getQueryParameter("h");
                int hp = parse(u.getQueryParameter("hp"), 39757), p = parse(u.getQueryParameter("p"), 39758);
                pairError = host == null ? "链接不完整，请回到电脑设置页重新扫码" :
                        Net.pair(MainActivity.this, host, hp, p, u.getQueryParameter("k"), u.getQueryParameter("fp"));
                ui.post(new Runnable() { public void run() { if (pairError == null) startIfReady(); refresh(); } });   // 待命中的服务下一轮自动用新的配对信息
            }
        }).start();
    }

    /** 问电脑：它带着的 App 是不是比这台手机上的新。 */
    private void checkUpdate() {
        if (!Net.paired(this)) return;
        new Thread(new Runnable() {
            public void run() {
                try {
                    android.content.SharedPreferences p = Net.prefs(MainActivity.this);
                    java.net.HttpURLConnection h = (java.net.HttpURLConnection) Net.connect(MainActivity.this, new java.net.URL("http://" +
                            p.getString("host", "") + ":" + p.getInt("httpPort", 39757) + "/config.json"));
                    h.setConnectTimeout(3000);
                    h.setReadTimeout(4000);
                    org.json.JSONObject app = new org.json.JSONObject(new String(Net.readAll(h.getInputStream()), "UTF-8")).optJSONObject("app");
                    h.disconnect();
                    newVersion = app != null && app.optInt("versionCode") > BuildInfo.CODE ? app.optString("versionName") : null;
                } catch (Exception ignored) {
                }
            }
        }).start();
    }

    private static int parse(String s, int d) {
        try { return Integer.parseInt(s); } catch (Exception e) { return d; }
    }

    private void startIfReady() {
        if (!Net.paired(this) || !Net.prefs(this).getBoolean("enabled", true)) return;
        if (Build.VERSION.SDK_INT >= 23 && checkSelfPermission(Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
            if (!asking) { asking = true; requestPermissions(new String[]{Manifest.permission.CAMERA}, 1); }
            return;
        }
        if (!EyesService.running) EyesService.start(this);
    }

    @Override
    public void onRequestPermissionsResult(int code, String[] perms, int[] res) {
        asking = false;
        if (res.length > 0 && res[0] == PackageManager.PERMISSION_GRANTED) startIfReady();
        else hint.setText("需要摄像头权限才能拍照。请在系统设置 → 应用 → Open Your Eyes → 权限里打开「相机」。");
    }

    private void askBattery() {
        try {
            if (Build.VERSION.SDK_INT >= 23) {
                Intent i = new Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:" + getPackageName()));
                startActivity(i);
                return;
            }
        } catch (Exception ignored) {
        }
        startActivity(new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:" + getPackageName())));
    }

    private void refresh() {
        boolean paired = Net.paired(this);
        if (pairError != null) set(state, pairError);
        else if (!paired) set(state, "还没连接电脑");
        else set(state, EyesService.running ? EyesService.status : "已停止");
        String host = Net.prefs(this).getString("host", "");
        set(info, paired ? "电脑：" + host + (EyesService.myName.isEmpty() ? "" : "　本机：" + EyesService.myName) : "");
        toggle.setVisibility(paired ? View.VISIBLE : View.GONE);
        tabBar.setVisibility(paired && EyesService.running ? View.VISIBLE : View.GONE);
        if (showing && !EyesService.running) setShowing(false);
        find.setVisibility(!paired || EyesService.certChanged ? View.VISIBLE : View.GONE);
        set(toggle, EyesService.running ? "停止待命" : "开始待命");
        boolean ignoring = true;
        if (Build.VERSION.SDK_INT >= 23) {
            PowerManager pm = (PowerManager) getSystemService(POWER_SERVICE);
            ignoring = pm.isIgnoringBatteryOptimizations(getPackageName());
        }
        battery.setVisibility(paired && !ignoring ? View.VISIBLE : View.GONE);
        brand.setVisibility(paired && !"other".equals(BrandHelp.brand()) ? View.VISIBLE : View.GONE);
        update.setVisibility(newVersion != null ? View.VISIBLE : View.GONE);
        if (newVersion != null) set(update, "电脑上有新版本 " + newVersion + "，点这里更新");
        set(hint, paired
                ? "可以关掉屏幕、插上充电器放着。手机重启后会自动回到待命。\n熄屏一段时间后掉线的话，按下面设置一次（" + BrandHelp.label() + "）：\n" + BrandHelp.steps()
                : "在电脑上打开 Open Your Eyes，点「＋ 添加机位」，用手机相机扫码，在打开的页面里点「用 App 连接」。\n或者直接点上面的「查找电脑」，找到后在电脑上点这台手机下面的「允许」。");
    }

    private TextView text(int sp, int color) {
        TextView t = new TextView(this);
        t.setTextSize(TypedValue.COMPLEX_UNIT_SP, sp);
        t.setTextColor(color);
        t.setLineSpacing(0, 1.2f);
        return t;
    }

    private Button button(String s) {
        Button b = new Button(this);
        b.setText(s);
        b.setAllCaps(false);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT);
        lp.topMargin = dp(12);
        b.setLayoutParams(lp);
        b.setGravity(Gravity.CENTER);
        return b;
    }

    private int dp(int v) {
        return (int) TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, v, getResources().getDisplayMetrics());
    }
}
