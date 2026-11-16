package com.openyoureyes.app;

import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.net.Uri;
import android.os.Build;
import android.provider.Settings;

/** 各品牌手机「自启动 / 后台运行」设置：国产手机熄屏后常把后台 App 关掉，需要用户手动放行一次。 */
final class BrandHelp {
    private BrandHelp() {}

    static String brand() {
        String m = (Build.MANUFACTURER + " " + Build.BRAND).toLowerCase();
        if (m.contains("xiaomi") || m.contains("redmi") || m.contains("poco")) return "xiaomi";
        if (m.contains("honor")) return "honor";
        if (m.contains("huawei")) return "huawei";
        if (m.contains("oppo") || m.contains("realme") || m.contains("oneplus")) return "oppo";
        if (m.contains("vivo") || m.contains("iqoo")) return "vivo";
        if (m.contains("samsung")) return "samsung";
        if (m.contains("meizu")) return "meizu";
        return "other";
    }

    static String label() {
        switch (brand()) {
            case "xiaomi": return "小米 / 红米";
            case "honor": return "荣耀";
            case "huawei": return "华为";
            case "oppo": return "OPPO / 一加 / 真我";
            case "vivo": return "vivo / iQOO";
            case "samsung": return "三星";
            case "meizu": return "魅族";
            default: return "这台手机";
        }
    }

    /** 给用户看的步骤（点按钮会尽量直接打开对应页面，打不开就按这个找）。 */
    static String steps() {
        switch (brand()) {
            case "xiaomi": return "① 打开「自启动」② 设置 → 应用设置 → 应用管理 → Open Your Eyes → 省电策略 →「无限制」\n③ 最近任务里把 Open Your Eyes 下拉「锁定」";
            case "honor":
            case "huawei": return "设置 → 电池 → 启动管理（或「应用启动管理」）→ 找到 Open Your Eyes，关掉「自动管理」，在弹窗里把三个开关都打开";
            case "oppo": return "设置 → 电池 → 更多设置 / 应用耗电管理 → Open Your Eyes → 打开「允许自启动」「允许后台运行」\n最近任务里把 Open Your Eyes 下拉「锁定」";
            case "vivo": return "i 管家 → 应用管理 → 权限管理 → 自启动 → 打开 Open Your Eyes\n设置 → 电池 → 后台耗电管理 → Open Your Eyes →「允许后台高耗电」";
            case "samsung": return "设置 → 电池 → 后台使用限制 → 「从不休眠的应用」里添加 Open Your Eyes";
            case "meizu": return "手机管家 → 权限管理 → 后台管理 → Open Your Eyes →「允许后台运行」";
            default: return "设置 → 电池 / 应用管理 → Open Your Eyes → 允许自启动和后台运行";
        }
    }

    private static ComponentName[] pages() {
        switch (brand()) {
            case "xiaomi": return new ComponentName[]{
                    new ComponentName("com.miui.securitycenter", "com.miui.permcenter.autostart.AutoStartManagementActivity")};
            case "honor":
            case "huawei": return new ComponentName[]{
                    new ComponentName("com.huawei.systemmanager", "com.huawei.systemmanager.startupmgr.ui.StartupNormalAppListActivity"),
                    new ComponentName("com.huawei.systemmanager", "com.huawei.systemmanager.appcontrol.activity.StartupAppControlActivity"),
                    new ComponentName("com.huawei.systemmanager", "com.huawei.systemmanager.optimize.process.ProtectActivity"),
                    new ComponentName("com.hihonor.systemmanager", "com.hihonor.systemmanager.startupmgr.ui.StartupNormalAppListActivity")};
            case "oppo": return new ComponentName[]{
                    new ComponentName("com.coloros.safecenter", "com.coloros.safecenter.permission.startup.StartupAppListActivity"),
                    new ComponentName("com.coloros.safecenter", "com.coloros.safecenter.startupapp.StartupAppListActivity"),
                    new ComponentName("com.oppo.safe", "com.oppo.safe.permission.startup.StartupAppListActivity")};
            case "vivo": return new ComponentName[]{
                    new ComponentName("com.vivo.permissionmanager", "com.vivo.permissionmanager.activity.BgStartUpManagerActivity"),
                    new ComponentName("com.iqoo.secure", "com.iqoo.secure.ui.phoneoptimize.BgStartUpManager"),
                    new ComponentName("com.iqoo.secure", "com.iqoo.secure.ui.phoneoptimize.AddWhiteListActivity")};
            case "samsung": return new ComponentName[]{
                    new ComponentName("com.samsung.android.lool", "com.samsung.android.sm.battery.ui.BatteryActivity"),
                    new ComponentName("com.samsung.android.lool", "com.samsung.android.sm.ui.battery.BatteryActivity")};
            case "meizu": return new ComponentName[]{
                    new ComponentName("com.meizu.safe", "com.meizu.safe.permission.SmartBGActivity")};
            default: return new ComponentName[0];
        }
    }

    /** 尽量直接打开品牌的自启动页面；都打不开就打开这个 App 的系统设置页。 */
    static void open(Context c) {
        for (ComponentName cn : pages()) {
            try {
                Intent i = new Intent().setComponent(cn).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                c.startActivity(i);
                return;
            } catch (Exception ignored) {
            }
        }
        try {
            c.startActivity(new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:" + c.getPackageName()))
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
        } catch (Exception ignored) {
        }
    }
}
