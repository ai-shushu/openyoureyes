#!/usr/bin/env python3
"""Open Your Eyes 电脑端程序（跨平台）。用户只需要打开它，其余全部自动。

  open-your-eyes                启动：菜单栏 / 任务栏出现图标，并打开 Open Your Eyes 窗口（已在运行就把窗口显示出来）
  open-your-eyes --no-browser   启动但不打开设置页（开机自启用）
  open-your-eyes --no-tray      不显示图标（没有图形界面的电脑）
  open-your-eyes stop | status  停止 / 查看是否在运行
  open-your-eyes --serve        （内部用）页面服务本身

它是一个「管家」：在后台运行页面服务（server/serve.py），服务意外退出就自动重启
（短时间内反复崩溃会逐渐拉长间隔，免得空转）。证书、电脑地址变化、照片清理由页面服务自己维护。
设置页：http://127.0.0.1:39757/settings

安装版（PyInstaller 打包）的数据放在用户目录：
  macOS   ~/Library/Application Support/Open Your Eyes
  Windows %APPDATA%\\Open Your Eyes
  Linux   ~/.local/share/open-your-eyes
开发版仍放在仓库里（snaps/、server/certs/、server/logs/）。
"""
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser

FROZEN = bool(getattr(sys, "frozen", False))
if FROZEN:
    BUNDLE = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    if sys.platform == "darwin":
        DATA = os.path.expanduser("~/Library/Application Support/Open Your Eyes")
    elif os.name == "nt":
        DATA = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "Open Your Eyes")
    else:
        DATA = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "open-your-eyes")
    # 改名前的数据（照片、证书、机位名单）原样搬过来：先后叫过 T9 Eyes、AgentEye
    for _a, _b in (("Open Your Eyes", "AgentEye"), ("Open Your Eyes", "T9 Eyes")):
        _old = DATA.replace(_a, _b).replace("open-your-eyes", "agenteye" if _b == "AgentEye" else "t9-eyes")
        if os.path.isdir(_old) and not os.path.exists(DATA):
            try:
                os.rename(_old, DATA)
            except OSError:
                pass
    os.environ.setdefault("T9_WEB_ROOT", os.path.join(BUNDLE, "web"))
    os.environ.setdefault("T9_SNAP_DIR", os.path.join(DATA, "snaps"))
    os.environ.setdefault("T9_CERT_DIR", os.path.join(DATA, "certs"))
    os.environ.setdefault("T9_LOG_DIR", os.path.join(DATA, "logs"))
    ROOT = BUNDLE
    LOG_DIR = os.environ["T9_LOG_DIR"]
else:
    ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    LOG_DIR = os.environ.get("T9_LOG_DIR") or os.path.join(ROOT, "server", "logs")
    sys.path.insert(0, os.path.join(ROOT, "server"))

SERVE = os.path.join(ROOT, "server", "serve.py")
PID_FILE = os.path.join(LOG_DIR, "openyoureyes.pid")
DEFAULT_PORT = 39757
PORT = int(os.environ.get("T9_WEB_PORT", str(DEFAULT_PORT)))
SETTINGS_URL = "http://127.0.0.1:%d/settings" % PORT
EXIT_PORT_BUSY = 3
_np = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # 本机请求绕开系统代理


def _get(path, timeout=2):
    with _np.open("http://127.0.0.1:%d%s" % (PORT, path), timeout=timeout) as r:
        return r.read()


def running():
    try:
        return json.loads(_get("/api/status")).get("ok") is True
    except Exception:
        return False


def _read_pid():
    try:
        with open(PID_FILE) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def stop():
    pid = _read_pid()
    if not pid:
        print("Open Your Eyes 没有在运行")
        return
    try:
        os.remove(PID_FILE)   # 带托盘图标时信号可能被图形界面挡住：程序发现 PID 文件没了也会自己退出
    except OSError:
        pass
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass
    for _ in range(30):
        if not running():
            break
        time.sleep(0.2)
    print("Open Your Eyes 已停止")


