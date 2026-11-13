#!/usr/bin/env python3
"""生成发给用户的两种 Claude 插件：
  dist/open-your-eyes.mcpb   Claude 桌面版扩展（双击安装）
  plugin/             Claude Code 插件（插件市场，本仓库即市场）——这里只做一致性检查

工具清单从 server/eyes_mcp.py 读取，写进扩展的 tools.json（Open Your Eyes 没开时也能列出工具）。
"""
import json
import os
import shutil
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))
import eyes_mcp  # noqa: E402
eyes_mcp.API = "http://127.0.0.1:1"   # 打包时不连真实程序：备份说明里不写死某台电脑的机位名

ext = os.path.join(ROOT, "desktop-extension")
with open(os.path.join(ext, "server", "tools.json"), "w", encoding="utf-8") as f:
    json.dump(eyes_mcp.tools_list(), f, ensure_ascii=False, indent=1)
with open(os.path.join(ext, "server", "instructions.txt"), "w", encoding="utf-8") as f:
    f.write(eyes_mcp.INSTRUCTIONS)
with open(os.path.join(ext, "manifest.json"), encoding="utf-8") as f:
    man = json.load(f)
man["version"] = eyes_mcp.VERSION
man["tools"] = [{"name": t["name"], "description": t["description"].split("：")[0].split("。")[0][:120]} for t in eyes_mcp.tools_list()]
with open(os.path.join(ext, "manifest.json"), "w", encoding="utf-8") as f:
    json.dump(man, f, ensure_ascii=False, indent=2)
    f.write("\n")

os.makedirs(os.path.join(ROOT, "dist"), exist_ok=True)
out = os.path.join(ROOT, "dist", "open-your-eyes.mcpb")
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for base, _, files in os.walk(ext):
        for n in files:
            fp = os.path.join(base, n)
            z.write(fp, os.path.relpath(fp, ext))
print("OK", out, os.path.getsize(out), "bytes")
os.makedirs(os.path.join(ROOT, "web", "app"), exist_ok=True)   # 电脑端自带一份，设置页「连接 AI」里直接下载
shutil.copy(out, os.path.join(ROOT, "web", "app", "open-your-eyes.mcpb"))

pp = os.path.join(ROOT, "plugin", ".claude-plugin", "plugin.json")
plug = json.load(open(pp, encoding="utf-8"))
plug["version"] = eyes_mcp.VERSION   # Claude Code 插件版本跟着走
with open(pp, "w", encoding="utf-8") as f:
    json.dump(plug, f, ensure_ascii=False, indent=2)
    f.write("\n")
