#!/usr/bin/env python3
"""打包电脑端安装包（在对应系统上运行）：

  macOS    dist/Open Your Eyes.app + dist/OpenYourEyes-<版本>-mac.dmg（拖进「应用程序」即可）
  Windows  dist/Open Your Eyes/Open Your Eyes.exe（再用 Inno Setup 等做安装程序，见 docs/发布.md）
  Linux    dist/open-your-eyes/open-your-eyes

用法：.venv/bin/python dev/build_app.py
需要：pip install pyinstaller（以及 requirements.txt 里的依赖）
签名 / 公证：设置环境变量 T9_SIGN_ID（"Developer ID Application: …"）后自动签名；没有就只做本机临时签名。
"""
import glob
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST, BUILD = os.path.join(ROOT, "dist"), os.path.join(ROOT, "build")
VERSION = re.search(r'^VERSION = "([^"]+)"', open(os.path.join(ROOT, "server", "serve.py"), encoding="utf-8").read(), re.M).group(1)
# 在 dev/make_universal_env.py 建的 .venv-universal 里运行时，打 Intel + Apple 芯片都能用的通用包
UNIVERSAL = sys.platform == "darwin" and os.path.basename(os.path.dirname(os.path.dirname(sys.executable))) == ".venv-universal"
NAME = "Open Your Eyes" if sys.platform in ("darwin", "win32") else "open-your-eyes"
HIDDEN = ["serve", "certs", "autostart", "power", "eyes_mcp", "integrations", "qrcode", "qrcode.image.svg", "psutil", "PIL.Image"]
HIDDEN += ["webview"]
if sys.platform == "darwin":
    HIDDEN += ["pystray._darwin", "webview.platforms.cocoa"]
elif os.name == "nt":
    HIDDEN += ["pystray._win32", "webview.platforms.edgechromium", "webview.platforms.winforms"]
else:
    HIDDEN += ["pystray._appindicator", "pystray._xorg", "webview.platforms.gtk"]


OPEN_HELP = """Open Your Eyes 安装说明（Mac）

1. 把「Open Your Eyes」拖到旁边的「应用程序」文件夹。
2. 打开「应用程序」，双击 Open Your Eyes。

如果提示「无法打开」「无法验证开发者」或「已损坏」：
这是因为 Open Your Eyes 还没有向苹果付费签名，不是病毒。按下面做一次，以后就能正常打开：

  a. 打开「终端」（在启动台搜索「终端」）。
  b. 复制下面这一整行，粘贴进去，按回车：

     sudo xattr -rd com.apple.quarantine "/Applications/Open Your Eyes.app"

  c. 提示 Password 时输入这台 Mac 的开机密码（输入时屏幕上不显示，正常），再按回车。
  d. 回到「应用程序」，双击 Open Your Eyes。

也可以不用终端：双击一次被拦下后，打开「系统设置 → 隐私与安全性」，
拉到最下面，在 Open Your Eyes 那一行点「仍要打开」。

第一次打开时如果问「是否允许查找本地网络上的设备」，请点「允许」，否则手机连不上。
"""


def mac_icns():
    from PIL import Image
    big = os.path.join(ROOT, "web", "icon-1024.png")   # dev/make_icon.py 生成的大图，缩到各个尺寸更清楚
    src = Image.open(big if os.path.exists(big) else os.path.join(ROOT, "web", "icon-192.png")).convert("RGBA")
    tmp = tempfile.mkdtemp()
    iconset = os.path.join(tmp, "icon.iconset")
    os.makedirs(iconset)
    for s in (16, 32, 64, 128, 256, 512):
        src.resize((s, s), Image.LANCZOS).save(os.path.join(iconset, "icon_%dx%d.png" % (s, s)))
        src.resize((s * 2, s * 2), Image.LANCZOS).save(os.path.join(iconset, "icon_%dx%d@2x.png" % (s, s)))
    out = os.path.join(BUILD, "icon.icns")
    os.makedirs(BUILD, exist_ok=True)
    subprocess.check_call(["iconutil", "-c", "icns", iconset, "-o", out])
    return out


def win_ico():
    from PIL import Image
    out = os.path.join(BUILD, "icon.ico")
    os.makedirs(BUILD, exist_ok=True)
    Image.open(os.path.join(ROOT, "web", "icon-192.png")).save(out, sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128)])
    return out


