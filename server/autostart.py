"""开机自动启动 Open Your Eyes（登录后在后台运行，不弹窗口）。

  macOS   ~/Library/LaunchAgents/com.openyoureyes.app.plist
  Windows 「启动」文件夹里的 Open Your Eyes.cmd（用 pythonw 运行，没有黑窗口）
  Linux   ~/.config/autostart/openyoureyes.desktop
"""
import os
import sys

APP = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app", "openyoureyes.py"))
LABEL = "com.openyoureyes.app"


def _python():
    exe = sys.executable
    if os.name == "nt":   # pythonw 不弹黑窗口
        w = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(w):
            exe = w
    return exe


def _path():
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/LaunchAgents/%s.plist" % LABEL)
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "Microsoft", "Windows", "Start Menu", "Programs", "Startup", "Open Your Eyes.cmd")
    return os.path.expanduser("~/.config/autostart/openyoureyes.desktop")


def _xml(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _cmd():
    """开机要执行的命令：安装版就是程序本身，开发版是 python + app/openyoureyes.py。"""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [_python(), APP]


def _content():
    cmd = _cmd() + ["--no-browser"]
    if sys.platform == "darwin":
        return """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>%s</string>
  <key>ProgramArguments</key>
  <array>%s</array>
  <key>RunAtLoad</key><true/>
  <key>ProcessType</key><string>Background</string>
</dict>
</plist>
""" % (LABEL, "".join("<string>%s</string>" % _xml(c) for c in cmd))
    if os.name == "nt":
        return '@echo off\r\nstart "" %s\r\n' % " ".join('"%s"' % c for c in cmd)
    return ("[Desktop Entry]\nType=Application\nName=Open Your Eyes\nComment=把手机当作 Claude 的眼睛\n"
            "Exec=%s\nX-GNOME-Autostart-enabled=true\nNoDisplay=true\n" % " ".join('"%s"' % c for c in cmd))


# 改名前（T9 Eyes）的开机自启文件：改名后自动换成新的，免得开机时启动旧程序
def _old_paths():
    """先后用过的名字：T9 Eyes、AgentEye。"""
    if sys.platform == "darwin":
        return [os.path.expanduser("~/Library/LaunchAgents/%s.plist" % n) for n in ("com.t9eyes.app", "com.agenteye.app")]
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return [os.path.join(base, "Microsoft", "Windows", "Start Menu", "Programs", "Startup", n) for n in ("T9 Eyes.cmd", "AgentEye.cmd")]
    return [os.path.expanduser("~/.config/autostart/%s" % n) for n in ("t9eyes.desktop", "agenteye.desktop")]


def migrate():
    """旧名字的开机自启还在：换成新的（保持用户原来的选择）。"""
    old = [p for p in _old_paths() if os.path.exists(p)]
    if not old:
        return False
    for p in old:
        try:
            os.remove(p)
        except OSError:
            pass
    enable()
    return True


def status():
    p = _path()
    if not os.path.exists(p):
        return {"enabled": False, "path": p}
    try:
        with open(p, encoding="utf-8") as f:
            current = f.read() == _content()
    except OSError:
        current = False
    # current=False：开着，但指向的是旧位置的程序（比如程序搬了家），重新打开一次即可修正
    return {"enabled": True, "current": current, "path": p}


def enable():
    p = _path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write(_content())
    return status()


def disable():
    p = _path()
    if os.path.exists(p):
        os.remove(p)
    return status()
