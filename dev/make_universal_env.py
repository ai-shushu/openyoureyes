#!/usr/bin/env python3
"""建一个打 Mac 通用安装包（Intel + Apple 芯片都能用）的环境：.venv-universal

pip 在 Apple 芯片的电脑上只会装 arm64 版的依赖，打出来的程序 Intel Mac 打不开。
这里把带编译模块的依赖（Pillow、cffi、psutil、cryptography）分别下载两种芯片的版本，合并成通用版再装。
cryptography 从 49 起不再提供 Intel Mac 版，所以通用包固定用 48.0.1（满足 requirements.txt 的 >=42）。

用法（只需运行一次，依赖升级时再运行）：
  .venv/bin/python dev/make_universal_env.py
  .venv-universal/bin/python dev/build_app.py      # 打出来的就是通用安装包
"""
import glob
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENV = os.path.join(ROOT, ".venv-universal")
WORK = os.path.join(ROOT, "build-universal")
PY = "/Library/Developer/CommandLineTools/usr/bin/python3"   # 苹果自带的 Python，本身就是通用版
PINS = ["pillow==11.3.0", "cffi==2.0.0", "cryptography==48.0.1", "psutil==7.2.2"]
X86 = ["macosx_%s_x86_64" % v for v in ("10_9", "10_10", "10_12", "10_13", "10_14", "10_15", "11_0", "12_0")]
ARM = ["macosx_11_0_arm64"]


def run(*args):
    print("+", " ".join(args))
    subprocess.check_call(args)


def download(pip, dest, plats):
    args = [pip, "download", "-q", "--only-binary=:all:", "--python-version", "3.9.6", "--implementation", "cp",
            "--abi", "cp39", "--abi", "abi3", "--no-deps", "-d", dest]
    for p in plats + ["macosx_10_9_universal2"]:
        args += ["--platform", p]
    run(*(args + PINS))


def main():
    if sys.platform != "darwin":
        sys.exit("只在 Mac 上用")
    shutil.rmtree(WORK, ignore_errors=True)
    shutil.rmtree(VENV, ignore_errors=True)
    run(PY, "-m", "venv", VENV)
    pip = os.path.join(VENV, "bin", "pip")
    run(pip, "install", "-q", "--upgrade", "pip", "delocate")
    arm, x86, fused = (os.path.join(WORK, d) for d in ("arm", "x86", "fused"))
    download(pip, arm, ARM)
    download(pip, x86, X86)
    os.makedirs(fused)
    for name in ("pillow", "cffi", "psutil", "cryptography"):
        a, x = glob.glob(os.path.join(arm, name + "-*.whl"))[0], glob.glob(os.path.join(x86, name + "-*.whl"))[0]
        if a.endswith("universal2.whl"):
            shutil.copy(a, fused)
        else:
            run(os.path.join(VENV, "bin", "delocate-merge"), a, x, "-w", fused)
    run(pip, "install", "-q", *glob.glob(os.path.join(fused, "*.whl")))
    run(pip, "install", "-q", "-r", os.path.join(ROOT, "requirements.txt"), "pyinstaller")
    # 检查：除了 Pillow 按芯片分开的两个 zlib，其它编译模块都必须是双架构
    bad = []
    for f in glob.glob(os.path.join(VENV, "lib", "python3*", "site-packages", "**", "*.so"), recursive=True):
        archs = subprocess.run(["lipo", "-archs", f], capture_output=True, text=True).stdout.split()
        if not {"x86_64", "arm64"} <= set(archs):
            bad.append(f)
    if bad:
        sys.exit("这些模块不是双架构：\n" + "\n".join(bad))
    print("OK：.venv-universal/bin/python dev/build_app.py 打通用安装包")


if __name__ == "__main__":
    main()
