#!/usr/bin/env python3
"""生成 Open Your Eyes 的图标（橙色底 + 一对白眼睛），一次输出所有用到的尺寸：

  dev/logo-source.png     原始 logo（方形满版大图，其它都从它来）
  web/icon.svg            网页里用（设置页标题、浏览器标签）：按原图画的矢量版，小尺寸也清楚
  web/icon-192.png        手机「添加到主屏幕」、Windows 程序图标、菜单栏图标：方形满版（系统自己会切圆角）
  web/icon-1024.png       打安装包时生成 Mac .icns：按 macOS 图标规范留边 + 圆角
  android/res/mipmap-*/   安卓 App 图标：圆角方形
  desktop-extension/icon.png  Claude 桌面版扩展列表里的图标：圆角方形

用法：.venv/bin/python dev/make_icon.py [新的 logo.png]
  给了新图片就先把它存成 dev/logo-source.png 再生成。
"""
import os
import shutil
import sys

from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(ROOT, "dev", "logo-source.png")

# 矢量版：按原图量出来的位置（512 × 512 坐标）
SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
  <rect width="512" height="512" rx="112" fill="#d06e4f"/>
  <ellipse cx="175.5" cy="257.5" rx="60.5" ry="81.5" fill="#f9f7f3"/>
  <ellipse cx="334.5" cy="258" rx="60.5" ry="81" fill="#f9f7f3"/>
  <ellipse cx="182" cy="231" rx="24.5" ry="27" fill="#23272d"/>
  <ellipse cx="338.5" cy="231" rx="24.5" ry="27" fill="#23272d"/>
</svg>
"""


def square(size):
    """方形满版。"""
    return Image.open(SOURCE).convert("RGBA").resize((size, size), Image.LANCZOS)


def rounded(size, margin=0.0, radius=0.22):
    """圆角方形：margin = 四周留白占比（macOS 规范约 10%），radius = 圆角占图标边长的比例。先画 4 倍大再缩小，边缘平滑。"""
    big = size * 4
    inner = round(big * (1 - 2 * margin))
    off = (big - inner) // 2
    art = Image.open(SOURCE).convert("RGBA").resize((inner, inner), Image.LANCZOS)
    mask = Image.new("L", (inner, inner), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, inner - 1, inner - 1], radius=round(inner * radius), fill=255)
    out = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    out.paste(art, (off, off), mask)
    return out.resize((size, size), Image.LANCZOS)


def main():
    if len(sys.argv) > 1:
        shutil.copy(sys.argv[1], SOURCE)
    with open(os.path.join(ROOT, "web", "icon.svg"), "w", encoding="utf-8") as f:
        f.write(SVG)
    square(192).save(os.path.join(ROOT, "web", "icon-192.png"))
    rounded(1024, margin=0.1, radius=0.225).save(os.path.join(ROOT, "web", "icon-1024.png"))
    rounded(512).save(os.path.join(ROOT, "desktop-extension", "icon.png"))
    for folder, px in (("mipmap-mdpi", 48), ("mipmap-hdpi", 72), ("mipmap-xhdpi", 96), ("mipmap-xxhdpi", 144), ("mipmap-xxxhdpi", 192)):
        p = os.path.join(ROOT, "android", "res", folder)
        os.makedirs(p, exist_ok=True)
        rounded(px).save(os.path.join(p, "ic_launcher.png"))
    print("OK")


if __name__ == "__main__":
    main()
