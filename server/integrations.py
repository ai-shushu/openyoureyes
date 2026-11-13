"""连接 AI：检测电脑上装了哪些 AI，并一键把 Open Your Eyes 接进去（用户不用碰终端、不用改配置文件）。

目前支持：Claude 桌面版、Claude Code、Codex、WorkBuddy。它们最后都连到同一个本机接口 http://127.0.0.1:<端口>/mcp，
区别只在「怎么装进去」：
  Claude 桌面版  用它打开扩展文件 open-your-eyes.mcpb，弹出安装界面，用户点「安装」
  Claude Code    替用户运行 claude mcp add（用户级，所有项目都能用）
  Codex          替用户运行 codex mcp add --url（写进 ~/.codex/config.toml，命令行 / 桌面版 / 编辑器插件共用）
  WorkBuddy      在 ~/.workbuddy/mcp.json 里加一条（改之前先备份）；新服务要在 WorkBuddy 里点一次「信任」
"""
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

NAME = "open-your-eyes"
OLD_NAMES = ("agenteye", "t9-eyes")   # 改名前装过的，一并清掉，免得出现两份
HOME = os.path.expanduser("~")
MAC = sys.platform == "darwin"
WIN = os.name == "nt"


def _which(cmd):
    """找命令行程序：开机自启时 PATH 很短，常见安装位置都看一遍。"""
    hit = shutil.which(cmd)
    if hit:
        return hit
    cands = [os.path.join(HOME, ".local", "bin", cmd), os.path.join(HOME, ".claude", "local", cmd),
             "/opt/homebrew/bin/" + cmd, "/usr/local/bin/" + cmd, os.path.join(HOME, ".npm-global", "bin", cmd),
             os.path.join(HOME, ".bun", "bin", cmd), os.path.join(HOME, ".volta", "bin", cmd)]
    cands += glob.glob(os.path.join(HOME, ".nvm", "versions", "node", "*", "bin", cmd))
    if WIN:
        appdata = os.environ.get("APPDATA", "")
        cands += [os.path.join(appdata, "npm", cmd + ".cmd"), os.path.join(HOME, ".local", "bin", cmd + ".exe")]
    for c in cands:
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def _run(args, timeout=30):
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join([os.path.dirname(args[0]), env.get("PATH", ""), "/opt/homebrew/bin", "/usr/local/bin"])
    p = subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=env)
    return p.returncode, (p.stdout + p.stderr).strip()


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------- 各个 AI

def _claude_desktop_app():
    if MAC:
        for p in ("/Applications/Claude.app", os.path.join(HOME, "Applications", "Claude.app")):
            if os.path.isdir(p):
                return p
    elif WIN:
        la = os.environ.get("LOCALAPPDATA", "")
        for p in (os.path.join(la, "AnthropicClaude"), os.path.join(la, "Programs", "Claude")):
            if os.path.isdir(p):
                return p
    return None


def _claude_desktop_installed():
    base = os.path.join(HOME, "Library", "Application Support", "Claude") if MAC else \
        os.path.join(os.environ.get("APPDATA", ""), "Claude")
    try:
        with open(os.path.join(base, "extensions-installations.json"), encoding="utf-8") as f:
            return NAME in f.read()
    except OSError:
        return False


def _claude_code_installed():
    cfg = os.environ.get("CLAUDE_CONFIG_DIR")
    for path in ([os.path.join(cfg, ".claude.json")] if cfg else []) + [os.path.join(HOME, ".claude.json")]:
        d = _read_json(path)
        if d is not None:
            return NAME in (d.get("mcpServers") or {})
    return False


def _codex_home():
    return os.environ.get("CODEX_HOME") or os.path.join(HOME, ".codex")


def _codex_config():
    return os.path.join(_codex_home(), "config.toml")


# Codex 每个对话都会读 ~/.codex/AGENTS.md，而且不依赖 MCP 有没有连上：
# 万一某个对话没连上 Open Your Eyes（比如程序正在重启），它也会直接告诉用户，而不是去截屏冒充
AGENTS_BEGIN, AGENTS_END = "<!-- open-your-eyes:begin -->", "<!-- open-your-eyes:end -->"
AGENTS_BLOCK = AGENTS_BEGIN + """
## Open Your Eyes（手机眼睛）
- 用户说「睁眼」时，用 open-your-eyes 的 look 工具让工作台上的手机拍照来看。
- 如果 look 工具不可用或手机不在线，直接告诉用户「Open Your Eyes 没连上（或手机不在线），请确认电脑上的 Open Your Eyes 在运行后新开一个对话」；不要用截屏、Computer Use、浏览器看 Open Your Eyes 的窗口或电脑屏幕来代替。
""" + AGENTS_END + "\n"


def write_codex_agents():
    """把上面这段写进 Codex 的全局说明（已有就只替换这一段，其它内容不动；改之前先备份）。"""
    path = os.path.join(_codex_home(), "AGENTS.md")
    text = ""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            text = f.read()
        shutil.copy(path, path + ".bak-openyoureyes")
    if AGENTS_BEGIN in text and AGENTS_END in text:
        i, j = text.index(AGENTS_BEGIN), text.index(AGENTS_END) + len(AGENTS_END)
        text = text[:i] + AGENTS_BLOCK.rstrip("\n") + text[j:]
    else:
        text = (text.rstrip() + "\n\n" if text.strip() else "") + AGENTS_BLOCK
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def _codex_installed():
    try:
        with open(_codex_config(), encoding="utf-8") as f:
            return re.search(r'^\[mcp_servers\.("?)%s\1\]' % re.escape(NAME), f.read(), re.M) is not None
    except OSError:
        return False


