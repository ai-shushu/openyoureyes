#!/usr/bin/env python3
"""Open Your Eyes 页面服务（局域网）。

手机浏览器打开拍照页；Claude 的拍照请求和照片都经由这里，全部在本机与局域网内完成。
手机只拍 Claude 请求的照片（用户在电脑上「允许」即同意），照片原路交回发起请求的那个对话，不做主动推送。

手机侧接口：
  GET  /config.json          电脑的局域网地址
  GET  /api/phone/poll       手机每秒轮询：上报在线状态，领取待拍请求
  POST /api/snap             上传照片（JPEG + 拍摄瞬间的传感器数据），必须属于一个进行中的请求
本机接口（仅 127.0.0.1，供 MCP 使用）：
  POST /api/request          创建拍照请求；GET /api/request?id=&wait= 等待结果
  GET  /api/status           手机在线状态
  POST /api/crop             从全尺寸原图裁剪局部
设置页（仅本机，http://127.0.0.1:39757/settings）：
  GET  /api/settings         概况：手机地址、机位、偏好、照片占用、证书、开机自启、最近事件
  POST /api/phones/rename | /api/phones/remove | /api/prefs | /api/photos/clear | /api/autostart | /api/repair
  GET  /qr.svg?text=         二维码（扫码添加手机）

自动维护：启动时确保证书有效；电脑地址变化时自动重签证书并重启 HTTPS；按设置定期清理旧照片。
"""

import base64
import collections
import functools
import http.server
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import ssl
import threading
import time
import urllib.parse

ROOT = os.path.abspath(os.environ.get("T9_WEB_ROOT") or
                       os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "web"))
DEFAULT_PORT, DEFAULT_HTTPS_PORT = 39757, 39758   # 不常用的端口，不容易和别的软件冲突（早期版本用 8080 / 8443）
LEGACY_PORT, LEGACY_HTTPS_PORT = 8080, 8443
PORT = int(os.environ.get("T9_WEB_PORT", str(DEFAULT_PORT)))
SNAP_DIR = os.path.abspath(os.environ.get("T9_SNAP_DIR") or
                           os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "snaps"))
