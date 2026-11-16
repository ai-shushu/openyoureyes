"""连接 AI：检测电脑上装了哪些 AI，并一键把 Open Your Eyes 接进去（用户不用碰终端、不用改配置文件）。

目前支持：Claude 桌面版、Claude Code、Codex、WorkBuddy。它们最后都连到同一个本机接口 http://127.0.0.1:<端口>/mcp，
但都不直接填这个网址，而是由 AI 启动一个本地小程序（标准输入输出）再转过来——
AI 自己连 HTTP 时会跟随系统代理（Shadowrocket、Clash 等），连 127.0.0.1 也被送进代理而失败；
本地小程序明确不走代理，用户开不开代理都一样能用。
  Claude 桌面版  用它打开扩展文件 open-your-eyes.mcpb，弹出安装界面，用户点「安装」（扩展本身就是这样的小程序）
  Claude Code    替用户运行 claude mcp add-json（用户级，所有项目都能用），启动 Open Your Eyes --mcp-bridge
  Codex          在 ~/.codex/config.toml 里加一条，启动 Open Your Eyes --mcp-bridge（命令行 / 桌面版 / 编辑器插件共用）
  WorkBuddy      在 ~/.workbuddy/mcp.json 里加一条（改之前先备份），启动 Open Your Eyes --mcp-bridge；
                 新服务要在 WorkBuddy 里点一次「信任」
拿不到可靠的程序路径时（Windows 安装版、还在 dmg 里运行），退回直接填网址。
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


# Claude 桌面版扩展里的程序（desktop-extension/server/）最近一次改动是在这个版本。
# 用户装的扩展比它旧，就在「连接 AI」里提示「更新」（扩展不会跟着电脑程序自动更新，要用户在 Claude 里再点一次「安装」）。
# 改了 desktop-extension/server/ 里的东西，记得把这里改成当次的版本号。
EXT_MIN_VERSION = "0.7.15"


def _ver(v):
    try:
        return tuple(int(x) for x in str(v).split(".")[:3])
    except ValueError:
        return (0,)


def claude_desktop_ext_version():
    """Claude 桌面版里装的 Open Your Eyes 扩展是哪个版本；没装或读不到返回 None。"""
    base = os.path.join(HOME, "Library", "Application Support", "Claude") if MAC else \
        os.path.join(os.environ.get("APPDATA", ""), "Claude")
    root = os.path.join(base, "Claude Extensions")
    try:
        dirs = os.listdir(root)
    except OSError:
        return None
    for d in dirs:
        m = _read_json(os.path.join(root, d, "manifest.json"))
        if m and m.get("name") == NAME:
            return str(m.get("version") or "0")
    return None


def _claude_code_entry_now():
    """Claude Code 里我们那一条现在的样子（用户级）；没有返回 None。"""
    cfg = os.environ.get("CLAUDE_CONFIG_DIR")
    for path in ([os.path.join(cfg, ".claude.json")] if cfg else []) + [os.path.join(HOME, ".claude.json")]:
        d = _read_json(path)
        if d is not None:
            return (d.get("mcpServers") or {}).get(NAME)
    return None


def _claude_code_installed():
    return _claude_code_entry_now() is not None


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


def bridge_command():
    """Codex 用的本地桥接怎么启动：(程序, 参数)。拿不到可靠的路径时返回 None（退回直接填网址）。"""
    exe = os.path.abspath(sys.executable)
    if getattr(sys, "frozen", False):
        if WIN:   # Windows 安装版是窗口程序，没有标准输入输出可用
            return None
        if "/AppTranslocation/" in exe or exe.startswith("/Volumes/"):   # 还在 dmg 里 / 被系统临时搬走：路径会变
            return None
        return exe, ["--mcp-bridge"]
    script = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app", "openyoureyes.py"))
    return exe, [script, "--mcp-bridge"]


def _bridge_entry(url):
    """JSON 格式的一条（Claude Code、WorkBuddy 用）：能用桥接就用桥接，否则填网址。"""
    bc = bridge_command()
    if not bc:
        return {"type": "http", "url": url}
    return {"type": "stdio", "command": bc[0], "args": list(bc[1]), "env": {}}


def _is_current_bridge(entry):
    """这一条已经是当前程序位置的桥接了吗。"""
    bc = bridge_command()
    return bool(bc and isinstance(entry, dict) and entry.get("command") == bc[0]
                and "--mcp-bridge" in (entry.get("args") or []))


def _codex_entry(url):
    bc = bridge_command()
    if not bc:
        return '[mcp_servers.%s]\nurl = %s\n' % (NAME, json.dumps(url))
    cmd, args = bc
    return ('[mcp_servers.%s]\ncommand = %s\nargs = [%s]\nstartup_timeout_sec = 30\n'
            % (NAME, json.dumps(cmd, ensure_ascii=False), ", ".join(json.dumps(a, ensure_ascii=False) for a in args)))


_CODEX_SECTION = r'(?ms)^\[mcp_servers\.("?)(%s)\1\]\n.*?(?=^\[|\Z)'


def _write_codex_entry(url):
    """在 Codex 设置里写我们这一条（原来的一条换掉；其它内容不动；改之前先备份）。"""
    path = _codex_config()
    text = ""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            text = f.read()
        shutil.copy(path, path + ".bak-openyoureyes")
    text = re.sub(_CODEX_SECTION % re.escape(NAME), "", text)
    text = text.rstrip() + "\n\n" + _codex_entry(url)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp-openyoureyes"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text.lstrip())
    os.replace(tmp, path)


def repair_codex(url):
    """已接入 Codex 但配置是旧的（直接填网址、或程序换了位置）：改成当前的桥接。改了返回 True。"""
    try:
        with open(_codex_config(), encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return False
    m = re.search(_CODEX_SECTION % re.escape(NAME), text)
    bc = bridge_command()
    if not m or bc is None:   # 没接入，或这台电脑用不了桥接：不动
        return False
    want = "command = %s" % json.dumps(bc[0], ensure_ascii=False)
    if want in m.group(0) and "--mcp-bridge" in m.group(0):   # 已经是当前的桥接（Codex 自己加的其它设置保留）
        return False
    _write_codex_entry(url)
    return True


def repair_claude_code(url):
    """已接入 Claude Code 但还是直接填网址（或程序换了位置）：改成当前的桥接。改了返回 True。"""
    entry = _claude_code_entry_now()
    if entry is None or bridge_command() is None or _is_current_bridge(entry):
        return False
    ok, _msg = install("claude_code", url)
    return ok


def repair_workbuddy(url):
    """同上，WorkBuddy。只换连接方式，名字不变（WorkBuddy 的「信任」按名字记）。改了返回 True。"""
    d = _workbuddy_dir()
    path = os.path.join(d, "mcp.json") if d else None
    j = _read_json(path) if path else None
    entry = ((j or {}).get("mcpServers") or {}).get(NAME)
    if entry is None or bridge_command() is None or _is_current_bridge(entry):
        return False
    ok, _msg = install("workbuddy", url)
    return ok


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
        row = {"id": aid, "name": name, "found": found, "installed": installed}
        if aid == "claude_desktop" and installed:
            try:
                v = claude_desktop_ext_version()
            except Exception:
                v = None
            if v and _ver(v) < _ver(EXT_MIN_VERSION):
                row["extVersion"], row["outdated"] = v, True
        out.append(row)
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
        _run([exe, "mcp", "remove", "--scope", "user", NAME])
        code, out = _run([exe, "mcp", "add-json", "--scope", "user", NAME, json.dumps(_bridge_entry(url))])
        if code != 0:   # 很老的 Claude Code 没有 add-json：退回直接填网址
            code, out = _run([exe, "mcp", "add", "--scope", "user", "--transport", "http", NAME, url])
        return (code == 0, "已装好。新开一个 Claude Code 对话，说「睁眼，看看……」试试" if code == 0 else "安装失败：%s" % out[-200:])

    if aid == "codex":
        try:
            _write_codex_entry(url)
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
            b = _bridge_entry(url)
            if b["type"] == "stdio":   # 保留用户在 WorkBuddy 里改过的其它设置（开关等），只换连接方式
                old = servers.get(NAME) if isinstance(servers.get(NAME), dict) else {}
                e = {k: v for k, v in old.items() if k not in ("url", "type", "headers", "transport")}
                e.update(command=b["command"], args=b["args"])
            else:
                e = {"url": url}
            servers[NAME] = e
            os.makedirs(d, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(j, f, ensure_ascii=False, indent=2)
            return True, "已装好：打开 WorkBuddy 的「连接 → 自定义连接」，在 Open Your Eyes 上点「信任」并打开开关"
        except OSError as exc:
            return False, "写入 WorkBuddy 设置失败：%s" % exc

    return False, "不支持的 AI"