def _workbuddy_dir():
    for d in (".workbuddy", ".workbuddy-ai"):
        p = os.path.join(HOME, d)
        if os.path.isdir(p):
            return p
    return None


def _workbuddy_app():
    if MAC:
        for p in glob.glob("/Applications/WorkBuddy*.app") + glob.glob(os.path.join(HOME, "Applications", "WorkBuddy*.app")):
            return p
    return _workbuddy_dir()


def _workbuddy_installed():
    d = _workbuddy_dir()
    j = _read_json(os.path.join(d, "mcp.json")) if d else None
    return bool(j) and NAME in (j.get("mcpServers") or {})


AIS = [
    # id, 名字, 怎么检测有没有装这个 AI, 怎么检测已经接好
    ("claude_desktop", "Claude 桌面版", _claude_desktop_app, _claude_desktop_installed),
    ("claude_code", "Claude Code", lambda: _which("claude"), _claude_code_installed),
    ("codex", "Codex", lambda: _which("codex") or (os.path.isdir(os.path.join(HOME, ".codex")) and "config"), _codex_installed),
    ("workbuddy", "WorkBuddy", _workbuddy_app, _workbuddy_installed),
]


def status():
    """[{id, name, found, installed}]：found=电脑上有这个 AI；installed=已经把 Open Your Eyes 接进去了。"""
    out = []
    for aid, name, find, inst in AIS:
        try:
            found = bool(find())
        except Exception:
            found = False
        try:
            installed = bool(inst()) if found else False
        except Exception:
            installed = False
        out.append({"id": aid, "name": name, "found": found, "installed": installed})
    return out


def install(aid, url, mcpb_path=None):
    """一键接入。返回 (成功?, 给用户看的一句话)。"""
    if aid == "claude_desktop":
        if not mcpb_path or not os.path.exists(mcpb_path):
            return False, "电脑端缺少扩展文件，请重新安装 Open Your Eyes"
        tmp = os.path.join(tempfile.gettempdir(), "OpenYourEyes.mcpb")
        shutil.copy(mcpb_path, tmp)
        if MAC:
            subprocess.Popen(["open", "-a", _claude_desktop_app() or "Claude", tmp])
        elif WIN:
            os.startfile(tmp)
        else:
            subprocess.Popen(["xdg-open", tmp])
        return True, "已在 Claude 桌面版里打开安装界面，点「安装」；装好后新开对话，说「睁眼，看看……」试试"

    if aid == "claude_code":
        exe = _which("claude")
        if not exe:
            return False, "没找到 Claude Code"
        for n in (NAME,) + OLD_NAMES:
            _run([exe, "mcp", "remove", "--scope", "user", n])
        code, out = _run([exe, "mcp", "add", "--scope", "user", "--transport", "http", NAME, url])
        return (code == 0, "已装好。新开一个 Claude Code 对话，说「睁眼，看看……」试试" if code == 0 else "安装失败：%s" % out[-200:])

    if aid == "codex":
        exe = _which("codex")
        if exe:
            for n in (NAME,) + OLD_NAMES:
                _run([exe, "mcp", "remove", n])
            code, out = _run([exe, "mcp", "add", NAME, "--url", url])
            if code == 0:
                write_codex_agents()
                return True, "已装好。新开一个 Codex 对话，说「睁眼，看看……」试试"
            return False, "安装失败：%s" % out[-200:]
        # 只有 Codex 桌面版、没有命令行：直接写配置文件（先备份）
        path = _codex_config()
        try:
            text = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
            if os.path.exists(path):
                shutil.copy(path, path + ".bak-openyoureyes")
            text = re.sub(r'(?ms)^\[mcp_servers\."?%s"?\]\n.*?(?=^\[|\Z)' % re.escape(NAME), "", text)
            text = text.rstrip() + '\n\n[mcp_servers.%s]\nurl = "%s"\n' % (NAME, url)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text.lstrip())
            write_codex_agents()
            return True, "已装好。新开一个 Codex 对话，说「睁眼，看看……」试试"
        except OSError as exc:
            return False, "写入 Codex 设置失败：%s" % exc

    if aid == "workbuddy":
        d = _workbuddy_dir() or os.path.join(HOME, ".workbuddy")
        path = os.path.join(d, "mcp.json")
        try:
            j = _read_json(path) if os.path.exists(path) else {}
            if j is None:
                return False, "WorkBuddy 的设置文件格式不对，没有改动它"
            if os.path.exists(path):
                shutil.copy(path, path + ".bak-openyoureyes")
            servers = j.setdefault("mcpServers", {})
            for n in OLD_NAMES:
                servers.pop(n, None)
            servers[NAME] = {"url": url}
            os.makedirs(d, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(j, f, ensure_ascii=False, indent=2)
            return True, "已装好：打开 WorkBuddy 的「连接 → 自定义连接」，在 Open Your Eyes 上点「信任」并打开开关"
        except OSError as exc:
            return False, "写入 WorkBuddy 设置失败：%s" % exc

    return False, "不支持的 AI"
