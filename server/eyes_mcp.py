#!/usr/bin/env python3
"""Open Your Eyes 本地 MCP 服务（stdio，仅用 Python 标准库）。

让 Claude 通过工作台上的手机「看」东西：
  look(what, phone)  Claude 请求看某样东西。用户在电脑上允许这次工具调用 = 同意拍照，
                     所有在线手机（或指定的一台）同时拍，按机位返回照片 + 一行拍摄角度。
  look_clip(...)     同上，在一段时间内连拍几帧。
  zoom(x, y, w, h)   放大本对话最近一次 look 的照片局部（从全尺寸原图裁剪），多机位时用 phone 指定。
  eyes_status()      各台手机是否在线。

手机只拍 Claude 请求的照片，照片作为这次工具调用的结果原路返回，所以天然只进入发起请求的那个对话。
每个对话各自启动一个本程序，zoom 只认本对话自己拍过的照片，不会放大到别的对话的照片。

依赖本机运行的页面服务（server/serve.py，默认 http://127.0.0.1:39757）。全部在本机与局域网内完成，不经过任何云服务。
"""
import base64
import json
import os
import sys
import threading
import time
import urllib.request

API = os.environ.get("T9_EYES_API", "http://127.0.0.1:39757")
PROTOCOL = "2025-06-18"
_np = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # 本机请求绕开系统代理


def api(path, body=None, timeout=5):
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(API + path, data=data, headers={"Content-Type": "application/json", "X-T9": "1"})
    with _np.open(req, timeout=timeout) as r:
        return json.load(r)


# ---------------- 照片 → MCP 内容（只有图片 + 一行拍摄角度） ----------------
def pose_line(meta):
    """与手机屏幕上显示的姿态文字一致，不加任何解释。"""
    p = meta.get("pose") or {}
    if not p:
        return None
    if p.get("mode") == "平视":
        return "平视 · 俯仰 %s° · 横滚 %s°" % (p.get("tiltFromLevelDeg"), p.get("horizonRollDeg"))
    return "%s · 偏离垂直 %s°（前后 %s° / 左右 %s°）" % (
        p.get("mode"), p.get("errDeg"), p.get("pitchFrontBackDeg"), p.get("rollLeftRightDeg"))


def _img(path):
    with open(path, "rb") as f:
        return {"type": "image", "data": base64.b64encode(f.read()).decode("ascii"), "mimeType": "image/jpeg"}


def _photo_links(snap_dir, name):
    try:
        with open(os.path.join(snap_dir, name + ".json"), encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, ValueError):
        meta = {}
    if meta.get("kind") == "clip":
        return " ".join("%s/photo/%s.f%d.jpg" % (API, name, i) for i in range(1, (meta.get("frames") or 0) + 1))
    return "%s/photo/%s.jpg" % (API, name)


def photo_content(snap_dir, name):
    # 用户要求：只返回图片 + 拍摄角度（与手机屏幕显示一致），不附任何解释；其余拍摄数据保存在同名 .json 里
    try:
        with open(os.path.join(snap_dir, name + ".json"), encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, ValueError):
        meta = {}
    if meta.get("kind") == "clip":
        out = [_img(os.path.join(snap_dir, "%s.f%d.jpg" % (name, i))) for i in range(1, (meta.get("frames") or 0) + 1)
               if os.path.exists(os.path.join(snap_dir, "%s.f%d.jpg" % (name, i)))]
    else:
        view = os.path.join(snap_dir, name + ".view.jpg")   # 全尺寸原图太大，给概览图；细节用 zoom 从原图取
        out = [_img(view if os.path.exists(view) else os.path.join(snap_dir, name + ".jpg"))]
    line = pose_line(meta)
    if line:
        out.append({"type": "text", "text": line})
    return out


# 本对话最近一次 look 的照片：[(机位名, 照片名)]，zoom 只对它们操作。
# stdio 方式每个对话一个进程，用模块里这一份；HTTP 方式按会话各存一份（见 respond 的 state 参数）
_ctx = threading.local()
_stdio_state = {"last": []}


