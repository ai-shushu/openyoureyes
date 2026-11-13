package com.openyoureyes.app;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** 手机重启、App 升级后自动回到待命（用户在 App 里点过「停止」就不自动启动）。 */
public class BootReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context c, Intent i) {
        if (Net.paired(c) && Net.prefs(c).getBoolean("enabled", true)) EyesService.start(c);
    }
}
