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
        setContentView(sv);
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
    }

    @Override
    protected void onPause() {
        super.onPause();
        ui.removeCallbacks(tick);
    }

    private final Runnable tick = new Runnable() {
        public void run() { refresh(); ui.postDelayed(this, 1000); }
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
                    java.net.HttpURLConnection h = (java.net.HttpURLConnection) new java.net.URL("http://" + p.getString("host", "") + ":" +
                            p.getInt("httpPort", 39757) + "/config.json").openConnection();
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
