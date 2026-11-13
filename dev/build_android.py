#!/usr/bin/env python3
"""编译安卓 App（不用 Gradle / Android Studio，只用 JDK + 安卓 build-tools），输出到 web/app/：
  web/app/openyoureyes.apk     电脑端 Open Your Eyes 自带，手机扫码后直接从电脑下载安装
  web/app/version.json   版本号，App 和教程页用来判断要不要更新

工具链放在仓库外层的 tools/android/（不入库）：jdk/、sdk/（platforms;android-34、build-tools;34.0.0）。
签名密钥：tools/android/release.keystore（第一次自动生成，务必备份——换了密钥，用户手机上的 App 就没法直接升级）。

用法：.venv/bin/python dev/build_android.py
"""
import glob
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.environ.get("T9_ANDROID_TOOLS") or os.path.join(ROOT, "tools", "android")
if not os.path.isdir(TOOLS):   # worktree 里开发时，工具链在主仓库
    TOOLS = os.path.join(os.path.dirname(ROOT.split("/.claude/worktrees/")[0] + "/"), "tools", "android") \
        if "/.claude/worktrees/" in ROOT else TOOLS
JAVA_HOME = os.path.join(TOOLS, "jdk", "Contents", "Home")
if not os.path.isdir(JAVA_HOME):
    JAVA_HOME = os.path.join(TOOLS, "jdk")
SDK = os.path.join(TOOLS, "sdk")
BT = sorted(glob.glob(os.path.join(SDK, "build-tools", "*")))[-1]
ANDROID_JAR = sorted(glob.glob(os.path.join(SDK, "platforms", "android-*", "android.jar")))[-1]
SRC = os.path.join(ROOT, "android")
OUT = os.path.join(ROOT, "build", "android")
WEB = os.path.join(ROOT, "web", "app")


def run(*args, **kw):
    subprocess.check_call(list(args), **kw)


def main():
    # 版本号只在 server/serve.py 里定义：这里同步进 AndroidManifest.xml 和 BuildInfo.java
    vname = re.search(r'^VERSION = "([^"]+)"', open(os.path.join(ROOT, "server", "serve.py"), encoding="utf-8").read(), re.M).group(1)
    a, b, c = (int(x) for x in vname.split("."))
    vcode = a * 10000 + b * 100 + c
    mp = os.path.join(SRC, "AndroidManifest.xml")
    manifest = open(mp, encoding="utf-8").read()
    manifest = re.sub(r'versionCode="\d+"', 'versionCode="%d"' % vcode, manifest)
    manifest = re.sub(r'versionName="[^"]+"', 'versionName="%s"' % vname, manifest)
    open(mp, "w", encoding="utf-8").write(manifest)
    bp = os.path.join(SRC, "src", "com", "openyoureyes", "app", "BuildInfo.java")
    binfo = open(bp, encoding="utf-8").read()
    binfo = re.sub(r'VERSION = "[^"]+";', 'VERSION = "%s";' % vname, binfo)
    binfo = re.sub(r"CODE = \d+;", "CODE = %d;" % vcode, binfo)
    open(bp, "w", encoding="utf-8").write(binfo)

    shutil.rmtree(OUT, ignore_errors=True)
    os.makedirs(os.path.join(OUT, "classes"))
    env = dict(os.environ, JAVA_HOME=JAVA_HOME, PATH=os.path.join(JAVA_HOME, "bin") + os.pathsep + os.environ["PATH"])

    run(os.path.join(BT, "aapt2"), "compile", "--dir", os.path.join(SRC, "res"), "-o", os.path.join(OUT, "res.zip"))
    unsigned = os.path.join(OUT, "app.unsigned.apk")
    run(os.path.join(BT, "aapt2"), "link", "-I", ANDROID_JAR, "--manifest", os.path.join(SRC, "AndroidManifest.xml"),
        "-o", unsigned, os.path.join(OUT, "res.zip"), "--auto-add-overlay")

    srcs = glob.glob(os.path.join(SRC, "src", "**", "*.java"), recursive=True)
    run(os.path.join(JAVA_HOME, "bin", "javac"), "-encoding", "UTF-8", "-source", "8", "-target", "8", "-nowarn",
        "-Xlint:-options", "-bootclasspath", ANDROID_JAR, "-classpath", ANDROID_JAR,
        "-d", os.path.join(OUT, "classes"), *srcs, env=env)
    classes = glob.glob(os.path.join(OUT, "classes", "**", "*.class"), recursive=True)
    run(os.path.join(BT, "d8"), "--min-api", "21", "--release", "--lib", ANDROID_JAR,
        "--output", OUT, *classes, env=env)
    with zipfile.ZipFile(unsigned, "a", zipfile.ZIP_DEFLATED) as z:
        z.write(os.path.join(OUT, "classes.dex"), "classes.dex")

    aligned = os.path.join(OUT, "app.aligned.apk")
    run(os.path.join(BT, "zipalign"), "-f", "-p", "4", unsigned, aligned)

    ks = os.path.join(TOOLS, "release.keystore")   # 密钥别名沿用 t9eyes（改名前生成的，内部用，用户看不到）
    pw_file = os.path.join(TOOLS, "release.keystore.pass")
    if not os.path.exists(ks):
        pw = secrets.token_urlsafe(18)
        with open(pw_file, "w") as f:
            f.write(pw)
        os.chmod(pw_file, 0o600)
        run(os.path.join(JAVA_HOME, "bin", "keytool"), "-genkeypair", "-keystore", ks, "-alias", "t9eyes",
            "-keyalg", "RSA", "-keysize", "2048", "-validity", "10000", "-storepass", pw, "-keypass", pw,
            "-dname", "CN=Open Your Eyes, O=Open Your Eyes", env=env)
        print("已生成签名密钥：%s（请备份它和 %s）" % (ks, pw_file))
    pw = open(pw_file).read().strip()
    os.makedirs(WEB, exist_ok=True)
    apk = os.path.join(WEB, "openyoureyes.apk")
    run(os.path.join(BT, "apksigner"), "sign", "--v4-signing-enabled", "false", "--ks", ks, "--ks-key-alias", "t9eyes", "--ks-pass", "pass:" + pw,
        "--key-pass", "pass:" + pw, "--out", apk, aligned, env=env)
    with open(os.path.join(WEB, "version.json"), "w", encoding="utf-8") as f:
        json.dump({"versionName": vname, "versionCode": vcode, "size": os.path.getsize(apk), "url": "/app/openyoureyes.apk"}, f)
    print("OK", apk, os.path.getsize(apk), "bytes", vname)


if __name__ == "__main__":
    main()
