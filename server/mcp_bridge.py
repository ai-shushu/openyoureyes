"""Codex 用的本地桥接：Codex 通过标准输入输出跟它说话，它再把消息转给本机的 Open Your Eyes（http://127.0.0.1:39757/mcp）。

为什么需要它：Codex 直接连 HTTP 时会走 macOS 的系统代理（Shadowrocket、Clash 等），
发往 127.0.0.1 的请求也被送进代理，结果一直连不上；Codex 桌面版又不带 NO_PROXY，用户自己也改不了。
桥接自己连本机、明确不走任何代理，所以不受代理软件影响。

顺带：
- Open Your Eyes 正在重启（比如刚更新）：等它回来再转发，不用新开对话。
- Open Your Eyes 没开：自动把它打开（只限安装版）；还是连不上就用大白话告诉 AI。

用法（由 Codex 启动，用户不用管）：Open Your Eyes --mcp-bridge
"""
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

PORT = int(os.environ.get("T9_PORT") or os.environ.get("T9_WEB_PORT") or 39757)
URL = os.environ.get("T9_EYES_MCP") or "http://127.0.0.1:%d/mcp" % PORT
OFFLINE = "Open Your Eyes 没有运行：请让用户在电脑上打开 Open Your Eyes 程序（打开后会出现在菜单栏 / 任务栏），再试一次。"
FAILED = "Open Your Eyes 开着，但这次没有回应（%s）：请再试一次；还不行就让用户退出并重新打开 Open Your Eyes。"
WAIT_UP_S = float(os.environ.get("T9_BRIDGE_WAIT_S") or 20)          # 连不上时最多等多久（程序重启、刚被打开）
CALL_TIMEOUT_S = 330    # 一次请求最多等多久（拍照要等用户取景，最长 5 分钟）

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # 不走任何代理
_out_lock = threading.Lock()
_sess = {"sid": None, "init": None, "ua": "open-your-eyes-bridge"}
_sess_lock = threading.Lock()
_launched = [0.0]


class Answered(Exception):
    """程序开着、收到了这条消息，但出错了或等太久：不能当作「没开」，也不自动重发（拍照请求重发会多拍一次）。"""


def _out(msg):
    with _out_lock:
        sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def _post(msg, timeout=CALL_TIMEOUT_S):
    """转发一条消息。返回 (回复或 None, 新的会话编号或 None)。连不上抛 OSError。"""
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
               "User-Agent": _sess["ua"]}
    if _sess["sid"]:
        headers["Mcp-Session-Id"] = _sess["sid"]
    req = urllib.request.Request(URL, data=json.dumps(msg).encode("utf-8"), headers=headers, method="POST")
    try:
        with _opener.open(req, timeout=timeout) as r:
            sid = r.headers.get("Mcp-Session-Id")
            body = r.read()
    except urllib.error.HTTPError as e:
        raise Answered("HTTP %d" % e.code)
    except (TimeoutError, socket.timeout):   # 连上了、发出去了，等回复等太久
        raise Answered("等了太久")
    if sid:
        _sess["sid"] = sid
    if not body.strip():
        return None
    return json.loads(body.decode("utf-8"))


def _alive():
    try:
        with _opener.open("http://127.0.0.1:%d/api/status" % PORT, timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def _launch_app():
    """安装版：把 Open Your Eyes 打开（不弹窗口）。每分钟最多试一次。"""
    if not getattr(sys, "frozen", False) or time.time() - _launched[0] < 60:
        return
    _launched[0] = time.time()
    try:
        if sys.platform == "darwin":
            app = os.path.abspath(os.path.join(os.path.dirname(sys.executable), "..", ".."))
            if app.endswith(".app"):
                subprocess.Popen(["open", "-g", "-a", app, "--args", "--no-browser"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
        subprocess.Popen([sys.executable, "--no-browser"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         close_fds=True)
    except Exception:
        pass


def _wait_up(seconds):
    """等 Open Your Eyes 能连上（必要时把它打开）。"""
    deadline = time.time() + seconds
    tried_launch = False
    while time.time() < deadline:
        if _alive():
            return True
        if not tried_launch and time.time() > deadline - seconds + 3:   # 等了 3 秒还没回来：可能根本没开
            tried_launch = True
            _launch_app()
        time.sleep(0.5)
    return _alive()


def _ensure_session():
    """会话没了（程序重启过，或桥接刚连上）：用 Codex 当初的初始化参数重新建一个。"""
    with _sess_lock:
        if _sess["sid"] or not _sess["init"]:
            return
        _post({"jsonrpc": "2.0", "id": "bridge-init", "method": "initialize", "params": _sess["init"]}, timeout=10)
        _post({"jsonrpc": "2.0", "method": "notifications/initialized"}, timeout=10)


def _offline_reply(msg, why=None):
    import eyes_mcp
    mid, method = msg.get("id"), msg.get("method")
    text = FAILED % why if why else OFFLINE
    if method == "initialize":
        p = msg.get("params") or {}
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": p.get("protocolVersion") or "2025-06-18",
            "capabilities": {"tools": {}, "prompts": {}},
            "instructions": eyes_mcp.INSTRUCTIONS,
            "serverInfo": {"name": "open-your-eyes", "version": "bridge"}}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": eyes_mcp.TOOLS}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/call":
        return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": text}], "isError": True}}
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": text}}


def handle(msg):
    mid, method = msg.get("id"), msg.get("method")
    if method == "initialize":
        p = msg.get("params") or {}
        _sess["init"] = p
        ci = p.get("clientInfo") or {}
        _sess["ua"] = "%s/bridge" % (str(ci.get("name") or "codex")[:40] or "codex")
        _sess["sid"] = None
        for wait in (0, min(15.0, WAIT_UP_S)):   # Codex 启动 MCP 有时限：最多等几秒，等不到就先本地应答，之后的调用再连
            try:
                if wait and not _wait_up(wait):
                    break
                r = _post(msg, timeout=10)
                if r is not None:
                    return _out(r)
            except (OSError, ValueError, Answered):
                _sess["sid"] = None
        return _out(_offline_reply(msg))
    if mid is None:   # 通知
        try:
            _ensure_session()
            _post(msg, timeout=10)
        except (OSError, ValueError, Answered):
            pass
        return
    for attempt in range(2):
        try:
            _ensure_session()
            r = _post(msg)
            if r is not None:
                return _out(r)
            return
        except Answered as e:
            return _out(_offline_reply(msg, str(e)))
        except ValueError:
            return _out(_offline_reply(msg, "回复看不懂"))
        except OSError:
            _sess["sid"] = None   # 下次重新建立会话
            if attempt == 0 and _wait_up(WAIT_UP_S):   # 程序在重启 / 刚被打开：等它回来再试一次
                continue
            break
    _out(_offline_reply(msg))


def main():
    seq = threading.Lock()   # 普通消息按顺序处理；拍照可能要等几十秒，单独开线程，不挡住别的消息
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if not isinstance(msg, dict):
            continue
        if msg.get("method") == "tools/call":
            threading.Thread(target=handle, args=(msg,), daemon=True).start()
        else:
            with seq:
                handle(msg)


if __name__ == "__main__":
    main()