def main():
    try:   # 安卓 App 随电脑端一起分发（web/app/）；没有安卓工具链就沿用已有的安装包
        subprocess.check_call([sys.executable, os.path.join(ROOT, "dev", "build_android.py")])
        subprocess.check_call([sys.executable, os.path.join(ROOT, "dev", "build_extensions.py")])
    except Exception as exc:
        print("跳过安卓 App 编译：%s" % exc)
    shutil.rmtree(BUILD, ignore_errors=True)
    sep = ";" if os.name == "nt" else ":"
    args = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed", "--name", NAME,
            "--distpath", DIST, "--workpath", BUILD, "--specpath", BUILD,
            "--paths", os.path.join(ROOT, "server"),
            "--add-data", "%s%sweb" % (os.path.join(ROOT, "web"), sep)]
    for h in HIDDEN:
        args += ["--hidden-import", h]
    if sys.platform == "darwin":
        args += ["--icon", mac_icns(), "--osx-bundle-identifier", "com.openyoureyes.app"]
        if UNIVERSAL:
            args += ["--target-arch", "universal2"]
        if os.environ.get("T9_SIGN_ID"):
            args += ["--codesign-identity", os.environ["T9_SIGN_ID"]]
    elif os.name == "nt":
        args += ["--icon", win_ico()]
    args.append(os.path.join(ROOT, "app", "openyoureyes.py"))
    subprocess.check_call(args, cwd=ROOT)
    if sys.platform == "darwin":
        finish_mac()
    write_latest()
    print("完成：", DIST)


def write_latest():
    """dist/latest.json：和安装包一起传到发布地址，已装的电脑程序靠它知道有新版（见 serve.py 的 UPDATE_FEED）。"""
    import json
    with open(os.path.join(DIST, "latest.json"), "w", encoding="utf-8") as f:
        json.dump({"version": VERSION, "page": os.environ.get("T9_RELEASE_PAGE", "https://github.com/ai-shushu/openyoureyes/releases/latest"),
                   "notes": os.environ.get("T9_RELEASE_NOTES", "")}, f, ensure_ascii=False, indent=2)


def finish_mac():
    import plistlib
    app = os.path.join(DIST, NAME + ".app")
    pl = os.path.join(app, "Contents", "Info.plist")
    with open(pl, "rb") as f:
        info = plistlib.load(f)
    info.update({
        "CFBundleShortVersionString": VERSION, "CFBundleVersion": VERSION,
        "CFBundleDisplayName": NAME,
        "LSUIElement": True,                          # 启动时不占程序坞；窗口打开时程序自己切换成普通程序
        "LSMinimumSystemVersion": "11.0",
        "NSHumanReadableCopyright": "Open Your Eyes · 全部在本机与局域网内运行",
        "NSLocalNetworkUsageDescription": "Open Your Eyes 需要和同一 Wi-Fi 下的手机通信，手机才能把照片传到这台电脑。",
    })
    with open(pl, "wb") as f:
        plistlib.dump(info, f)
    ident = os.environ.get("T9_SIGN_ID") or "-"   # "-" = 本机临时签名（改了 Info.plist 必须重签）
    sign = ["codesign", "--force", "--deep", "--sign", ident]
    if ident != "-":
        sign += ["--options", "runtime", "--timestamp"]
    subprocess.check_call(sign + [app])
    # dmg：里面放程序和「应用程序」快捷方式，用户拖过去就装好了
    stage = tempfile.mkdtemp()
    shutil.copytree(app, os.path.join(stage, NAME + ".app"), symlinks=True)
    os.symlink("/Applications", os.path.join(stage, "应用程序"))
    if ident == "-":   # 没有苹果开发者签名：系统会拦，附一份大白话说明
        with open(os.path.join(stage, "打不开？看这里.txt"), "w", encoding="utf-8") as f:
            f.write(OPEN_HELP)
    for old in glob.glob(os.path.join(DIST, "OpenYourEyes-*-mac.dmg")):   # 只留这一版，免得发错
        os.remove(old)
    dmg = os.path.join(DIST, "OpenYourEyes-%s-mac.dmg" % VERSION)
    subprocess.check_call(["hdiutil", "create", "-volname", NAME, "-srcfolder", stage, "-ov", "-format", "UDZO", dmg])
    shutil.rmtree(stage, ignore_errors=True)
    if ident != "-" and os.environ.get("T9_NOTARY_PROFILE"):   # xcrun notarytool store-credentials 存好的凭据名
        subprocess.check_call(["xcrun", "notarytool", "submit", dmg, "--keychain-profile", os.environ["T9_NOTARY_PROFILE"], "--wait"])
        subprocess.check_call(["xcrun", "stapler", "staple", dmg])
    print("dmg：", dmg)


if __name__ == "__main__":
    main()