def _state():
    return getattr(_ctx, "state", None) or _stdio_state


def text(t, error=False):
    return {"content": [{"type": "text", "text": t}], "isError": error}


# ---------------- 工具实现 ----------------
ADD_HINT = "还没有添加手机：请让用户在电脑上打开 Open Your Eyes，点「＋ 添加机位」，用手机扫二维码，按手机上的提示操作（约 1 分钟）。"


def _offline_hint(phones=None):
    """手机不在线时，告诉用户该做什么（网页版和 App 不一样）。"""
    if not phones:
        return ADD_HINT
    if any(p.get("app") for p in phones) and not any(not p.get("app") for p in phones):
        return "请让用户打开手机上的 Open Your Eyes App（点「开始待命」）。"
    return "请让用户在手机上打开拍照页并保持在前台（手机屏幕要亮着）；装了 Open Your Eyes App 的安卓手机，打开 App 即可。"


def tool_status(_args):
    try:
        st = api("/api/status")
    except Exception as exc:
        return text("Open Your Eyes 没有运行（%s）。请让用户在电脑上打开 Open Your Eyes 程序。" % exc, True)
    phones = st["phones"]
    if not phones:
        return text("Open Your Eyes %s\n" % VERSION + ADD_HINT)
    lines = []
    for ph in phones:
        if ph["online"]:
            st = ph.get("state")
            state = {"ready": "在线，待命", "busy": "在线，正在拍照", "camera": "在线，摄像头启动中",
                     "hidden": "在线，⚠ 拍照页在后台（手机锁屏或切到了别的应用），请用户把拍照页切回前台"}.get(
                st, "在线，待命" if ph["visible"] else "在线，摄像头未就绪")
        else:
            state = "离线（%s 秒前在线）" % int(ph["lastSeenAgoS"]) if ph.get("lastSeenAgoS") is not None else "离线"
        cam = {"user": "前置", "environment": "后置"}.get(ph.get("facing"))
        if cam:
            light = "，有补光灯" if ph.get("torch") else ("，屏幕补光" if ph.get("facing") == "user" else "，无补光灯")
            state += "（%s摄像头%s）" % (cam, light)
        kind = "App" if ph.get("app") else "网页"
        lines.append("%s（%s %s）：%s" % (ph["name"], ph.get("device") or "未知设备", kind, state))
    return text("Open Your Eyes %s\n" % VERSION + "\n".join(lines))


GRACE_S = 6   # 秒：有机位交回照片后，其余机位最多再等这么久，不让一台卡住整个请求