class Supervisor:
    """在后台运行页面服务，崩了就重启。status 给托盘图标显示。"""

    def __init__(self, open_browser):
        self.open_browser = open_browser
        self.stopping = False
        self.child = None
        self.status = "正在启动…"
        self.problem = None   # 需要告诉用户的问题（大白话）
        self.on_ready = None  # 页面服务第一次就绪时调用（程序窗口用它加载设置页）

    def _cmd(self):
        if FROZEN:
            return [sys.executable, "--serve"]
        return [sys.executable, "-u", SERVE]

    def run(self):
        fails = []
        opened = False
        while not self.stopping:
            lp = os.path.join(LOG_DIR, "web.log")
            try:   # 日志超过 5 MB 就换一个新文件（只留上一份），长期运行也不会越积越大
                if os.path.getsize(lp) > 5 * 1024 * 1024:
                    os.replace(lp, lp + ".1")
            except OSError:
                pass
            log = open(lp, "a", encoding="utf-8")
            log.write("\n===== %s 启动页面服务 =====\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
            log.flush()
            env = dict(os.environ, PYTHONUNBUFFERED="1")
            kw = {"creationflags": 0x08000000} if os.name == "nt" else {}   # Windows：不弹黑窗口
            p = self.child = subprocess.Popen(self._cmd(), stdout=log, stderr=subprocess.STDOUT, cwd=ROOT, env=env, **kw)
            for _ in range(60):
                if running() or p.poll() is not None:
                    break
                time.sleep(0.25)
            if running():
                self.status, self.problem = "运行中", None
                if not opened:
                    opened = True
                    if self.on_ready:
                        self.on_ready()
                    elif self.open_browser:
                        webbrowser.open(SETTINGS_URL)
            code = p.wait()
            log.write("===== %s 页面服务退出（代码 %s） =====\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), code))
            log.close()
            if self.stopping:
                break
            if code == EXIT_PORT_BUSY:
                self.status = "无法启动"
                self.problem = "端口 %d 被别的程序占用了。请关掉占用它的程序，或重启电脑后再打开 Open Your Eyes。" % PORT
                wait = 30
            else:
                self.status = "正在重新启动…"
                now = time.time()
                fails = [t for t in fails if now - t < 300] + [now]
                wait = min(60, 2 ** min(len(fails), 6))   # 2、4、8…秒，最多 1 分钟
            for _ in range(int(wait * 4)):
                if self.stopping:
                    break
                time.sleep(0.25)

    def shutdown(self):
        self.stopping = True
        p = self.child
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(5)
            except Exception:
                p.kill()


def export_diagnostics():
    """把诊断包存到「下载」（没有就桌面），并在文件夹里显示出来。"""
    data = _get("/api/diagnostics.zip", timeout=15)
    for d in ("~/Downloads", "~/Desktop", "~"):
        d = os.path.expanduser(d)
        if os.path.isdir(d):
            break
    fp = os.path.join(d, "OpenYourEyes-诊断-%s.zip" % time.strftime("%Y%m%d-%H%M"))
    with open(fp, "wb") as f:
        f.write(data)
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-R", fp])
    elif os.name == "nt":
        subprocess.Popen(["explorer", "/select,", fp])
    else:
        subprocess.Popen(["xdg-open", d])
    return fp


def _tray_image():
    """菜单栏 / 任务栏图标：和程序图标一样（橙色圆角底 + 一对白眼睛）。"""
    from PIL import Image, ImageDraw
    web = os.environ.get("T9_WEB_ROOT") or os.path.join(ROOT, "web")
    try:
        im = Image.open(os.path.join(web, "icon-192.png")).convert("RGBA").resize((256, 256), Image.LANCZOS)
    except OSError:
        return Image.new("RGBA", (64, 64), (208, 110, 79, 255))
    mask = Image.new("L", im.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, 255, 255], radius=56, fill=255)
    out = Image.new("RGBA", im.size, (0, 0, 0, 0))
    out.paste(im, (0, 0), mask)
    return out.resize((64, 64), Image.LANCZOS)


LOADING_HTML = """<!doctype html><meta charset="utf-8"><body style="margin:0;height:100vh;display:flex;align-items:center;
justify-content:center;font:15px -apple-system,'PingFang SC','Microsoft YaHei',sans-serif;color:#8b97a5;background:#14181d">
Open Your Eyes 正在启动…</body>"""
SHOW_FILE = os.path.join(LOG_DIR, "show.request")


def _downloads_dir():
    for d in ("~/Downloads", "~/Desktop", "~"):
        d = os.path.expanduser(d)
        if os.path.isdir(d):
            return d


def _reveal(fp):
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-R", fp])
    elif os.name == "nt":
        subprocess.Popen(["explorer", "/select,", fp])
    else:
        subprocess.Popen(["xdg-open", os.path.dirname(fp)])


class Api:
    """设置页在程序窗口里时可以调用的功能（window.pywebview.api.*）。"""

    def export_diagnostics(self):
        return export_diagnostics()

    def save_extension(self):
        """把 Claude 桌面版扩展存到「下载」并选中，用户双击即可安装。"""
        data = _get("/app/open-your-eyes.mcpb", timeout=10)
        fp = os.path.join(_downloads_dir(), "OpenYourEyes.mcpb")
        with open(fp, "wb") as f:
            f.write(data)
        _reveal(fp)
        return fp

    def copy_text(self, text):
        text = str(text)
        if sys.platform == "darwin":
            subprocess.run(["pbcopy"], input=text.encode("utf-8"))
        elif os.name == "nt":
            subprocess.run(["clip"], input=text.encode("utf-16-le"))
        else:
            subprocess.run(["xclip", "-selection", "clipboard"], input=text.encode("utf-8"))
        return True


def run_gui(sup, show_window):
    """菜单栏图标 + 程序自己的窗口（系统自带网页组件显示设置页）。
    关窗口只是隐藏，程序继续在后台运行；从菜单栏或再次打开程序可以重新显示。
    没有网页组件（比如 Linux 缺 GTK）时退回用浏览器打开设置页。"""
    import pystray
    try:
        import webview
        webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
    except Exception:
        webview = None
    st = {"text": "Open Your Eyes：正在启动…", "auto": False, "quitting": False, "visible": show_window}
    win = None
    mac = sys.platform == "darwin"

    def dock(visible):   # Mac：窗口开着时出现在程序坞，关了只留菜单栏图标
        if not mac:
            return
        try:
            from PyObjCTools import AppHelper
            import AppKit

            def apply():
                app = AppKit.NSApplication.sharedApplication()
                app.setActivationPolicy_(0 if visible else 1)
                if visible:
                    app.activateIgnoringOtherApps_(True)
            AppHelper.callAfter(apply)
        except Exception:
            pass

    def show(*_):
        if win is None:
            webbrowser.open(SETTINGS_URL)
            return
        st["visible"] = True
        dock(True)
        win.show()
        try:
            win.restore()
        except Exception:
            pass

    def tell_hidden():
        """第一次关窗口时说一声：程序还在后台，AI 照样能拍；从菜单栏图标找回窗口。"""
        mark = os.path.join(LOG_DIR, ".hide-tip-shown")
        if os.path.exists(mark):
            return
        try:
            open(mark, "w").close()
        except OSError:
            pass
        msg = "Open Your Eyes 还在后台运行，AI 照样能拍照。要再打开窗口，点" + ("屏幕右上角菜单栏" if mac else "任务栏右下角") + "的眼睛图标。"
        try:
            if mac:
                subprocess.Popen(["osascript", "-e", 'display notification "%s" with title "Open Your Eyes"' % msg])
            else:
                icon.notify(msg, "Open Your Eyes")
        except Exception:
            pass

    def on_closing():
        if st["quitting"]:
            return True
        st["visible"] = False
        win.hide()
        dock(False)
        tell_hidden()
        return False   # 取消关闭：只是藏起来

    def on_ready():
        if win is not None:
            win.load_url(SETTINGS_URL + "?app=1")
        elif show_window:
            webbrowser.open(SETTINGS_URL)

    sup.on_ready = on_ready

    def poll():
        while not sup.stopping:
            try:
                s = json.loads(_get("/api/settings", timeout=3))
                on = [p for p in s["phones"] if p.get("online")]
                pend = [p for p in s["phones"] if p.get("pending")]
                t = ("%d 台手机在线" % len(on) if on else "运行中，没有手机在线") + " · v" + str(s.get("version"))
                if pend:
                    t += "（%d 台新手机等你允许）" % len(pend)
                st["text"], st["auto"] = t, bool(s["autostart"].get("enabled"))
                if win is not None and s.get("version") and st.get("title") != s["version"]:
                    st["title"] = s["version"]
                    try:
                        win.set_title("Open Your Eyes v" + s["version"])   # 窗口标题栏显示品牌名和版本号
                    except Exception:
                        pass
            except Exception:
                st["text"] = sup.problem or sup.status
            try:
                icon.update_menu()
            except Exception:
                pass
            time.sleep(5)

    def toggle_auto(_icon, _item):
        try:
            req = urllib.request.Request("http://127.0.0.1:%d/api/autostart" % PORT, method="POST",
                                         data=json.dumps({"on": not st["auto"]}).encode(),
                                         headers={"Content-Type": "application/json", "X-T9": "1"})
            _np.open(req, timeout=5).read()
            st["auto"] = not st["auto"]
        except Exception:
            pass

    def diag(_icon, _item):
        try:
            export_diagnostics()
        except Exception as exc:
            icon.notify("导出失败：%s" % exc, "Open Your Eyes")

    def quit_(*_):
        st["quitting"] = True
        sup.shutdown()
        try:
            icon.stop()
        except Exception:
            pass
        if win is not None:
            try:
                win.destroy()
            except Exception:
                pass

    icon = pystray.Icon("Open Your Eyes", _tray_image(), "Open Your Eyes", menu=pystray.Menu(
        pystray.MenuItem(lambda _i: st["text"], None, enabled=False),
        pystray.MenuItem("打开 Open Your Eyes", show, default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("开机自动启动", toggle_auto, checked=lambda _i: st["auto"]),
        pystray.MenuItem("导出诊断（出问题时发给开发者）", diag),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出 Open Your Eyes", quit_),
    ))

    def watch():
        while not sup.stopping:
            if _read_pid() != os.getpid():   # stop 命令删掉 PID 文件 = 请退出
                quit_()
                return
            if os.path.exists(SHOW_FILE):    # 用户又打开了一次程序 = 想看窗口
                try:
                    os.remove(SHOW_FILE)
                except OSError:
                    pass
                show()
            time.sleep(0.5)

    threading.Thread(target=poll, daemon=True).start()
    threading.Thread(target=watch, daemon=True).start()
    if webview is None:
        icon.run()   # 只有菜单栏图标，设置页用浏览器打开
        return
    win = webview.create_window("Open Your Eyes", html=LOADING_HTML, width=1280, height=820, min_size=(760, 560),
                                hidden=not show_window, js_api=Api(), background_color="#14181D")
    win.events.closing += on_closing
    if mac:
        _mac_reopen_hook(show)
        icon.run_detached()   # Mac 上图标和窗口共用一个主线程消息循环
    else:
        threading.Thread(target=icon.run, daemon=True).start()
    if not show_window:
        dock(False)
    webview.start(private_mode=False, storage_path=os.path.join(LOG_DIR, "webview"))
    quit_()


def _mac_reopen_hook(show):
    """Mac：程序已在运行时用户再双击它（或点程序坞图标），系统不会再开一个，而是通知已运行的这个——这时显示窗口。"""
    try:
        import objc
        from webview.platforms.cocoa import BrowserView

        def applicationShouldHandleReopen_hasVisibleWindows_(self, app, has_visible):
            show()
            return True
        objc.classAddMethods(BrowserView.AppDelegate, [
            objc.selector(applicationShouldHandleReopen_hasVisibleWindows_,
                          selector=b"applicationShouldHandleReopen:hasVisibleWindows:", signature=b"Z@:@Z")])
    except Exception:
        pass


def _first_run_setup():
    """安装版第一次运行：默认开机自启（用户可在设置页或菜单里关掉）。"""
    if not FROZEN or PORT != DEFAULT_PORT:   # 用别的端口试运行（测试）时不动开机自启
        return
    mark = os.path.join(LOG_DIR, ".first-run-done")
    if os.path.exists(mark):
        return
    try:
        import autostart
        autostart.enable()
    except Exception:
        pass
    with open(mark, "w") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S"))


def launch(open_browser, tray):
    if running():   # 已经开着：用户双击图标多半是想看设置页
        if open_browser:
            if tray and _read_pid():
                with open(SHOW_FILE, "w") as f:   # 让正在运行的那个显示窗口
                    f.write("1")
            else:
                webbrowser.open(SETTINGS_URL)
        print("Open Your Eyes 已在运行：%s" % SETTINGS_URL)
        return
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))
    _first_run_setup()
    sup = Supervisor(open_browser and not tray)   # 有窗口时不再打开浏览器

    def on_signal(*_):
        sup.shutdown()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    try:
        if tray:
            try:
                import pystray  # noqa: F401
            except Exception:
                tray = False
        if tray:
            threading.Thread(target=sup.run, daemon=True).start()
            run_gui(sup, show_window=open_browser)   # 图标和窗口必须在主线程
        else:
            sup.run()
    finally:
        sup.shutdown()
        if _read_pid() == os.getpid():
            try:
                os.remove(PID_FILE)
            except OSError:
                pass


def main(argv):
    if "--serve" in argv:   # 安装版：同一个程序带这个参数就是页面服务
        import serve
        serve.main()
    elif "stop" in argv:
        stop()
    elif "status" in argv:
        print("Open Your Eyes 运行中：%s" % SETTINGS_URL if running() else "Open Your Eyes 没有在运行")
    else:
        headless = sys.platform.startswith("linux") and not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY")
        launch(open_browser="--no-browser" not in argv, tray="--no-tray" not in argv and not headless)


if __name__ == "__main__":
    main(sys.argv[1:])