SNAP_MAX = 60 * 1024 * 1024   # 全尺寸原图或多帧连拍（base64）
HTTPS_PORT = int(os.environ.get("T9_HTTPS_PORT", str(DEFAULT_HTTPS_PORT)))
CERT_DIR = os.path.abspath(os.environ.get("T9_CERT_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "certs"))
# 检查电脑端新版本：发布地址上放一个 latest.json（dev/build_app.py 会生成），内容如 {"version": "0.8.0", "page": "下载页", "notes": "一句话说明"}
# 每 12 小时取一次，只发出版本号，不带任何用户数据；设环境变量 T9_UPDATE_FEED= （空）可关闭
UPDATE_FEED = os.environ.get("T9_UPDATE_FEED", "https://github.com/ai-shushu/openyoureyes/releases/latest/download/latest.json")
VERSION = "0.7.5"   # 唯一的版本号：插件、桌面版扩展、安卓 App 打包时都从这里同步

# 最近事件（手机报错、拒收、网络变化等），设置页「诊断」里用大白话列出来
_events = collections.deque(maxlen=80)


def log_event(kind, text):
    _events.append({"t": time.strftime("%m-%d %H:%M:%S"), "kind": kind, "text": text})
    print("%s %s" % (kind, text), flush=True)


_VIRTUAL_IF = re.compile(r"^(utun|tun|tap|ppp|ipsec|wg|tailscale|zt|docker|br-|veth|virbr|vmnet|vboxnet|bridge|awdl|llw|anpi|gif|stf)"
                         r"|vEthernet|VirtualBox|VMware|Hyper-V|Loopback|WSL", re.I)


def ip_candidates():
    """电脑上所有可能让手机连进来的地址，按「最像家里 Wi-Fi / 有线网」排序。
    开着 VPN、装了虚拟机时，系统默认路由可能指向虚拟网卡，手机连不上，所以要挑。"""
    out = []
    try:
        import psutil
        stats = psutil.net_if_stats()
        for name, addrs in psutil.net_if_addrs().items():
            st = stats.get(name)
            if st is not None and not st.isup:
                continue
            for a in addrs:
                if a.family != socket.AF_INET or a.address.startswith(("127.", "169.254.")):
                    continue
                score = 0
                if _VIRTUAL_IF.search(name):
                    score -= 50
                if a.address.startswith("192.168."):
                    score += 30
                elif a.address.startswith("10."):
                    score += 20
                elif re.match(r"^172\.(1[6-9]|2\d|3[01])\.", a.address):
                    score += 15
                elif a.address.startswith("100."):   # 运营商 NAT / Tailscale，多半不是家里 Wi-Fi
                    score -= 20
                else:
                    score -= 30   # 公网地址：手机用不了这个证书
                if re.match(r"^(en|eth|wlan|wl|Wi-?Fi|以太网|无线)", name, re.I):
                    score += 10
                out.append({"ip": a.address, "if": name, "score": score})
    except Exception:
        pass
    out.sort(key=lambda x: -x["score"])
    return out


def lan_ip():
    """手机连电脑用的地址：用户在设置页指定的 > 环境变量 T9_LAN_IP > 自动挑选。"""
    ip = (_prefs.get("lanIp") if "_prefs" in globals() else "") or os.environ.get("T9_LAN_IP", "").strip()
    if ip and ip != "127.0.0.1":
        if ip in [c["ip"] for c in ip_candidates()] or not ip_candidates():
            return ip
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.168.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        cands = ip_candidates()
        best = cands[0] if cands else None
        mine = next((c for c in cands if c["ip"] == ip), None)
        if best and (mine is None or mine["score"] < best["score"]) and best["score"] > 0:
            return best["ip"]   # 默认路由走的是 VPN / 虚拟网卡：换成真正的局域网地址
        if ip and not ip.startswith("127."):
            return ip
    except OSError:
        pass
    cands = ip_candidates()
    return cands[0]["ip"] if cands else "127.0.0.1"


# ---------------- 图像：概览图与局部裁剪 ----------------
# Claude 会把过大的图片自动缩小，整张原图直接给它既浪费又看不清细节。
# 所以：给 Claude 一张长边 VIEW_MAX 的概览图看全貌；要看细节时，从全尺寸原图裁出那一块。
VIEW_MAX = 1568


def _write_atomic(path, data):
    with open(path + ".tmp", "wb") as f:
        f.write(data)
    os.replace(path + ".tmp", path)


def _to_jpeg(im, quality=88):
    import io
    buf = io.BytesIO()
    im.convert("RGB").save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def make_view(name):
    """由原图生成概览图 name.view.jpg；原图本身不大时直接沿用。"""
    from PIL import Image, ImageOps
    src = os.path.join(SNAP_DIR, name + ".jpg")
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im)
        if max(im.size) <= VIEW_MAX:
            return
        im.thumbnail((VIEW_MAX, VIEW_MAX), Image.LANCZOS)
        _write_atomic(os.path.join(SNAP_DIR, name + ".view.jpg"), _to_jpeg(im))


def crop_original(name, box):
    """box = [x, y, w, h]，0~1 的相对坐标（相对整张照片）。从全尺寸原图裁剪，结果长边不超过 VIEW_MAX。"""
    from PIL import Image, ImageOps
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise ValueError("bad name")
    x, y, w, h = [float(v) for v in box]
    if not (0 <= x < 1 and 0 <= y < 1 and 0 < w <= 1 and 0 < h <= 1):
        raise ValueError("区域坐标要在 0~1 之间")
    with Image.open(os.path.join(SNAP_DIR, name + ".jpg")) as im:
        im = ImageOps.exif_transpose(im)
        W, H = im.size
        l, t = int(x * W), int(y * H)
        r, b = min(W, int((x + w) * W)), min(H, int((y + h) * H))
        if r - l < 8 or b - t < 8:
            raise ValueError("区域太小")
        part = im.crop((l, t, r, b))
        part.thumbnail((VIEW_MAX, VIEW_MAX), Image.LANCZOS)
        return _to_jpeg(part, 90)


# ---------------- 拍照请求：Claude 请求 → 所有手机同时拍 → 照片交回请求方 ----------------
# 用户在电脑上「允许」Claude 的工具调用即视为同意拍照，所以请求一创建就是可拍状态，手机领取后立刻拍。
# 多台手机：请求创建时把当时在线的手机都列为目标（或只用指定的一台），每台各拍一张，按机位分别交回。
# 手机用「长轮询」领取请求：连接挂在服务端，请求一出现就同时唤醒所有手机，机位之间只差零点几秒。
REQUEST_TTL = 60          # 秒：手机这么久没领取就作废
PHONE_ONLINE_S = 5        # 秒：手机多久内连过算在线
POLL_HOLD_S = 3           # 秒：长轮询最多挂这么久
DEBUG_POLL = os.environ.get("T9_DEBUG_POLL") == "1"   # 排查用：记录每一次轮询
_req_cv = threading.Condition()
_requests = {}            # id -> {id, what, kind, created, open, shots: {phoneId: {state, takenAt, file}}}
_phones = {}              # phoneId -> {id, name, ip, ua, lastSeen, visible, polling}
PHONES_FILE = os.path.join(SNAP_DIR, ".phones.json")
# 机位名单：手机编号 → {名字, Wi-Fi 网卡地址, 设备类型, 最后在线时间}，重启后保持。
# 手机编号存在手机浏览器里，清数据、换浏览器、无痕模式都会丢；这时靠「同一网卡地址 + 同类设备」认出是老手机。
STALE_DAYS = 30   # 这么久没出现的离线机位不再显示（记录保留，回来时照样认得）


def _load_names():
    names, meta = {}, {}
    try:
        with open(PHONES_FILE, encoding="utf-8") as f:
            raw = dict(json.load(f))
    except (OSError, ValueError):
        return names, meta
    for pid, v in raw.items():
        if isinstance(v, dict):   # 新格式
            names[pid] = v.get("name") or pid
            meta[pid] = {k: v.get(k) for k in ("mac", "device", "lastSeen", "app")}
            meta[pid]["ok"] = v.get("ok", True)   # 加「允许」机制之前登记的手机，都是用户自己的
        else:                     # 旧格式：只有名字
            names[pid] = v
            meta[pid] = {"ok": True}
    return names, meta


_names, _meta = _load_names()
_names_saved = [0.0]


def _save_names():
    data = {}
    for pid, nm in _names.items():
        rec = {"name": nm}
        rec.update({k: v for k, v in (_meta.get(pid) or {}).items() if v is not None})
        data[pid] = rec
    os.makedirs(SNAP_DIR, exist_ok=True)
    with open(PHONES_FILE + ".tmp", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(PHONES_FILE + ".tmp", PHONES_FILE)
    _names_saved[0] = time.time()


# ---------------- 配对：局域网里的其他设备不能冒充机位 ----------------
# 新手机要么扫电脑设置页的二维码（二维码里带配对密钥，自动通过），要么由用户在设置页点「允许」。
# 没通过的手机领不到拍照请求、发不了照片和画面。
PAIR_FILE = os.path.join(CERT_DIR, "pair.key")
PENDING_MAX = 5


def app_info():
    """电脑端带着的安卓 App 安装包版本（web/app/version.json，由 dev/build_android.py 生成）。"""
    try:
        with open(os.path.join(ROOT, "app", "version.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def pair_key():
    try:
        with open(PAIR_FILE) as f:
            k = f.read().strip()
            if len(k) >= 16:
                return k
    except OSError:
        pass
    k = secrets.token_urlsafe(18)
    os.makedirs(CERT_DIR, exist_ok=True)
    _write_atomic(PAIR_FILE, k.encode("ascii"))
    try:
        os.chmod(PAIR_FILE, 0o600)
    except OSError:
        pass
    return k


def key_ok(k):
    return bool(k) and secrets.compare_digest(str(k), pair_key())


def phone_ok(pid):
    return bool((_meta.get(pid) or {}).get("ok"))


def approve_phone(pid):
    with _req_cv:
        if pid not in _names:
            return False
        _meta.setdefault(pid, {})["ok"] = True
        _save_names()
        _req_cv.notify_all()
    log_event("PHONE", "已允许「%s」作为机位" % _names[pid])
    return True


def _trim_pending(keep):
    """没通过的新手机最多留 PENDING_MAX 台，防止有人刷一堆假手机占满名单。"""
    pend = [p for p in _names if p != keep and not phone_ok(p)]
    pend.sort(key=lambda p: (_meta.get(p) or {}).get("lastSeen") or 0)
    for p in pend[:max(0, len(pend) - PENDING_MAX + 1)]:
        _names.pop(p, None)
        _meta.pop(p, None)
        _phones.pop(p, None)


_mac_cache = {}


def mac_of(ip):
    """电脑在局域网里看到的这台手机的 Wi-Fi 网卡地址（查本机 ARP 表）。网页本身读不到任何硬件识别码，
    但手机连过来时电脑一定知道它的网卡地址。新手机默认用「私有地址」：同一个 Wi-Fi 下固定，换 Wi-Fi 才变。"""
    if not ip or ip.startswith("127.") or ip == "::1" or ":" in ip:
        return None
    hit = _mac_cache.get(ip)
    if hit and time.time() - hit[1] < 120:
        return hit[0]
    mac = None
    try:
        if sys.platform.startswith("linux") and os.path.exists("/proc/net/arp"):
            with open("/proc/net/arp") as f:
                for line in f:
                    parts = line.split()
                    if parts and parts[0] == ip and len(parts) > 3:
                        mac = parts[3]
        else:
            args = ["arp", "-a", ip] if os.name == "nt" else ["arp", "-n", ip]
            out = subprocess.run(args, capture_output=True, text=True, timeout=3).stdout
            m = re.search(r"(?:[0-9a-fA-F]{1,2}[:-]){5}[0-9a-fA-F]{1,2}", out)
            mac = m.group(0) if m else None
    except Exception:
        mac = None
    if mac:
        mac = ":".join(x.zfill(2) for x in re.split("[:-]", mac.lower()))
        if mac in ("00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff"):
            mac = None
    _mac_cache[ip] = (mac, time.time())
    return mac


def _clean_name(name):
    return "".join(ch for ch in (name or "") if ch.isprintable()).strip()[:16]


def _phone_online(ph, now):
    if not phone_ok(ph["id"]):
        return False
    return ph["polling"] > 0 or now - ph["lastSeen"] < PHONE_ONLINE_S


def _expire_requests(now):
    for r in _requests.values():
        for shot in r["shots"].values():
            if shot["state"] == "waiting" and now - r["created"] > REQUEST_TTL:
                shot["state"] = "failed"
                shot["error"] = "超时没有领取"


CLIP_MAX_SECONDS = 5
CLIP_MAX_FRAMES = 8


def create_request(what, kind="photo", seconds=None, frames=None, phone=None, light=None):
    """kind=photo：拍一张全尺寸照片；kind=clip：在 seconds 秒内均匀连拍 frames 帧。
    phone 为空 = 当前在线的所有手机同时拍；否则只用名字或编号匹配的那一台。返回 (请求编号, 错误)。"""
    now = time.time()
    with _req_cv:
        online = [ph for ph in _phones.values() if _phone_online(ph, now)]
        if not phone and _prefs.get("defaultPhone"):   # 设置页选了默认机位：在线就只用它，不在线就退回全部
            main = [ph for ph in online if ph["id"] == _prefs["defaultPhone"]]
            online = main or online
        if phone:
            want = _clean_name(phone)
            online = [ph for ph in online if ph["name"] == want or ph["id"] == want]
            if not online:
                names = "、".join(ph["name"] for ph in _phones.values() if _phone_online(ph, now)) or "无"
                return None, "没有叫「%s」的在线手机（在线：%s）" % (want, names)
        if not online:
            return None, "没有在线的手机"
        rid = "r" + secrets.token_hex(8)   # 随机编号：猜不到，就没法冒名交照片
        req = {"id": rid, "what": (what or "")[:200], "kind": "clip" if kind == "clip" else "photo",
               "created": now, "open": True, "light": light if light in ("on", "off", "auto") else None,
               "shots": {ph["id"]: {"state": "waiting", "takenAt": None, "file": None} for ph in online}}
        if req["kind"] == "clip":
            req["seconds"] = max(0.5, min(float(seconds or 2), CLIP_MAX_SECONDS))
            req["frames"] = max(2, min(int(frames or 4), CLIP_MAX_FRAMES))
        _expire_requests(now)
        _requests[rid] = req
        _req_cv.notify_all()
    print("REQUEST %s %s %r -> %s" % (rid, req["kind"], what, ",".join(_phones[i]["name"] for i in req["shots"])), flush=True)
    return rid, None


def _claim(pid, now):
    """找一个给这台手机、还没拍的请求并领取。领取后迟迟没交照片（比如页面被切走）就允许重新领取。"""
    for r in sorted(_requests.values(), key=lambda x: x["created"]):
        shot = r["shots"].get(pid)
        if not r["open"] or not shot:
            continue
        if shot["state"] == "waiting" or (shot["state"] == "taken" and now - shot["takenAt"] > 8 + r.get("seconds", 0)):
            shot["state"] = "taken"
            shot["takenAt"] = now
            print("CLAIM %s %s +%.2fs" % (r["id"], (_phones.get(pid) or {}).get("name", pid), now - r["created"]), flush=True)
            out = {k: r[k] for k in ("id", "what", "kind", "seconds", "frames", "light") if r.get(k) is not None}
            out["phones"] = len(r["shots"])
            return out
    return None


STATE_TEXT = {"ready": "待命", "busy": "正在拍照", "camera": "摄像头启动中", "hidden": "拍照页在后台"}


_STATE_RANK = {"busy": 4, "ready": 3, "camera": 2, "hidden": 1}
_cover_req = [0.0]   # 设置页点「刷新画面」的时间：手机看到它变了，就立刻截一张新封面（和每分钟自动截的一样，只给设置页看）


def refresh_covers():
    with _req_cv:
        _cover_req[0] = time.time()
        _req_cv.notify_all()   # 叫醒正在等的手机，马上去截
    log_event("PHONE", "在设置页刷新了全部画面")


def phone_poll(pid, name, ip, ua, visible, hold, facing=None, torch=None, state=None, live_have="", paired=False, app=False,
               page=""):
    """手机长轮询：登记在线状态；有给它的拍照请求、或实时预览会话有变化就立刻返回，否则最多挂 hold 秒。
    返回 (本机编号, 本机名字, 拍照请求, 预览会话列表)。"""
    pid = "".join(ch for ch in (pid or "") if ch.isalnum())[:32] or ("ip-" + ip.replace(".", "-").replace(":", "-"))
    # 查网卡地址要调用系统命令（最长几秒），必须在锁外做，否则会卡住所有手机、设置页和 AI 的请求
    known = _phones.get(pid)
    mac_now = mac_of(ip) if (known is None or known.get("ip") != ip) else None
    with _req_cv:
        if pid in _denied and not paired:
            return pid, "", None, None
        _denied.discard(pid)
        ph = _phones.get(pid)
        if not ph:
            legacy = "ip-" + ip.replace(".", "-").replace(":", "-")
            if pid not in _names and pid != legacy and legacy in _names:
                # 旧版页面没有自带编号，按 IP 登记过；刷新成新版后沿用原来的名字
                _names[pid] = _names.pop(legacy)
                _phones.pop(legacy, None)
                _save_names()
            if pid not in _names:
                # 同一台手机清了浏览器数据、换了浏览器或用了无痕模式，会带着新编号回来：
                # 网卡地址相同（或同一 IP，旧记录还在内存里）、同类设备、旧编号已离线的，就是它——接过名字和设置
                now, dev, mac = time.time(), _device(ua), mac_now
                for old in list(_names):
                    o, m = _phones.get(old), _meta.get(old) or {}
                    if o and (o["polling"] or now - o["lastSeen"] <= 15):
                        continue   # 旧编号还在线，那是另一台
                    by_mac = bool(mac) and m.get("mac") == mac and m.get("device") in (None, dev)
                    by_ip = bool(o) and o.get("ip") == ip and _device(o.get("ua")) == dev
                    if by_mac or by_ip:
                        _names[pid] = _names.pop(old)
                        _meta.pop(old, None)
                        _phones.pop(old, None)
                        if old in _last_photo:
                            _last_photo[pid] = _last_photo.pop(old)
                        _move_cover(old, pid)
                        if _prefs.get("defaultPhone") == old:
                            _prefs["defaultPhone"] = pid
                            _write_atomic(PREFS_FILE, json.dumps(_prefs, ensure_ascii=False, indent=1).encode("utf-8"))
                        log_event("PHONE", "认出了老机位「%s」（清过浏览器数据或换了浏览器），沿用原来的名字和设置" % _names[pid])
                        break
            if pid not in _names:
                _trim_pending(pid)
                n = 1
                while ("%d 号机" % n) in _names.values():   # 取最小的未占用编号，避免和改过名的手机重名
                    n += 1
                _names[pid] = "%d 号机" % n
                _save_names()
            ph = _phones[pid] = {"id": pid, "name": _names[pid], "polling": 0, "lastSeen": 0.0}
            old_m = _meta.get(pid) or {}
            _meta[pid] = {"mac": mac_now or old_m.get("mac"), "device": _device(ua), "lastSeen": time.time(),
                          "ok": bool(old_m.get("ok"))}
            _save_names()
        if not phone_ok(pid):
            if paired:   # 扫了电脑上的二维码：自动通过
                _meta.setdefault(pid, {})["ok"] = True
                _save_names()
                log_event("PHONE", "「%s」扫码配对成功" % ph["name"])
            else:
                if not ph.get("pendingLogged"):
                    ph["pendingLogged"] = True
                    log_event("PHONE", "有一台新手机（%s）请求加入，等你在设置页点「允许」" % ph["name"])
                ph.update({"ip": ip, "ua": ua, "visible": False, "state": None, "lastSeen": time.time()})
                (_meta.setdefault(pid, {}))["lastSeen"] = time.time()
                return pid, ph["name"], None, None   # None = 还没允许
        name = _clean_name(name)
        if name and name != ph["name"]:
            _names[pid] = ph["name"] = name
            _save_names()
        if state not in STATE_TEXT:   # 旧版页面只报「能不能拍」
            state = "ready" if visible else "camera"
        page_state = state
        # 同一台手机可能开着几个拍照页（比如浏览器里多了一个标签）：只要有一个在前台，这台手机就算在前台，
        # 不然几个页面轮流报「前台 / 后台」，看起来像手机在反复切换
        now0 = time.time()
        pages = ph.setdefault("pages", {})
        pages[(page or "_")[:16]] = (state, now0)
        for k in [k for k, v in pages.items() if now0 - v[1] > 8]:
            pages.pop(k, None)
        state = max((v[0] for v in pages.values()), key=lambda x: _STATE_RANK.get(x, 0))
        prev = ph.get("state")
        if state != prev:
            ph["stateSince"] = time.time()
            if state == "hidden":
                log_event("PHONE", "%s：拍照页切到了后台（屏幕关了或切到了别的应用），这期间拍不了" % ph["name"])
            elif prev == "hidden":
                log_event("PHONE", "%s：拍照页回到前台" % ph["name"])
        m = _meta.setdefault(pid, {})
        if ph.get("ip") != ip:
            m["mac"] = mac_now or m.get("mac")
            m["device"] = _device(ua) or m.get("device")
        m["lastSeen"] = time.time()
        if m["lastSeen"] - _names_saved[0] > 60:
            _save_names()
        ph.update({"ip": ip, "ua": ua, "visible": state == "ready", "state": state, "lastSeen": time.time(), "app": bool(app)})
        if bool(m.get("app")) != bool(app):   # 记下是 App 还是网页，离线时设置页也能正确显示
            m["app"] = bool(app)
            _save_names()
        if facing in ("user", "environment"):
            ph["facing"] = facing
        if torch in ("0", "1"):
            ph["torch"] = torch == "1"
        have = sorted(x for x in (live_have or "").split(",") if x)
        if page_state == "hidden":
            return pid, ph["name"], None, []   # 这个页面在后台时摄像头已关：不领拍照请求，也不发预览
        t_enter = time.time()
        deadline = t_enter + max(0.0, min(hold, POLL_HOLD_S))
        ph["polling"] += 1
        try:
            while True:
                now = time.time()
                _expire_requests(now)
                _live_expire(now)
                live = _live_for(pid)
                req = _claim(pid, now) if visible else None
                changed = [x["sid"] for x in live] != have or _cover_req[0] > t_enter
                if req or changed or now >= deadline:
                    return pid, ph["name"], req, live
                _req_cv.wait(deadline - now)
        finally:
            ph["polling"] -= 1
            ph["lastSeen"] = time.time()


# ---------------- 实时预览（对准机位用）：视频走手机 → 电脑浏览器的局域网直连，这里只转交连接信息 ----------------
# 必须按需：设置页每 3 秒发一次心跳，8 秒收不到就结束会话，手机随即停止发送；单次最长 30 分钟。
LIVE_TTL_S = 8
LIVE_MAX_S = 1800
_live = {}   # sid -> {sid, phone, q, created, ping, offer, answer}


def _live_expire(now):
    for sid in [k for k, v in _live.items() if now - v["ping"] > LIVE_TTL_S or now - v["created"] > LIVE_MAX_S]:
        _live.pop(sid, None)


def _live_for(pid):
    return sorted(({"sid": v["sid"], "q": v["q"]} for v in _live.values() if v["phone"] == pid), key=lambda x: x["sid"])


def live_start(phone, q):
    with _req_cv:
        if phone not in _phones:
            raise ValueError("没有这台手机")
        sid = secrets.token_hex(16)
        now = time.time()
        _live[sid] = {"sid": sid, "phone": phone, "q": "high" if q == "high" else "low", "created": now, "ping": now,
                      "offer": None, "answer": None}
        _req_cv.notify_all()
        return sid


def live_ping(sid, q=None):
    with _req_cv:
        _live_expire(time.time())
        v = _live.get(sid)
        if not v:
            return {"state": "ended"}
        v["ping"] = time.time()
        if q in ("low", "high") and q != v["q"]:
            v["q"] = q
            _req_cv.notify_all()
        if v.get("fseq"):
            return {"state": "live", "mode": "mjpeg"}
        return {"state": "live" if v["answer"] else "connecting", "offer": v["offer"] if not v["answer"] else None}


# 安卓 App 不走网页实时视频：直接把低分辨率 JPEG 画面一帧帧交上来，设置页按 MJPEG 显示
_frame_cv = threading.Condition()
FRAME_MAX = 600 * 1024


def live_frame(sid, phone, jpg):
    with _req_cv:
        v = _live.get(sid)
        if not v or v["phone"] != phone:
            return False
        v["ping_phone"] = time.time()
    with _frame_cv:
        v["frame"] = jpg
        v["fseq"] = v.get("fseq", 0) + 1
        _frame_cv.notify_all()
    return True


def live_next_frame(sid, seq, timeout=2.0):
    """等下一帧（比 seq 新的）。返回 (帧, 序号)；会话结束返回 (None, -1)。"""
    deadline = time.time() + timeout
    with _frame_cv:
        while True:
            v = _live.get(sid)
            if not v:
                return None, -1
            if v.get("fseq", 0) > seq:
                return v["frame"], v["fseq"]
            left = deadline - time.time()
            if left <= 0:
                return None, seq
            _frame_cv.wait(left)


def live_stop(sid):
    with _req_cv:
        if _live.pop(sid, None):
            _req_cv.notify_all()


def live_offer(sid, phone, sdp):
    with _req_cv:
        v = _live.get(sid)
        if not v or v["phone"] != phone:
            return False
        v["offer"] = str(sdp)[:20000]
        _req_cv.notify_all()
        return True


def live_answer(sid, sdp):
    with _req_cv:
        v = _live.get(sid)
        if not v:
            return False
        v["answer"] = str(sdp)[:20000]
        _req_cv.notify_all()
        return True


def live_wait_answer(sid, timeout):
    deadline = time.time() + timeout
    with _req_cv:
        while True:
            v = _live.get(sid)
            if not v:
                return {"state": "ended"}
            if v["answer"]:
                return {"state": "live", "answer": v["answer"]}
            left = deadline - time.time()
            if left <= 0:
                return {"state": "connecting"}
            _req_cv.wait(min(left, 1.0))


def _snapshot(r):
    shots = []
    for pid, shot in r["shots"].items():
        ph = _phones.get(pid, {})
        shots.append({"phone": ph.get("name") or pid, "phoneId": pid, "state": shot["state"],
                      "file": shot["file"], "error": shot.get("error")})
    done = all(s["state"] in ("done", "failed") for s in shots)
    return {"id": r["id"], "state": "done" if done else ("open" if r["open"] else "closed"), "shots": shots}


def wait_request(rid, timeout):
    """等到所有机位都有结果（拍到或失败），或超时；返回各机位的当前结果。"""
    deadline = time.time() + timeout
    with _req_cv:
        while True:
            r = _requests.get(rid)
            if not r:
                return {"state": "unknown", "shots": []}
            _expire_requests(time.time())
            snap = _snapshot(r)
            left = deadline - time.time()
            if snap["state"] != "open" or left <= 0:
                return snap
            _req_cv.wait(min(left, 1.0))


def cancel_request(rid):
    """结束请求：还没交照片的机位记为失败，之后再上传的照片一律不收。"""
    with _req_cv:
        r = _requests.get(rid)
        if r and r["open"]:
            r["open"] = False
            for shot in r["shots"].values():
                if shot["state"] in ("waiting", "taken"):
                    shot["state"] = "failed"
                    shot.setdefault("error", "没有及时拍到")
            _req_cv.notify_all()


def request_open(request_id, phone_id):
    with _req_cv:
        r = _requests.get(request_id) if request_id else None
        shot = r["shots"].get(phone_id) if r else None
        return bool(r and r["open"] and shot and shot["state"] in ("waiting", "taken"))


def on_snap_saved(name, request_id, phone_id):
    """照片落盘后交给发起请求的一方（MCP 正在 wait_request 里等）。"""
    _summary_cache[0] = 0.0
    with _req_cv:
        r = _requests.get(request_id)
        shot = r["shots"].get(phone_id) if r else None
        if r and r["open"] and shot and shot["state"] in ("waiting", "taken"):
            shot["state"] = "done"
            shot["file"] = name
            _req_cv.notify_all()
            return True
    return False


def _app_version(ua):
    m = re.search(r"(?:OpenYourEyesApp|AgentEyeApp|T9EyesApp)/([\d.]+)", ua or "")
    return m.group(1) if m else None


def _device_detail(ua):
    """给人看的机型 + 系统，比如「V1818A · Android 9」「iPhone · iOS 17.5」；认不出就 None（只用来显示）。"""
    ua = ua or ""
    m = re.search(r"iPhone OS (\d+)[_.](\d+)", ua)
    if m:
        return "%s · iOS %s.%s" % ("iPad" if "iPad" in ua else "iPhone", m.group(1), m.group(2))
    m = re.search(r"Android ([\d.]+)(?:; ([^;)]+))?", ua)
    if m:
        model = (m.group(2) or "").strip()
        if model in ("K", "wv") or not model:   # 新版 Chrome 把机型隐藏成「K」
            return "Android " + m.group(1)
        return "%s · Android %s" % (model.split(" Build/")[0], m.group(1))
    return None


def _device(ua):
    ua = ua or ""
    return "iPhone" if "iPhone" in ua else "iPad" if "iPad" in ua else "Android" if "Android" in ua else None


_last_photo = {}   # 手机编号 -> 最近一张照片文件名（设置页「手机屏幕」默认显示它）


def _scan_last_photos():
    try:
        names = sorted((n for n in os.listdir(SNAP_DIR) if n.endswith(".json") and not n.startswith(".")), reverse=True)
    except OSError:
        return
    for n in names[:400]:
        try:
            with open(os.path.join(SNAP_DIR, n), encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            continue
        pid = (meta.get("phone") or {}).get("id")
        if pid and pid not in _last_photo:
            base = n[:-5]
            view = base + ".view.jpg"
            _last_photo[pid] = view if os.path.exists(os.path.join(SNAP_DIR, view)) else meta.get("file")


# ---------------- 封面（只给设置页看的小图，不进照片库，Claude 拿不到） ----------------
COVER_DIR = os.path.join(SNAP_DIR, ".covers")
COVER_MAX = 300 * 1024
_PID_RE = re.compile(r"^[A-Za-z0-9_-]{4,40}$")


def cover_path(pid):
    return os.path.join(COVER_DIR, pid + ".jpg") if _PID_RE.match(pid or "") else None


def cover_ts(pid):
    fp = cover_path(pid)
    try:
        return int(os.path.getmtime(fp)) if fp else None
    except OSError:
        return None


def save_cover(pid, ip, jpg):
    """手机连上后、放稳时自动发来的一张小图，只用来在设置页显示「它现在对着哪儿」。"""
    with _req_cv:
        ph = _phones.get(pid)
        if not ph or ph.get("ip") != ip or not phone_ok(pid):
            return False   # 只收正在连着的、已允许的那台手机自己发的
    fp = cover_path(pid)
    if not fp or not jpg.startswith(b"\xff\xd8") or len(jpg) > COVER_MAX:
        return False
    os.makedirs(COVER_DIR, exist_ok=True)
    _write_atomic(fp, jpg)
    return True


def _move_cover(old, new):
    a, b = cover_path(old), cover_path(new)
    try:
        if a and b and os.path.exists(a):
            os.replace(a, b)
    except OSError:
        pass


def phones_status():
    now = time.time()
    with _req_cv:
        out = []
        for pid, nm in _names.items():   # 记得名字、但这次还没连上过的手机，也列出来（离线）
            if pid not in _phones and phone_ok(pid):
                m = _meta.get(pid) or {}
                ago = now - m["lastSeen"] if m.get("lastSeen") else None
                if ago is not None and ago > STALE_DAYS * 86400:
                    continue   # 太久没出现：不再占位置（记录还在，回来时照样认得）
                out.append({"id": pid, "name": nm, "online": False, "visible": False, "state": None, "stateText": None,
                            "stateForS": None, "facing": None, "torch": None,
                            "lastSeenAgoS": round(ago) if ago is not None else None, "ip": None,
                            "device": m.get("device"), "lastPhoto": _last_photo.get(pid), "cover": cover_ts(pid),
                            "app": bool(m.get("app"))})
        for ph in sorted(_phones.values(), key=lambda p: p["name"]):
            if not phone_ok(ph["id"]):
                if now - ph["lastSeen"] < 30:   # 没允许的新手机：只在它还开着页面时显示
                    out.append({"id": ph["id"], "name": ph["name"], "online": False, "pending": True, "visible": False,
                                "state": None, "stateText": None, "stateForS": None, "lastPhoto": None, "cover": None,
                                "facing": None, "torch": None, "lastSeenAgoS": round(now - ph["lastSeen"]),
                                "ip": ph.get("ip"), "device": _device(ph.get("ua"))})
                continue
            online = _phone_online(ph, now)
            st = ph.get("state") if online else None
            out.append({"id": ph["id"], "name": ph["name"], "online": online, "visible": bool(ph.get("visible")) and online,
                        "state": st, "stateText": STATE_TEXT.get(st) if st else None,
                        "stateForS": round(now - ph.get("stateSince", now)) if st else None,
                        "lastPhoto": _last_photo.get(ph["id"]), "cover": cover_ts(ph["id"]),
                        "facing": ph.get("facing"), "torch": ph.get("torch"), "app": bool(ph.get("app")),
                        "lastSeenAgoS": 0 if ph["polling"] else round(now - ph["lastSeen"], 1),
                        "ip": ph.get("ip"), "device": _device(ph.get("ua")), "model": _device_detail(ph.get("ua")),
                        "appVersion": _app_version(ph.get("ua"))})
        return sorted(out, key=lambda p: p["name"])


# ---------------- 偏好与照片管理（设置页） ----------------
PREFS_FILE = os.path.join(SNAP_DIR, ".prefs.json")
DEFAULT_PREFS = {"defaultPhone": "", "retentionDays": 30, "lanIp": ""}


def _load_prefs():
    out = dict(DEFAULT_PREFS)
    try:
        with open(PREFS_FILE, encoding="utf-8") as f:
            out.update({k: v for k, v in json.load(f).items() if k in DEFAULT_PREFS})
    except (OSError, ValueError):
        pass
    return out


_prefs = _load_prefs()


def save_prefs(upd):
    with _req_cv:
        if "defaultPhone" in upd:
            _prefs["defaultPhone"] = str(upd["defaultPhone"] or "")[:40]
        if "retentionDays" in upd:
            _prefs["retentionDays"] = max(0, min(int(upd["retentionDays"] or 0), 3650))
        if "lanIp" in upd:   # 空 = 自动挑选；电脑地址变化由 _watch_network 在 10 秒内处理（重签证书）
            _prefs["lanIp"] = str(upd["lanIp"] or "")[:45]
        os.makedirs(SNAP_DIR, exist_ok=True)
        _write_atomic(PREFS_FILE, json.dumps(_prefs, ensure_ascii=False, indent=1).encode("utf-8"))
        return dict(_prefs)


def rename_phone(pid, name):
    name = _clean_name(name)
    if not name:
        raise ValueError("名字不能为空")
    log_event("PHONE", "「%s」改名为「%s」" % (_names.get(pid, pid), name))
    with _req_cv:
        if pid not in _names:
            raise ValueError("没有这台手机")
        _names[pid] = name
        if pid in _phones:
            _phones[pid]["name"] = name
        _save_names()


_denied = set()   # 在设置页点了「拒绝」的新手机：这次运行期间不再显示


def remove_phone(pid):
    """从名单里移除（多半是不再用的旧手机）。它要是还开着拍照页，会以新编号重新出现。"""
    log_event("PHONE", "在设置页移除了「%s」" % _names.get(pid, pid))
    with _req_cv:
        if not phone_ok(pid):
            _denied.add(pid)
        _names.pop(pid, None)
        _meta.pop(pid, None)
        _phones.pop(pid, None)
        _save_names()
        fp = cover_path(pid)
        if fp and os.path.exists(fp):
            try:
                os.remove(fp)
            except OSError:
                pass
        if _prefs.get("defaultPhone") == pid:
            _prefs["defaultPhone"] = ""
            _write_atomic(PREFS_FILE, json.dumps(_prefs, ensure_ascii=False, indent=1).encode("utf-8"))


def _photo_groups():
    """照片按拍摄编号分组：name.jpg / name.view.jpg / name.json / name.fN.jpg 属于同一张。"""
    groups = {}
    try:
        for n in os.listdir(SNAP_DIR):
            if n.startswith("."):
                continue
            groups.setdefault(n.split(".", 1)[0], []).append(n)
    except OSError:
        pass
    return groups


_summary_cache = [0.0, None]


def photos_summary(fresh=False):
    """设置页每 3 秒刷新：统计结果缓存 20 秒（拍照、删除时会立即刷新）。"""
    if not fresh and _summary_cache[1] and time.time() - _summary_cache[0] < 20:
        return _summary_cache[1]
    groups = _photo_groups()
    size = 0
    for files in groups.values():
        for n in files:
            try:
                size += os.path.getsize(os.path.join(SNAP_DIR, n))
            except OSError:
                pass
    _summary_cache[:] = [time.time(), {"dir": SNAP_DIR, "count": len(groups), "bytes": size}]
    return _summary_cache[1]


def delete_photos(older_than_days=None):
    """删除照片：older_than_days 为空 = 全部删除。返回删掉的张数。"""
    cutoff = time.time() - older_than_days * 86400 if older_than_days else None
    n = 0
    for base, files in _photo_groups().items():
        if cutoff is not None:
            try:
                if os.path.getmtime(os.path.join(SNAP_DIR, files[0])) >= cutoff:
                    continue
            except OSError:
                continue
        for f in files:
            try:
                os.remove(os.path.join(SNAP_DIR, f))
            except OSError:
                pass
        n += 1
    _summary_cache[0] = 0.0
    return n


def _cleanup_loop():
    while True:
        days = _prefs.get("retentionDays") or 0
        if days > 0:
            n = delete_photos(days)
            if n:
                log_event("CLEAN", "已自动清理 %d 张 %d 天前的照片" % (n, days))
        time.sleep(3600)


# ---------------- 证书与 HTTPS：启动时确保有效，电脑地址变了自动重签并重启 ----------------
_https = {"srv": None, "ip": None, "error": None}


class _TLSServer(http.server.ThreadingHTTPServer):
    """手机用的 HTTPS 服务。
    - 握手放到每个连接自己的线程里做：浏览器常会先开好连接却不马上发数据，之前在接收线程里握手，
      一个这样的连接就能把所有手机的新连接卡住最多 10 秒
    - 收到的是普通 http 请求（用户手输地址少打了 https://）就自动跳转到 https，而不是直接断开
    - 握手失败（比如手机不信任证书）记入事件，设置页诊断和排查才看得到"""
    _last = {}
    rejects = {}   # 手机 IP -> 最近一次「不信任证书」的时间

    def get_request(self):
        return self.socket.accept()   # 原始连接，握手在 finish_request 里做

    def _note(self, addr, exc):
        reason = str(getattr(exc, "reason", "") or "")
        if "CERTIFICATE_UNKNOWN" in reason or "UNKNOWN_CA" in reason or "BAD_CERTIFICATE" in reason:
            _TLSServer.rejects[addr[0]] = time.time()
        key = (addr[0], reason or str(exc)[:80])
        if time.time() - self._last.get(key, 0) > 30:   # 同一台、同一原因 30 秒只记一次
            self._last[key] = time.time()
            log_event("TLS", "%s 连接被拒：%s" % (addr[0], reason or exc))

    def _redirect_http(self, raw):
        try:
            head = raw.recv(4096).decode("latin-1", "replace")
            path = head.split(" ", 2)[1] if head.count(" ") >= 2 else "/"
            host = ""
            for line in head.split("\r\n")[1:]:
                if line.lower().startswith("host:"):
                    host = line.split(":", 1)[1].strip()
            if not host:
                host = "%s:%d" % (lan_ip(), HTTPS_PORT)
            if not path.startswith("/"):
                path = "/"
            raw.sendall(("HTTP/1.1 301 Moved Permanently\r\nLocation: https://%s%s\r\nContent-Length: 0\r\n"
                         "Connection: close\r\n\r\n" % (host, path)).encode("latin-1", "replace"))
        except OSError:
            pass

    def finish_request(self, raw, addr):
        sock = None
        try:
            raw.settimeout(15)
            first = raw.recv(1, socket.MSG_PEEK)
            if not first:
                return
            if first != b"\x16":   # 不是 TLS 握手：多半是少打了 https:// 的普通 http 请求
                self._redirect_http(raw)
                return
            sock = self.t9_ctx.wrap_socket(raw, server_side=True, do_handshake_on_connect=False)
            sock.do_handshake()
            sock.settimeout(None)
        except (ssl.SSLError, OSError) as exc:
            self._note(addr, exc)
            if sock:
                sock.close()
            return
        try:
            super().finish_request(sock, addr)
        finally:
            try:
                sock.close()
            except OSError:
                pass


def _ensure_certs(ip, force=False):
    try:
        import certs
    except ImportError as exc:   # 缺少 cryptography 时退回旧方式：用现成证书
        return "无法自动维护证书（%s）" % exc
    try:
        if certs.ensure_server_cert(CERT_DIR, [ip], force=force):
            log_event("CERT", "已按电脑地址 %s 签发证书" % ip)
    except Exception as exc:
        return "证书生成失败：%s" % exc
    return None


def start_https(handler, force_cert=False):
    """局域网 HTTPS：本机根证书签发，手机信任一次根证书后即为安全上下文。"""
    ip = lan_ip()
    _https["error"] = _ensure_certs(ip, force_cert)
    crt = os.path.join(CERT_DIR, "server.crt")
    key = os.path.join(CERT_DIR, "server.key")
    if not (os.path.exists(crt) and os.path.exists(key)):
        _https["error"] = _https["error"] or "缺少证书"
        log_event("ERROR", "HTTPS 未启用：%s" % _https["error"])
        return
    for old in [_https["srv"]] + _https.get("legacy", []):
        if old:
            old.shutdown()
            old.server_close()
    _https["legacy"] = []
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(crt, key)
    if _legacy["on"]:   # 升级前连过的手机还记着旧端口：旧端口上也开一份，它们连上后会被引导到新端口
        try:
            lg = _TLSServer(("0.0.0.0", LEGACY_HTTPS_PORT), handler)
            lg.daemon_threads, lg.t9_handler, lg.t9_ctx = True, handler, ctx
            threading.Thread(target=lg.serve_forever, daemon=True).start()
            _https["legacy"].append(lg)
        except OSError:
            pass
    try:
        httpd = _TLSServer(("0.0.0.0", HTTPS_PORT), handler)
    except OSError:
        _https["error"] = "端口 %d 被别的程序占用了：关掉那个程序或重启电脑后再打开 Open Your Eyes" % HTTPS_PORT
        log_event("ERROR", _https["error"])
        return
    httpd.daemon_threads = True
    httpd.t9_handler = handler
    httpd.t9_ctx = ctx   # 监听用原始套接字，每个连接在自己的线程里再加密
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    _https.update({"srv": httpd, "ip": ip})
    print("HTTPS https://%s:%d/" % (ip, HTTPS_PORT), flush=True)


_legacy = {"on": False}


def _legacy_wanted():
    """早期版本用 8080 / 8443：升级过来的电脑（已经有手机或设置）继续兼容旧端口；全新安装不占旧端口。"""
    return PORT == DEFAULT_PORT and (os.path.exists(PHONES_FILE) or os.path.exists(PREFS_FILE))


def _watch_network(handler):
    while True:
        time.sleep(10)
        ip = lan_ip()
        if ip != _https["ip"] and ip != "127.0.0.1":
            log_event("NET", "电脑地址从 %s 变为 %s，已自动更新证书；手机需要用新地址重新打开拍照页" % (_https["ip"], ip))
            try:
                start_https(handler)
            except Exception as exc:
                log_event("ERROR", "HTTPS 重启失败：%s" % exc)


_update = {}   # 最近一次查到的新版本信息


def _ver_tuple(v):
    try:
        return tuple(int(x) for x in str(v).split(".")[:3])
    except ValueError:
        return (0,)


def check_update_once():
    """去发布地址看一眼有没有新版本；网络不通就算了，下次再查。"""
    import urllib.request
    if not UPDATE_FEED:
        return None
    try:
        req = urllib.request.Request(UPDATE_FEED, headers={"User-Agent": "OpenYourEyes/%s" % VERSION})
        with urllib.request.urlopen(req, timeout=15) as r:
            j = json.loads(r.read(65536).decode("utf-8"))
        v, page = str(j.get("version") or ""), str(j.get("page") or "")
        if re.match(r"^\d+\.\d+\.\d+$", v) and page.startswith("https://"):
            _update.clear()
            _update.update({"version": v, "page": page, "notes": str(j.get("notes") or "")[:200]})
            if _ver_tuple(v) > _ver_tuple(VERSION):
                log_event("UPDATE", "有新版本 %s" % v)
        return dict(_update)
    except Exception as exc:
        print("检查新版本失败：%s" % exc)
        return None


def _update_loop():
    time.sleep(60)   # 别和启动抢网络
    while True:
        check_update_once()
        time.sleep(12 * 3600)


def settings_snapshot():
    ip = _https.get("ip") or ("" if _https.get("starting") else lan_ip())   # 还在等系统允许访问局域网时，不去碰网络
    now = time.time()
    with _req_cv:
        online_ips = {ph.get("ip") for ph in _phones.values() if _phone_online(ph, now)}
    try:
        import certs
        cert = certs.cert_info(CERT_DIR)
        cert["ok"], cert["reason"] = certs.server_cert_status(CERT_DIR, [ip])
    except Exception as exc:
        cert = {"ok": False, "reason": "无法检查证书（%s）" % exc}
    try:
        import autostart
        auto = autostart.status() if PORT == DEFAULT_PORT else {"enabled": False, "error": "测试实例"}
    except Exception as exc:
        auto = {"enabled": False, "error": str(exc)}
    return {
        "ok": True, "version": VERSION, "lanIp": ip, "ipCandidates": ip_candidates(),
        "phoneUrl": "https://%s:%d/" % (ip, HTTPS_PORT),
        "setupUrl": "http://%s:%d/trust.html#k=%s" % (ip, PORT, pair_key()),
        "setupShown": "%s:%d" % (ip, PORT),   # 二维码下面显示的文字：不带配对密钥
        "ai": {"lastSeenAgoS": round(time.time() - _ai["t"]) if _ai["t"] else None, "client": _ai["client"],
               "url": "http://127.0.0.1:%d/mcp" % PORT, "extension": os.path.exists(os.path.join(ROOT, "app", "open-your-eyes.mcpb")),
               "list": ai_list(),
               "clients": sorted(({"name": k, "agoS": round(time.time() - v)} for k, v in _ai_clients.items()),
                                 key=lambda x: x["agoS"])},
        "photosTaken": bool(_last_photo),
        "appLatest": (app_info() or {}).get("versionName"),
        "update": _update if _update.get("version") and _ver_tuple(_update["version"]) > _ver_tuple(VERSION) else None,
        "https": {"running": bool(_https["srv"]), "ip": _https["ip"], "error": _https["error"], "starting": bool(_https.get("starting"))},
        "phones": phones_status(), "prefs": dict(_prefs), "photos": photos_summary(),
        "cert": cert, "autostart": auto, "events": list(_events)[-30:],
        # 最近 10 分钟拒绝过证书的设备：多半只是点过「访问此网站」临时放行，没有真正信任根证书
        # 已经在线的手机除外：有的浏览器（如 vivo 浏览器）不认手机里装的证书，用户点过「继续浏览」后照样能用
        "untrusted": sorted(ip for ip, t in _TLSServer.rejects.items() if time.time() - t < 600 and ip not in online_ips
                           and not ip.startswith("127.") and ip != "::1"),   # 电脑自己不是手机
    }


# ---------------- Claude 插件接口（MCP，Streamable HTTP）：插件只写一个本机地址，用户电脑上不需要 Python ----------------
_mcp_sessions = {}   # 会话编号 → {"last": 本对话最近的照片, "t": 最后活动时间}
_ai = {"t": 0.0, "client": None}   # 最近一次有 AI 来连（设置页显示「AI 已连接」）
AI_FILE = os.path.join(SNAP_DIR, ".ai.json")


def _load_ai():
    try:
        with open(AI_FILE, encoding="utf-8") as f:
            return {str(k): float(v) for k, v in json.load(f).items()}
    except (OSError, ValueError, AttributeError):
        return {}


_ai_clients = _load_ai()   # AI 的名字 → 最后一次来连的时间（设置页分别显示每个 AI；存盘，重启后还记得）
_ai_saved = [0.0]
_ai_ver = {}   # AI 的名字 → 它最近一次连上时拿到的 Open Your Eyes 版本（设置页显示，旧了就提示新开对话）
try:
    with open(AI_FILE + ".ver", encoding="utf-8") as _f:
        _ai_ver.update(json.load(_f))
except (OSError, ValueError):
    pass


def _note_ai(name):
    new = name not in _ai_clients
    _ai_clients[name] = time.time()
    if new or time.time() - _ai_saved[0] > 300:
        _ai_saved[0] = time.time()
        try:
            os.makedirs(SNAP_DIR, exist_ok=True)
            _write_atomic(AI_FILE, json.dumps(_ai_clients, ensure_ascii=False).encode("utf-8"))
        except OSError:
            pass
AI_NAMES = {"claude-ai": "Claude 桌面版", "claude-code": "Claude Code", "claude code": "Claude Code", "codex": "Codex"}


def _ai_name(raw):
    raw = (raw or "").strip()
    low = raw.lower()
    if low in AI_NAMES:
        return AI_NAMES[low]
    if "codex" in low:
        return "Codex"
    if "workbuddy" in low or "codebuddy" in low:
        return "WorkBuddy"
    if "claude" in low and "code" in low:
        return "Claude Code"
    return raw or "未知的 AI"


_ai_list_cache = [0.0, None]


def ai_list():
    """电脑上的 AI 和接入情况（读几个设置文件，缓存 8 秒），再加上「最近连没连上」。"""
    if not _ai_list_cache[1] or time.time() - _ai_list_cache[0] > 8:
        try:
            import integrations
            _ai_list_cache[:] = [time.time(), integrations.status()]
        except Exception:
            _ai_list_cache[:] = [time.time(), []]
    out = []
    for a in _ai_list_cache[1]:
        seen = _ai_clients.get(a["name"])
        out.append(dict(a, agoS=round(time.time() - seen) if seen else None, ver=_ai_ver.get(a["name"])))
    return out
MCP_SESSION_TTL = 7 * 86400


def mcp_session(sid, create=False):
    now = time.time()
    with _req_cv:
        for k in [k for k, v in _mcp_sessions.items() if now - v["t"] > MCP_SESSION_TTL]:
            _mcp_sessions.pop(k, None)
        if create:
            sid = secrets.token_hex(16)
            _mcp_sessions[sid] = {"last": [], "t": now}
        st = _mcp_sessions.get(sid or "")
        if st:
            st["t"] = now
        return sid, st


LOG_DIR = os.path.abspath(os.environ.get("T9_LOG_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs"))


def diagnostics_zip():
    """「导出诊断」：运行记录打包，出问题时发给开发者。不含照片、配对密钥、网卡地址。"""
    import io
    import platform
    import zipfile
    snap = settings_snapshot()
    snap.pop("setupUrl", None)
    for ph in snap.get("phones", []):
        ph.pop("lastPhoto", None)
    info = {"version": VERSION, "time": time.strftime("%Y-%m-%d %H:%M:%S"), "platform": platform.platform(),
            "python": sys.version.split()[0], "frozen": bool(getattr(sys, "frozen", False)),
            "ports": {"http": PORT, "https": HTTPS_PORT}, "settings": snap,
            "phonesFile": {pid: {k: v for k, v in (_meta.get(pid) or {}).items() if k != "mac"} for pid in _names}}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("info.json", json.dumps(info, ensure_ascii=False, indent=1))
        for n in ("web.log", "app.log"):
            fp = os.path.join(LOG_DIR, n)
            try:
                with open(fp, "rb") as f:
                    f.seek(max(0, os.path.getsize(fp) - 2 * 1024 * 1024))
                    z.writestr(n, f.read())
            except OSError:
                pass
    return buf.getvalue()


def qr_svg(text):
    import io
    import qrcode
    import qrcode.image.svg
    img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue()


LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]")

# 拍照页版本：页面里的 __T9_BUILD__ 在发给手机时换成内容指纹；手机轮询时比对，发现电脑上有新版就自动刷新，
# 用户永远不用手动刷新手机（之前实测某台安卓一直跑着旧页面）
_build = {"mtime": None, "id": None, "html": None}


def page_build():
    path = os.path.join(ROOT, "index.html")
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return None, None
    if mt != _build["mtime"]:
        import hashlib
        with open(path, "rb") as f:
            raw = f.read()
        bid = hashlib.sha1(raw).hexdigest()[:10]
        _build.update({"mtime": mt, "id": bid,
                       "html": raw.replace(b"__T9_BUILD__", bid.encode()).replace(b"__T9_VERSION__", VERSION.encode())})
    return _build["id"], _build["html"]


class Handler(http.server.SimpleHTTPRequestHandler):
    extensions_map = {
        **http.server.SimpleHTTPRequestHandler.extensions_map,
        ".apk": "application/vnd.android.package-archive",
        ".mcpb": "application/zip",
        ".webmanifest": "application/manifest+json",
        ".js": "text/javascript",
        ".svg": "image/svg+xml",
        ".json": "application/json",
    }

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")   # 接口回包绝不能被缓存（否则旧拍照任务会被重放）
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if DEBUG_POLL and not path.startswith("/api/"):
            print("GET %s %s cache=%s ims=%s sw=%s" % (self.client_address[0], self.path[:60], self.headers.get("Cache-Control"),
                  self.headers.get("If-Modified-Since"), self.headers.get("Service-Worker")), flush=True)
        if path == "/config.json":
            info = {}
            try:
                import certs
                info = certs.cert_info(CERT_DIR)
            except Exception:
                pass
            self._json(200, {"lanIp": lan_ip(), "httpsPort": HTTPS_PORT, "caName": info.get("caName"),
                             "caSha256": info.get("caSha256"), "app": app_info()})
            return
        q = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
        if path == "/mcp":   # 不提供服务器推送流
            self.send_response(405)
            self.send_header("Allow", "POST, DELETE")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/api/phone/poll":
            arg = lambda k: (q.get(k) or [""])[0]
            try:
                hold = float(arg("hold") or 0)
            except ValueError:
                hold = 0.0
            t0 = time.time()
            pid, name, req, live = phone_poll(arg("id"), arg("name"), self.client_address[0], self.headers.get("User-Agent"),
                                              arg("vis") == "1", hold, arg("facing"), arg("torch"), arg("state"), arg("live"),
                                              page=arg("page"),
                                              paired=key_ok(self.headers.get("X-T9-Key")), app=arg("app") == "1")
            extra = {"ports": {"http": PORT, "https": HTTPS_PORT}}
            if (self.server.server_address[1] == LEGACY_HTTPS_PORT and live is not None and not req
                    and arg("app") != "1" and phone_ok(pid)):
                # 旧端口上的网页拍照页：带着编号和配对密钥搬到新端口（网页的本地存储按端口分开，不带上就成了新手机）
                extra["moveTo"] = "https://%s:%d/#k=%s&id=%s" % (self.headers.get("Host", "").rsplit(":", 1)[0] or lan_ip(),
                                                                 HTTPS_PORT, pair_key(), pid)
            try:
                if live is None:
                    self._json(200, dict(extra, ok=True, id=pid, name=name, pending=True, build=page_build()[0]))
                    return
                self._json(200, dict(extra, ok=True, id=pid, name=name, request=req, live=live, build=page_build()[0],
                                     coverReq=_cover_req[0]))
                err = ""
            except OSError as exc:
                err = " 回包失败:%s" % exc
                raise
            finally:
                if DEBUG_POLL or req:
                    print("POLL %s page=%s vis=%s state=%s have=%s held=%.2fs req=%s live=%s%s" % (
                          name, arg("page"), arg("vis"), arg("state"), arg("live")[:12], time.time() - t0,
                          req and req["id"], ",".join(x["sid"][:6] for x in live), err), flush=True)
            return
        if path == "/api/live/mjpeg":   # 设置页看安卓 App 的实时画面（本机专用）
            if not self._local_only():
                return
            sid = (q.get("sid") or [""])[0]
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=t9frame")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            seq = 0
            try:
                while True:
                    jpg, seq = live_next_frame(sid, seq)
                    if seq < 0:
                        break
                    if jpg is None:
                        continue
                    self.wfile.write(b"--t9frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(jpg))
                    self.wfile.write(jpg)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
            except OSError:
                pass
            self.close_connection = True
            return
        if path == "/api/live/answer":   # 手机取应答（凭会话编号，编号是 128 位随机数，猜不到）
            try:
                wait = min(float((q.get("wait") or ["0"])[0] or 0), 15)
            except ValueError:
                wait = 0.0
            self._json(200, dict(live_wait_answer((q.get("sid") or [""])[0], wait), ok=True))
            return
        if path in ("/settings", "/settings.html", "/api/settings", "/qr.svg", "/api/status", "/api/request"):
            if not self._local_only():
                return
        if path == "/settings":
            self.send_response(302)
            self.send_header("Location", "/settings.html" + ("?" + self.path.split("?", 1)[1] if "?" in self.path else ""))
            self.end_headers()
            return
        if path == "/api/diagnostics.zip":
            if not self._local_only():
                return
            data = diagnostics_zip()
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", 'attachment; filename="OpenYourEyes-diagnostics-%s.zip"' % time.strftime("%Y%m%d-%H%M"))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if path.startswith("/cover/"):
            if not self._local_only():
                return
            fp = cover_path(path[len("/cover/"):])
            if not fp or not os.path.exists(fp):
                self._json(404, {"ok": False, "error": "not found"})
                return
            with open(fp, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Cache-Control", "private, max-age=86400")   # 网址里带时间戳，换了新封面网址就变
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if path.startswith("/photo/"):
            if not self._local_only():
                return
            fn = path[len("/photo/"):]
            import re as _re
            fp = os.path.join(SNAP_DIR, fn)
            if not _re.match(r"^[0-9]{8}-[0-9]{6}-[0-9]{3}(_[0-9]+)?(\.view|\.f[0-9]+)?\.jpg$", fn) or not os.path.exists(fp):
                self._json(404, {"ok": False, "error": "not found"})
                return
            with open(fp, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Cache-Control", "private, max-age=3600")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if path == "/api/settings":
            self._json(200, settings_snapshot())
            return
        if path == "/qr.svg":
            svg = qr_svg((q.get("text") or [""])[0][:300])
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(svg)))
            self.end_headers()
            self.wfile.write(svg)
            return
        if path == "/api/request":
            rid = (q.get("id") or [""])[0]
            wait = min(float((q.get("wait") or ["0"])[0] or 0), 60)
            self._json(200, wait_request(rid, wait))
            return
        if path == "/api/status":
            self._json(200, {"ok": True, "phones": phones_status(), "snapDir": SNAP_DIR})
            return
        if path == "/ca.crt":
            # 只发根证书（公钥），私钥永不出本机。iOS Safari 下载后提示「已下载描述文件」。
            try:
                with open(os.path.join(CERT_DIR, "ca.crt"), "rb") as f:
                    pem = f.read()
            except OSError:
                self._json(404, {"ok": False, "error": "no ca.crt"})
                return
            der = ssl.PEM_cert_to_DER_cert(pem.decode("ascii"))
            self.send_response(200)
            self.send_header("Content-Type", "application/x-x509-ca-cert")
            self.send_header("Content-Disposition", 'attachment; filename="OpenYourEyes-CA.crt"')
            self.send_header("Content-Length", str(len(der)))
            self.end_headers()
            self.wfile.write(der)
            return
        if path == "/" and not isinstance(self.server, _TLSServer):
            # 普通地址（http）打不开拍照页（网页用摄像头必须加密）：本机去设置页；手机去入口页，
            # 入口页先检查这台手机认不认电脑的证书，认就跳到拍照页，不认就教它重新信任，而不是停在浏览器报错上
            self.send_response(302)
            self.send_header("Location", "/settings" if self.client_address[0] in ("127.0.0.1", "::1") else "/trust.html")
            self.end_headers()
            return
        if path in ("/", "/index.html"):
            bid, html = page_build()
            if html is not None:
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Length", str(len(html)))
                self.end_headers()
                self.wfile.write(html)
                return
        return super().do_GET()

    def _save_snap(self):
        """手机上传：单张照片 {meta, jpegBase64}（全尺寸原图）或连拍 {meta, frames:[jpegBase64...]}。

        单张：存原图 name.jpg + 概览图 name.view.jpg（给 Claude 看全貌，局部细节用裁剪从原图取）。
        连拍：存 name.f1.jpg ... name.fN.jpg。
        """
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > SNAP_MAX:
            self._json(400, {"ok": False, "error": "bad size %d" % length})
            return
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        client = body.get("meta") or {}
        rid = client.get("requestId")
        pid = client.get("phoneId")
        if not request_open(rid, pid):
            # 只收 Claude 请求的照片：没有请求、请求已超时或已取消，一律不收
            log_event("REJECT", "拒收照片 request=%s phone=%s from %s" % (rid, pid, self.client_address[0]))
            self._json(409, {"ok": False, "error": "没有进行中的拍照请求"})
            return
        frames = [base64.b64decode(b) for b in (body.get("frames") or [])]
        single = None if frames else base64.b64decode(body.get("jpegBase64") or "")
        for d in (frames or [single]):
            if not d.startswith(b"\xff\xd8"):
                self._json(400, {"ok": False, "error": "not a jpeg"})
                return
        os.makedirs(SNAP_DIR, exist_ok=True)
        now = time.time()
        name = time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + "-%03d" % int(now % 1 * 1000)
        with _req_cv:   # 多台手机可能在同一毫秒上传，名字要唯一
            k = 1
            while os.path.exists(os.path.join(SNAP_DIR, name + ".json")) or os.path.exists(os.path.join(SNAP_DIR, name + ".jpg")):
                k += 1
                name = name.split("_")[0] + "_%d" % k
            open(os.path.join(SNAP_DIR, name + ".json"), "a").close()   # 先占位
        if frames:
            for i, d in enumerate(frames, 1):
                _write_atomic(os.path.join(SNAP_DIR, "%s.f%d.jpg" % (name, i)), d)
            data = frames[0]
        else:
            data = single
            _write_atomic(os.path.join(SNAP_DIR, name + ".jpg"), data)
            make_view(name)
        img = client.get("image") or {}
        cam = client.get("camera") or {}
        meta = {
            "file": name + (".f1.jpg" if frames else ".jpg"),
            "kind": "clip" if frames else "photo",
            "frames": len(frames) or None,
            "clipSeconds": client.get("clipSeconds"),
            "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
            "facing": cam.get("facing"),
            "size": "%sx%s" % (img.get("width") or "?", img.get("height") or "?"),
            "bytes": sum(len(d) for d in (frames or [single])),
            # 拍摄瞬间（浏览器侧）：姿态 / 防抖 / 相机参数 / 画面质量 / 电池 / 网络
            "pose": client.get("pose"),
            "stability": client.get("stability"),
            "image": img or None,
            "camera": cam or None,
            "client": {k: v for k, v in client.items()
                       if k not in ("pose", "stability", "image", "camera")} or None,
        }
        with _req_cv:
            r = _requests.get(rid)
            meta["request"] = {"id": rid, "what": r["what"] if r else None}
            meta["phone"] = {"id": pid, "name": (_phones.get(pid) or {}).get("name")}
        meta["trigger"] = "request"
        with open(os.path.join(SNAP_DIR, name + ".json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=1)
        delivered = on_snap_saved(name, rid, pid)
        if delivered:
            _last_photo[pid] = name + (".f1.jpg" if frames else ".view.jpg")
        print("SNAP %s %s %s %d bytes request=%s phone=%s%s" % (meta["kind"], name, meta["size"], meta["bytes"], rid,
                                                                 meta["phone"]["name"],
                                                        "" if delivered else "（请求已结束）"), flush=True)
        self._json(200, {"ok": True, "file": meta["file"]})

    def _read_json(self, limit=65536):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(min(length, limit)).decode("utf-8") or "{}")
        except Exception:
            return {}

    def _local_only(self, write=False):
        """拍照请求、设置等只允许本机程序调用：局域网里的其他设备不能代替用户发起拍照或改设置。
        另外防浏览器里的恶意网页借道：Host 必须是本机名（防 DNS 重绑定）；写操作必须带自定义头 X-T9
        （跨站网页带不了自定义头而不触发预检，我们也不响应预检），且 Origin 若有必须是本机。"""
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        origin = self.headers.get("Origin")
        ok = self.client_address[0] in ("127.0.0.1", "::1") and host in LOCAL_HOSTS
        if ok and write:
            ok = self.headers.get("X-T9") == "1" and (
                not origin or urllib.parse.urlparse(origin).hostname in ("127.0.0.1", "localhost", "::1"))
        if ok:
            return True
        self._json(403, {"ok": False, "error": "local only"})
        return False

    def _mcp_allowed(self):
        """只给本机的 Claude 用；带了 Origin 的（浏览器里的网页）必须也是本机，防网页借道。"""
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        origin = self.headers.get("Origin")
        ok = self.client_address[0] in ("127.0.0.1", "::1") and host in LOCAL_HOSTS and (
            not origin or urllib.parse.urlparse(origin).hostname in ("127.0.0.1", "localhost", "::1"))
        if not ok:
            self._json(403, {"jsonrpc": "2.0", "id": None, "error": {"code": -32000, "message": "local only"}})
        return ok

    def _mcp_post(self):
        import eyes_mcp
        eyes_mcp.API = "http://127.0.0.1:%d" % PORT
        length = int(self.headers.get("Content-Length") or 0)
        try:
            msg = json.loads(self.rfile.read(min(length, 1 << 20)).decode("utf-8"))
        except Exception:
            self._json(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
            return
        batch = isinstance(msg, list)
        msgs = msg if batch else [msg]
        sid = self.headers.get("Mcp-Session-Id")
        new_sid = None
        _ai["t"] = time.time()
        for m in msgs:
            if isinstance(m, dict) and m.get("method") == "initialize":
                ci = (m.get("params") or {}).get("clientInfo") or {}
                _ai["client"] = _ai_name(str(ci.get("title") or ci.get("name") or "")[:60])
                _note_ai(_ai["client"])
                _ai_ver[_ai["client"]] = VERSION
                try:
                    _write_atomic(AI_FILE + ".ver", json.dumps(_ai_ver, ensure_ascii=False).encode("utf-8"))
                except OSError:
                    pass
        if _ai["client"] in _ai_clients:
            _note_ai(_ai["client"])
        if any(isinstance(m, dict) and m.get("method") == "initialize" for m in msgs):
            new_sid, st = mcp_session(None, create=True)
            st["client"] = _ai["client"]   # 记住这个会话是哪个 AI（Codex 看不到照片，要多给一行原图链接）
        else:
            _, st = mcp_session(sid)
            if st is None:   # 电脑端程序重启过、会话没了：沿用这个编号接着用（只是忘了上次的照片），不让 Claude 那边断掉
                with _req_cv:
                    st = _mcp_sessions.setdefault(sid or "default", {"last": [], "t": time.time()})
        # 认不出是哪个 AI 时（比如本程序重启过、对方还用着老连接），看请求自带的客户端标识；只认得出 Codex 时才用
        ua_name = _ai_name((self.headers.get("User-Agent") or "").split("/")[0])
        if ua_name == "Codex":
            if not st.get("client"):
                st["client"] = "Codex"
            _note_ai("Codex")
        outs = [o for o in (eyes_mcp.respond(m, st) for m in msgs if isinstance(m, dict)) if o is not None]
        if not outs:
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = json.dumps(outs if batch else outs[0], ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        if new_sid:
            self.send_header("Mcp-Session-Id", new_sid)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_DELETE(self):
        if self.path.split("?", 1)[0] == "/mcp" and self._mcp_allowed():
            with _req_cv:
                _mcp_sessions.pop(self.headers.get("Mcp-Session-Id") or "", None)
            self._json(200, {"ok": True})
            return
        self._json(405, {"ok": False})

    def do_POST(self):
        p = self.path.split("?", 1)[0]
        if p == "/mcp":
            if self._mcp_allowed():
                self._mcp_post()
            return
        if p in ("/api/live/start", "/api/live/ping", "/api/live/answer", "/api/live/stop"):
            if not self._local_only(write=True):
                return
            body = self._read_json(65536)
            try:
                if p == "/api/live/start":
                    self._json(200, {"ok": True, "sid": live_start(str(body.get("phone")), body.get("q"))})
                elif p == "/api/live/ping":
                    self._json(200, dict(live_ping(str(body.get("sid")), body.get("q")), ok=True))
                elif p == "/api/live/answer":
                    self._json(200, {"ok": live_answer(str(body.get("sid")), body.get("sdp") or "")})
                else:
                    live_stop(str(body.get("sid")))
                    self._json(200, {"ok": True})
            except Exception as exc:
                self._json(400, {"ok": False, "error": str(exc)})
            return
        if p == "/api/live/frame":   # 安卓 App 交实时画面（凭会话编号 + 手机编号）
            qs = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            length = int(self.headers.get("Content-Length") or 0)
            jpg = self.rfile.read(length) if 0 < length <= FRAME_MAX else b""
            ok = jpg.startswith(b"\xff\xd8") and live_frame((qs.get("sid") or [""])[0], (qs.get("phone") or [""])[0], jpg)
            self._json(200 if ok else 410, {"ok": bool(ok)})
            return
        if p == "/api/live/offer":   # 手机交连接邀请
            body = self._read_json(65536)
            self._json(200, {"ok": live_offer(str(body.get("sid")), str(body.get("phone")), body.get("sdp") or "")})
            return
        if p == "/api/ai/install":   # 一键把 Open Your Eyes 接进某个 AI
            if not self._local_only(write=True):
                return
            body = self._read_json()
            try:
                import integrations
                ok, msg = integrations.install(str(body.get("id")), "http://127.0.0.1:%d/mcp" % PORT,
                                               os.path.join(ROOT, "app", "open-your-eyes.mcpb"))
            except Exception as exc:
                ok, msg = False, "安装出错：%s" % exc
            _ai_list_cache[0] = 0.0
            log_event("AI", "%s：%s" % (body.get("id"), msg))
            self._json(200, {"ok": ok, "msg": msg})
            return
        if p == "/api/phones/refresh":   # 设置页「刷新画面」
            if not self._local_only(write=True):
                return
            refresh_covers()
            self._json(200, {"ok": True})
            return
        if p in ("/api/phones/approve", "/api/phones/rename", "/api/phones/remove", "/api/prefs", "/api/photos/clear", "/api/autostart", "/api/repair"):
            if not self._local_only(write=True):
                return
            body = self._read_json()
            try:
                if p == "/api/phones/approve":
                    approve_phone(str(body.get("id")))
                elif p == "/api/phones/rename":
                    rename_phone(str(body.get("id")), body.get("name"))
                elif p == "/api/phones/remove":
                    remove_phone(str(body.get("id")))
                elif p == "/api/prefs":
                    save_prefs(body)
                elif p == "/api/photos/clear":
                    log_event("CLEAN", "已手动清空 %d 张照片" % delete_photos())
                elif p == "/api/autostart":
                    if PORT != DEFAULT_PORT:   # 测试 / 开发实例不碰系统的开机自启（那是装好的程序的）
                        raise ValueError("测试实例不能设置开机自启")
                    import autostart
                    (autostart.enable if body.get("on") else autostart.disable)()
                elif p == "/api/repair":
                    log_event("REPAIR", "手动修复：重新签发证书并重启 HTTPS")
                    start_https(self.server.t9_handler, force_cert=True)
            except Exception as exc:
                self._json(400, {"ok": False, "error": str(exc)})
                return
            self._json(200, settings_snapshot())
            return
        if p == "/api/request":
            if not self._local_only(write=True):
                return
            body = self._read_json()
            rid, err = create_request(body.get("what") or "", body.get("kind") or "photo",
                                      body.get("seconds"), body.get("frames"), body.get("phone"), body.get("light"))
            self._json(200, {"ok": True, "id": rid} if rid else {"ok": False, "error": err})
            return
        if p == "/api/phone/cover":
            body = self._read_json(COVER_MAX * 2)
            try:
                jpg = base64.b64decode(body.get("jpegBase64") or "")
            except Exception:
                jpg = b""
            ok = save_cover(str(body.get("id") or ""), self.client_address[0], jpg)
            self._json(200 if ok else 400, {"ok": ok})
            return
        if p == "/api/phone/log":
            body = self._read_json(4096)
            if not phone_ok(str(body.get("id"))):
                self._json(403, {"ok": False})
                return
            with _req_cv:
                who = (_phones.get(str(body.get("id"))) or {}).get("name") or self.client_address[0]
            log_event("PHONE", "%s: %s" % (who, str(body.get("msg"))[:300]))
            self._json(200, {"ok": True})
            return
        if p == "/api/crop":
            if not self._local_only(write=True):
                return
            body = self._read_json()
            try:
                jpg = crop_original(body.get("name") or "", [body.get(k) for k in ("x", "y", "w", "h")])
            except Exception as exc:
                self._json(400, {"ok": False, "error": str(exc)})
                return
            self._json(200, {"ok": True, "jpegBase64": base64.b64encode(jpg).decode("ascii")})
            return
        if p == "/api/request/cancel":
            if not self._local_only(write=True):
                return
            cancel_request(self._read_json().get("id"))
            self._json(200, {"ok": True})
            return
        if self.path.split("?", 1)[0] == "/api/snap":
            try:
                self._save_snap()
            except Exception as exc:
                self._json(500, {"ok": False, "error": str(exc)})
            return
        self._json(404, {"ok": False, "error": "not found"})

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        # sw.js 位于根目录，显式声明作用域，避免不同 Chrome 版本的差异
        if self.path.split("?", 1)[0].endswith("sw.js"):
            self.send_header("Service-Worker-Allowed", "/")
            self.send_header("Cache-Control", "no-cache")
        else:
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt, *args):
        pass


def _power_loop():
    """有已允许的手机在线就不让电脑闲置睡眠。"""
    import power
    while True:
        now = time.time()
        with _req_cv:
            any_on = any(_phone_online(ph, now) for ph in _phones.values())
        power.hold(any_on)
        time.sleep(20)


def _port_owner_is_us(port):
    try:
        import urllib.request
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with op.open("http://127.0.0.1:%d/api/status" % port, timeout=2) as r:
            return "phones" in json.load(r)
    except Exception:
        return False


EXIT_PORT_BUSY = 3


def main():
    handler = functools.partial(Handler, directory=ROOT)
    os.makedirs(SNAP_DIR, exist_ok=True)
    try:
        server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), handler)
    except OSError:
        if _port_owner_is_us(PORT):
            print("Open Your Eyes 已经在运行了", flush=True)
            sys.exit(0)
        print("PORT_BUSY 端口 %d 被别的程序占用了：请关掉占用它的程序（或重启电脑）后再打开 Open Your Eyes" % PORT, flush=True)
        sys.exit(EXIT_PORT_BUSY)
    _legacy["on"] = _legacy_wanted()
    if _legacy["on"]:
        try:
            lsrv = http.server.ThreadingHTTPServer(("0.0.0.0", LEGACY_PORT), handler)
            lsrv.daemon_threads, lsrv.t9_handler = True, handler
            threading.Thread(target=lsrv.serve_forever, daemon=True).start()
            print("兼容旧端口 http://0.0.0.0:%d" % LEGACY_PORT, flush=True)
        except OSError:
            pass   # 旧端口被别的软件占着：不影响新端口
    _scan_last_photos()
    try:
        import autostart
        if PORT == DEFAULT_PORT and autostart.migrate():   # 只有正式运行的程序才动开机自启（测试用别的端口，不碰）
            log_event("APP", "已把开机自启换成新名字 Open Your Eyes")
    except Exception:
        pass
    if PORT == DEFAULT_PORT:   # 已接入 Codex：把它全局说明里我们那一段更新成最新版（规则改了也能自动跟上）
        try:
            import integrations
            if integrations._codex_installed():
                integrations.write_codex_agents()
        except Exception:
            pass
    # 手机连接放到后台开：新版 macOS 第一次访问局域网会弹「允许查找本地网络上的设备」，用户点之前这一步会卡住；
    # 不能让它挡住设置页（设置页会提示用户去点「允许」）
    _https["starting"] = True
    try:   # 根证书不需要联网，先生成好（手机教程页要用它的指纹）
        import certs
        certs.ensure_ca(CERT_DIR)
    except Exception:
        pass

    def _bg_https():
        try:
            start_https(handler)
        finally:
            _https["starting"] = False
    threading.Thread(target=_bg_https, daemon=True).start()
    server.daemon_threads = True
    server.t9_handler = handler
    threading.Thread(target=_watch_network, args=(handler,), daemon=True).start()
    threading.Thread(target=_cleanup_loop, daemon=True).start()
    threading.Thread(target=_power_loop, daemon=True).start()
    if UPDATE_FEED and PORT == DEFAULT_PORT:   # 测试实例不去查
        threading.Thread(target=_update_loop, daemon=True).start()
    print("Open Your Eyes 页面服务 %s http://0.0.0.0:%d（页面目录 %s）" % (VERSION, PORT, ROOT), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