def _capture(what, kind="photo", seconds=None, frames=None, phone=None, light=None):
    what = (what or "").strip() or "当前画面"
    try:
        st = api("/api/status")
    except Exception as exc:
        return text("Open Your Eyes 没有运行（%s）。请让用户在电脑上打开 Open Your Eyes 程序。" % exc, True)
    res = api("/api/request", {"what": what, "kind": kind, "seconds": seconds, "frames": frames, "phone": phone, "light": light})
    recent = [p for p in st.get("phones") or [] if not p.get("online") and (p.get("lastSeenAgoS") or 9e9) < 180]
    if not res.get("ok") and recent and not any(p.get("online") for p in st.get("phones") or []):
        # 手机刚才还在线（多半是电脑端刚重启、手机正在自动重连）：等最多 10 秒再试，不要马上失败
        end = time.time() + 10
        while time.time() < end and not res.get("ok"):
            time.sleep(1)
            try:
                st = api("/api/status")
                if any(p.get("online") for p in st.get("phones") or []):
                    res = api("/api/request", {"what": what, "kind": kind, "seconds": seconds, "frames": frames, "phone": phone, "light": light})
            except Exception:
                pass
    if not res.get("ok"):
        return text("没法拍：%s。%s" % (res.get("error"), _offline_hint(st.get("phones"))), True)
    rid = res["id"]
    start = time.time()
    deadline = start + 30 + (seconds or 0)
    first_done = None
    r = {}
    while True:
        r = api("/api/request?id=%s&wait=1" % rid, timeout=10)
        now = time.time()
        if r.get("state") != "open":
            break
        if first_done is None and any(x["state"] == "done" for x in r["shots"]):
            first_done = now
        if now >= deadline or (first_done and now - first_done >= GRACE_S + (seconds or 0)):
            break
    try:
        api("/api/request/cancel", {"id": rid})
        r = api("/api/request?id=%s&wait=0" % rid)
    except Exception:
        pass
    shots = r.get("shots") or []
    got = [x for x in shots if x["state"] == "done"]
    if not got:
        return text("没拍到（拍照页不在前台、摄像头没打开或手机没有响应）。" + _offline_hint(), True)
    multi = len(shots) > 1
    content = []
    for x in shots:
        if x["state"] == "done":
            if multi:
                content.append({"type": "text", "text": "【%s】" % x["phone"]})
            content += photo_content(st["snapDir"], x["file"])
            if (_state() or {}).get("client") == "Codex":   # 只给 Codex：它的对话里不显示照片，附上原图链接让用户能点开看
                content.append({"type": "text", "text": "原图（在这台电脑上点开）：" + _photo_links(st["snapDir"], x["file"])})
        else:
            content.append({"type": "text", "text": "【%s】没拍到（%s）" % (x["phone"], x.get("error") or "没有响应")})
    if kind == "photo":
        _state()["last"] = [(x["phone"], x["file"]) for x in got]
    return {"content": content, "isError": False}


def tool_look(args):
    return _capture(args.get("what"), phone=args.get("phone"), light=args.get("light"))


def tool_look_clip(args):
    return _capture(args.get("what"), "clip", args.get("seconds") or 2, args.get("frames") or 4, args.get("phone"), args.get("light"))


def tool_zoom(args):
    try:
        st = api("/api/status")
    except Exception as exc:
        return text("Open Your Eyes 没有运行（%s）。请让用户在电脑上打开 Open Your Eyes 程序。" % exc, True)
    _last_photos = _state()["last"]
    if not _last_photos:
        return text("这个对话里还没拍过照片，先用 look 拍一张。", True)
    want = (args.get("phone") or "").strip()
    pick = [p for p in _last_photos if p[0] == want] if want else _last_photos[:1]
    if not pick:
        return text("最近一次 look 没有「%s」的照片（有：%s）。" % (want, "、".join(p[0] for p in _last_photos)), True)
    label, name = pick[0]
    try:
        res = api("/api/crop", {"name": name, "x": args.get("x"), "y": args.get("y"),
                                "w": args.get("w"), "h": args.get("h")}, timeout=20)
    except Exception as exc:
        return text("裁剪失败：%s" % exc, True)
    if not res.get("ok"):
        return text("裁剪失败：%s" % res.get("error"), True)
    out = [{"type": "image", "data": res["jpegBase64"], "mimeType": "image/jpeg"}]
    if len(_last_photos) > 1:
        out.insert(0, {"type": "text", "text": "【%s】" % label})
    return {"content": out, "isError": False}


TOOLS = [
    {
        "name": "look",
        "description": "LOOK_DESC",
        "inputSchema": {
            "type": "object",
            "properties": {"what": {"type": "string", "description": "想看什么，会显示在手机屏幕上，例如「板子背面的接线」"},
                           "phone": {"type": "string", "description": "只用这台手机（机位名，如「1 号机」「俯拍」）；不填则所有在线手机同时拍"},
                           "light": {"type": "string", "enum": ["auto", "on", "off"], "description": "补光：on 拍照时打开补光，off 这张不补光，auto 光线太暗才补；不填按手机上的设置"}},
            "required": ["what"],
        },
        # 拍照会动用用户的摄像头，不是「只读」操作：不标 readOnly，避免被自动模式当成无害调用直接放行
        "annotations": {"title": "用手机看一下", "readOnlyHint": False, "destructiveHint": False, "openWorldHint": True},
    },
    {
        "name": "zoom",
        "description": "放大本对话最近一次 look 照片的某个区域：从全尺寸原图裁剪，不重新拍照。x、y、w、h 是相对整张照片的比例（0~1，左上角为原点）。多机位时用 phone 指定哪一张，不填用第一张。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "x": {"type": "number", "minimum": 0, "maximum": 1, "description": "区域左边缘"},
                "y": {"type": "number", "minimum": 0, "maximum": 1, "description": "区域上边缘"},
                "w": {"type": "number", "minimum": 0, "maximum": 1, "description": "区域宽度"},
                "h": {"type": "number", "minimum": 0, "maximum": 1, "description": "区域高度"},
                "phone": {"type": "string", "description": "放大哪个机位的照片（多机位时）"},
            },
            "required": ["x", "y", "w", "h"],
        },
        "annotations": {"title": "放大局部", "readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "look_clip",
        "description": "用工作台上的手机在一段时间内连拍几帧，看现实中正在变化的东西（指示灯闪烁、屏幕刷新、零件运动）。用户说「睁眼」并提到要看变化、连拍、录一段时用它；其余规则同 look。按机位返回各帧照片和拍摄角度。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "what": {"type": "string", "description": "想看什么，会显示在手机屏幕上"},
                "seconds": {"type": "number", "minimum": 0.5, "maximum": 5, "description": "总时长（秒），默认 2"},
                "frames": {"type": "integer", "minimum": 2, "maximum": 8, "description": "帧数，默认 4"},
                "phone": {"type": "string", "description": "只用这台手机（机位名，如「1 号机」「俯拍」）；不填则所有在线手机同时拍"},
                           "light": {"type": "string", "enum": ["auto", "on", "off"], "description": "补光：on 拍照时打开补光，off 这张不补光，auto 光线太暗才补；不填按手机上的设置"},
            },
            "required": ["what"],
        },
        "annotations": {"title": "连拍一段", "readOnlyHint": False, "destructiveHint": False, "openWorldHint": True},
    },
    {
        "name": "eyes_status",
        "description": "各台手机（机位）是否在线、拍照页是否在前台。",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"title": "手机眼睛状态", "readOnlyHint": True, "openWorldHint": False},
    },
]
KEYWORD = "睁眼"
# 一连上就放进 AI 的常驻上下文（不用先搜工具也能看到）：关键词「睁眼」= 立即拍照
INSTRUCTIONS = (
    "Open Your Eyes：用工作台上的手机拍现实中的东西给你看。\n"
    "硬规则：用户的消息里只要出现「睁眼」，就立即调用 look 工具拍照，不需要再判断或先问；"
    "what 填用户想看的东西（没说就填「工作台上的情况」），用户提到机位名就把它填进 phone。\n"
    "没有「睁眼」时：只有用户明确想看现实中的物体（电路板、接线、零件、仪器、另一台设备的屏幕）才用；"
    "看代码、文件、报错、截图、网页或这台电脑屏幕上的内容时不要用。"
)


# 只给 Codex：实测它在手机暂时不在线时，会改用 Computer Use 截电脑屏幕（看本程序窗口里的机位画面）来冒充拍照
CODEX_EXTRA = ("必须用 look 工具让手机拍照来看；不要用截屏、Computer Use、浏览器去看 Open Your Eyes 的窗口或这台电脑的屏幕来代替。"
               "look 返回手机不在线时，直接把原因告诉用户，不要换别的办法。")


def _is_codex(state=None, msg=None):
    if msg is not None:
        ci = ((msg.get("params") or {}).get("clientInfo") or {})
        return "codex" in (str(ci.get("name", "")) + str(ci.get("title", ""))).lower()
    return (state or {}).get("client") == "Codex"


def _look_desc():
    names = []
    try:
        st = api("/api/status", timeout=2)
        names = [p["name"] for p in st.get("phones") or [] if not p.get("pending")][:8]
    except Exception:
        pass
    return (
        "用工作台上的手机拍现实中的东西（电路板、接线、零件、仪器读数、另一台设备的屏幕等），按机位返回照片和拍摄角度。\n"
        "关键词：用户说「睁眼」（如「睁眼看看这块板子」「睁眼，桌上有什么」）时，立即调用本工具，不用再判断。\n"
        "没说「睁眼」时，只在用户明确想看现实中的物体时用；看代码、文件、报错、截图、网页时不要用。\n"
        "默认所有在线手机同时各拍一张%s；只想用某一台时传 phone（机位名）。每次拍照会先请用户确认。"
        % (("（机位：" + "、".join(names) + "）") if names else ""))


def tools_list(codex=False):
    out = []
    for t in TOOLS:
        t = dict(t)
        if t["description"] == "LOOK_DESC":
            t["description"] = _look_desc() + (("\n" + CODEX_EXTRA) if codex else "")
        out.append(t)
    return out


# 现成的命令：Claude Code 里输入 / 就能选到，Claude 桌面版在输入框「+」菜单里。选了必定拍照，不靠 AI 去猜
PROMPTS = [{
    "name": "snap",
    "title": "用手机拍一张",
    "description": "让工作台上的手机拍一张照片给 AI 看",
    "arguments": [{"name": "what", "description": "想看什么（可以不填）", "required": False}],
}]


def prompt_get(name, args):
    if name != "snap":
        return None
    what = ((args or {}).get("what") or "").strip() or "工作台上现在的情况"
    return {"description": "用手机拍一张", "messages": [{"role": "user", "content": {"type": "text",
            "text": "请用 open-your-eyes 的 look 工具，让工作台上的手机拍一张照片，看看：%s。看完告诉我你看到了什么。" % what}}]}


HANDLERS = {"look": tool_look, "zoom": tool_zoom, "look_clip": tool_look_clip, "eyes_status": tool_status}


# ---------------- MCP（JSON-RPC 2.0）：电脑端程序的 /mcp 直接调用 respond；也可按行走 stdio（开发用） ----------------
VERSION = "0.7.5"   # 与 serve.py 一致（测试会检查）


def respond(msg, state=None):
    """处理一条 JSON-RPC 消息，返回要回复的消息（通知返回 None）。state：本会话的状态（HTTP 方式按会话传入）。"""
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:
        return None  # 通知（如 notifications/initialized）无需回复
    if method == "initialize":
        ver = (msg.get("params") or {}).get("protocolVersion") or PROTOCOL
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": ver,
            "capabilities": {"tools": {}, "prompts": {}},
            "instructions": INSTRUCTIONS + (("\n" + CODEX_EXTRA) if _is_codex(msg=msg) else ""),
            "serverInfo": {"name": "open-your-eyes", "version": VERSION},
        }}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": tools_list(codex=_is_codex(state))}}
    if method == "prompts/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"prompts": PROMPTS}}
    if method == "prompts/get":
        p = msg.get("params") or {}
        got = prompt_get(p.get("name"), p.get("arguments"))
        if got is None:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "没有这个命令"}}
        return {"jsonrpc": "2.0", "id": mid, "result": got}
    if method == "tools/call":
        p = msg.get("params") or {}
        fn = HANDLERS.get(p.get("name"))
        if not fn:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "未知工具 %s" % p.get("name")}}
        _ctx.state = state
        try:
            result = fn(p.get("arguments") or {})
        except Exception as exc:
            result = text("出错：%s" % exc, True)
        finally:
            _ctx.state = None
        return {"jsonrpc": "2.0", "id": mid, "result": result}
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "不支持 %s" % method}}


def send(msg):
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def handle(msg):
    out = respond(msg)
    if out is not None:
        send(out)


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        handle(msg)


if __name__ == "__main__":
    main()
