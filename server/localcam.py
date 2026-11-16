"""这台电脑自己的摄像头（内置摄像头、USB 摄像头）当作机位用。

做法：每个启用的摄像头在页面服务里跑一个「本机机位」线程，它和安卓 App 走完全一样的接口
（/api/phone/poll、/api/live/frame、/api/snap、/api/phone/cover），所以取景器、拍照、连拍、放大都不用另写。

隐私：
- 默认不用任何电脑摄像头；用户在设置页逐个打开。
- 摄像头只在需要时打开：AI 要拍照、取景器 / 设置页在看实时画面、用户点「刷新画面」。用完几秒就关，指示灯随之熄灭。
- 不像手机那样每分钟自动拍一张小图（那会让摄像头灯每分钟亮一下）。

采集方式：
- Mac：系统自带的 AVFoundation（通过 pyobjc，组件很小，Intel 和 Apple 芯片通用）。第一次用会弹系统授权框，要用户自己点「允许」。
- Windows / Linux：OpenCV。
"""
import base64
import hashlib
import json
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

IDLE_CLOSE_S = 6      # 秒：这么久没人用就关摄像头
SETTLE_S = 1.2        # 秒：摄像头刚打开时自动曝光、对焦要一会儿，先等等再拍
STALE_S = 3           # 秒：开着的摄像头这么久没有新画面，就当它断了，重新打开
LIVE_SIDE = {"low": 640, "high": 1280}
LIVE_FPS = {"low": 8, "high": 12}
COVER_SIDE = 360


class CamError(Exception):
    """给人看的错误（大白话）。"""


def cam_pid(uid):
    """摄像头在机位名单里的编号：固定由摄像头自己的标识算出来，拔了再插、程序重启都不变。"""
    return "pc" + hashlib.sha1(str(uid).encode("utf-8")).hexdigest()[:14]


# ---------------------------------------------------------------- Mac：AVFoundation

_mac_delegate = [None]


def _mac_delegate_class():
    if _mac_delegate[0]:
        return _mac_delegate[0]
    import objc
    import CoreMedia
    import Quartz
    from Foundation import NSObject

    class OYECamFrameSink(NSObject, protocols=[objc.protocolNamed("AVCaptureVideoDataOutputSampleBufferDelegate")]):
        def captureOutput_didOutputSampleBuffer_fromConnection_(self, output, sbuf, conn):
            owner = getattr(self, "owner", None)
            if owner is None:
                return
            with objc.autorelease_pool():
                pb = CoreMedia.CMSampleBufferGetImageBuffer(sbuf)
                if pb is None:
                    return
                owner._frame(Quartz.CIImage.imageWithCVPixelBuffer_(pb))

    _mac_delegate[0] = OYECamFrameSink
    return OYECamFrameSink


class _MacCam:
    def __init__(self, uid):
        import AVFoundation as AV
        import Quartz
        import libdispatch
        self._lock = threading.Lock()
        self._img, self._seq, self.started, self.last = None, 0, 0.0, 0.0
        dev = AV.AVCaptureDevice.deviceWithUniqueID_(uid)
        if dev is None:
            raise CamError("找不到这个摄像头（可能拔掉了）")
        self.name = str(dev.localizedName())
        inp, err = AV.AVCaptureDeviceInput.deviceInputWithDevice_error_(dev, None)
        if inp is None:
            raise CamError("打不开摄像头：%s" % (err.localizedDescription() if err else "可能正被别的程序占用"))
        s = AV.AVCaptureSession.alloc().init()
        for preset in ("AVCaptureSessionPreset3840x2160", "AVCaptureSessionPreset1920x1080", "AVCaptureSessionPresetHigh"):
            p = getattr(AV, preset, None)
            if p is not None and dev.supportsAVCaptureSessionPreset_(p) and s.canSetSessionPreset_(p):
                s.setSessionPreset_(p)
                break
        if not s.canAddInput_(inp):
            raise CamError("打不开摄像头：可能正被别的程序占用")
        s.addInput_(inp)
        out = AV.AVCaptureVideoDataOutput.alloc().init()
        out.setVideoSettings_({"PixelFormatType": 1111970369})   # kCVPixelFormatType_32BGRA
        out.setAlwaysDiscardsLateVideoFrames_(True)
        self._sink = _mac_delegate_class().alloc().init()
        self._sink.owner = self
        self._queue = libdispatch.dispatch_queue_create(b"openyoureyes.localcam", None)
        out.setSampleBufferDelegate_queue_(self._sink, self._queue)
        if not s.canAddOutput_(out):
            raise CamError("打不开摄像头的画面输出")
        s.addOutput_(out)
        self._session, self._out = s, out
        self._ctx = Quartz.CIContext.contextWithOptions_(None)
        self._cs = Quartz.CGColorSpaceCreateDeviceRGB()

    def _frame(self, img):
        with self._lock:
            self._img = img
            self._seq += 1
            self.last = time.time()

    def start(self):
        self.started = time.time()
        self._session.startRunning()

    def stop(self):
        """关摄像头：停下会话，并把输入、输出都拆下来——不拆的话旧会话一直占着这台摄像头，
        同一个程序里再开新会话就拿不到画面（实测：第一次能拍，关掉再开就一直没有画面）。"""
        s = self._session
        try:
            with objc_pool():
                s.stopRunning()
                self._out.setSampleBufferDelegate_queue_(None, None)
                s.beginConfiguration()
                for i in list(s.inputs() or []):
                    s.removeInput_(i)
                for o in list(s.outputs() or []):
                    s.removeOutput_(o)
                s.commitConfiguration()
        finally:
            self._sink.owner = None
            self._session = self._out = None
            with self._lock:
                self._img = None

    def frames(self):
        return self._seq

    def jpeg(self, max_side=None, quality=0.9):
        import Quartz
        with self._lock:
            img = self._img
        if img is None:
            return None
        with objc_pool():
            ext = img.extent()
            w, h = ext.size.width, ext.size.height
            if max_side and max(w, h) > max_side:
                k = float(max_side) / max(w, h)
                img = img.imageByApplyingTransform_(Quartz.CGAffineTransformMakeScale(k, k))
            data = self._ctx.JPEGRepresentationOfImage_colorSpace_options_(
                img, self._cs, {Quartz.kCGImageDestinationLossyCompressionQuality: quality})
            return bytes(data) if data is not None else None


class objc_pool:
    """Mac 上在后台线程里调系统接口，用完的对象要及时释放；别的系统上什么也不做。"""

    def __enter__(self):
        self._p = None
        if sys.platform == "darwin":
            try:
                import objc
                self._p = objc.autorelease_pool()
                self._p.__enter__()
            except Exception:
                self._p = None
        return self

    def __exit__(self, *a):
        if self._p is not None:
            self._p.__exit__(*a)


class _MacBackend:
    name = "mac"

    def __init__(self):
        import AVFoundation  # noqa: F401  缺组件就在这里失败，上层退回「不支持」

    def permission(self):
        import AVFoundation as AV
        st = AV.AVCaptureDevice.authorizationStatusForMediaType_(AV.AVMediaTypeVideo)
        return {0: "ask", 1: "denied", 2: "denied", 3: "ok"}.get(int(st), "ask")

    def request_permission(self, timeout=60):
        """弹系统的摄像头授权框（用户自己点）。返回是否允许。"""
        import AVFoundation as AV
        if self.permission() != "ask":
            return self.permission() == "ok"
        done = threading.Event()
        res = [False]

        def cb(granted):
            res[0] = bool(granted)
            done.set()
        AV.AVCaptureDevice.requestAccessForMediaType_completionHandler_(AV.AVMediaTypeVideo, cb)
        done.wait(timeout)
        return res[0]

    def list(self, probe=False):
        import AVFoundation as AV
        out = []
        with objc_pool():
            for d in AV.AVCaptureDevice.devicesWithMediaType_(AV.AVMediaTypeVideo) or []:
                out.append({"uid": str(d.uniqueID()), "name": str(d.localizedName())})
        return out

    def open(self, uid):
        return _MacCam(uid)


# ---------------------------------------------------------------- Windows / Linux：OpenCV

class _CvCam:
    def __init__(self, index, name):
        import cv2
        self._cv2 = cv2
        api = cv2.CAP_DSHOW if sys.platform == "win32" else cv2.CAP_V4L2 if sys.platform.startswith("linux") else cv2.CAP_ANY
        self._cap = cv2.VideoCapture(index, api)
        if not self._cap.isOpened():
            raise CamError("打不开摄像头：可能正被别的程序占用，或者拔掉了")
        for w, h in ((3840, 2160), (1920, 1080)):   # 尽量用摄像头支持的最高分辨率
            self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
            self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
            if self._cap.get(cv2.CAP_PROP_FRAME_WIDTH) >= w * 0.9:
                break
        self.name = name
        self._lock = threading.Lock()
        self._img, self._seq, self._run, self.started, self.last = None, 0, False, 0.0, 0.0

    def start(self):
        self.started = time.time()
        self._run = True
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        while self._run:
            ok, img = self._cap.read()
            if not ok:
                time.sleep(0.05)
                continue
            with self._lock:
                self._img = img
                self._seq += 1
                self.last = time.time()

    def stop(self):
        self._run = False
        time.sleep(0.1)
        self._cap.release()

    def frames(self):
        return self._seq

    def jpeg(self, max_side=None, quality=0.9):
        cv2 = self._cv2
        with self._lock:
            img = self._img
        if img is None:
            return None
        h, w = img.shape[:2]
        if max_side and max(w, h) > max_side:
            k = float(max_side) / max(w, h)
            img = cv2.resize(img, (int(w * k), int(h * k)), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, int(quality * 100)])
        return buf.tobytes() if ok else None


class _CvBackend:
    name = "opencv"

    def __init__(self):
        import cv2  # noqa: F401

    def permission(self):
        return "ok"

    def request_permission(self, timeout=60):
        return True

    def list(self, probe=False):
        """OpenCV 认不出摄像头的名字，只能按顺序试着打开（会让摄像头灯闪一下），所以只在用户点了「查找」时才试。"""
        if not probe:
            return list(_cv_seen)
        import cv2
        found = []
        api = cv2.CAP_DSHOW if sys.platform == "win32" else cv2.CAP_V4L2 if sys.platform.startswith("linux") else cv2.CAP_ANY
        for i in range(6):
            cap = cv2.VideoCapture(i, api)
            ok = cap.isOpened() and cap.read()[0]
            cap.release()
            if ok:
                found.append({"uid": "cv:%d" % i, "name": "电脑摄像头 %d" % (len(found) + 1)})
        _cv_seen[:] = found
        return found

    def open(self, uid):
        i = int(str(uid).split(":", 1)[1])
        name = next((c["name"] for c in _cv_seen if c["uid"] == uid), "电脑摄像头")
        return _CvCam(i, name)


class _FakeCam:
    """测试用：不碰真摄像头，生成一张纯色图。"""

    def __init__(self, uid):
        self.name, self._seq, self.started, self._run, self.last = "测试摄像头", 0, 0.0, False, 0.0

    def start(self):
        self.started, self._run = time.time(), True
        threading.Thread(target=self._tick, daemon=True).start()

    def _tick(self):
        while self._run:
            self._seq += 1
            self.last = time.time()
            time.sleep(0.03)

    def stop(self):
        self._run = False

    def frames(self):
        return self._seq

    def jpeg(self, max_side=None, quality=0.9):
        import io
        from PIL import Image
        side = max_side or 1280
        buf = io.BytesIO()
        Image.new("RGB", (side, side * 3 // 4), (40, 120, 200)).save(buf, "JPEG", quality=int(quality * 100))
        return buf.getvalue()


class _FakeBackend:
    name = "fake"

    def permission(self):
        return "ok"

    def request_permission(self, timeout=60):
        return True

    def list(self, probe=False):
        return [{"uid": "fake:0", "name": "测试摄像头"}]

    def open(self, uid):
        return _FakeCam(uid)


_cv_seen = []
_backend = [None, False]


def backend():
    """这台电脑能用的采集方式；都不行返回 None（设置页就不显示电脑摄像头）。"""
    if _backend[1]:
        return _backend[0]
    _backend[1] = True
    import os
    if os.environ.get("T9_FAKE_LOCALCAM") == "1":   # 自动测试用
        _backend[0] = _FakeBackend()
        return _backend[0]
    for cls in ((_MacBackend,) if sys.platform == "darwin" else (_CvBackend,)):
        try:
            _backend[0] = cls()
            break
        except Exception:
            continue
    return _backend[0]


# ---------------------------------------------------------------- 本机机位：和安卓 App 走一样的接口

class Agent(threading.Thread):
    """一个启用的电脑摄像头。"""

    def __init__(self, uid, name, port, key, version, log=None):
        super().__init__(daemon=True, name="localcam-%s" % cam_pid(uid))
        self.uid, self.cam_name, self.port, self.key, self.version = uid, name, port, key, version
        self.pid = cam_pid(uid)
        self._log = log or (lambda *a: None)
        self._stop_ev = threading.Event()
        self._op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self._cam = None
        self._cam_lock = threading.Lock()
        self._used = 0.0
        self._live_sid = None
        self._live_q = "low"
        self._cover_req = None
        self.error = None

    # -- 和电脑端说话
    def _url(self, path):
        return "http://127.0.0.1:%d%s" % (self.port, path)

    def _headers(self, ctype=None):
        h = {"X-T9-Key": self.key, "User-Agent": "OpenYourEyesPC/%s (%s; %s)" % (
            self.version, {"darwin": "Mac", "win32": "Windows"}.get(sys.platform, "Linux"),
            urllib.parse.quote(self.cam_name, safe=" "))}   # 请求头只能放英文字符：中文名先编码
        if ctype:
            h["Content-Type"] = ctype
        return h

    def _get(self, path, timeout=10):
        with self._op.open(urllib.request.Request(self._url(path), headers=self._headers()), timeout=timeout) as r:
            return json.loads(r.read() or b"{}")

    def _post(self, path, data, ctype="application/json", timeout=30):
        body = data if isinstance(data, bytes) else json.dumps(data).encode("utf-8")
        req = urllib.request.Request(self._url(path), data=body, headers=self._headers(ctype), method="POST")
        try:
            with self._op.open(req, timeout=timeout) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    def _say(self, msg):
        self._log("%s: %s" % (self.cam_name, msg))
        try:
            self._post("/api/phone/log", {"id": self.pid, "msg": msg}, timeout=5)
        except Exception:
            pass

    # -- 摄像头开关（只在要用时开，用完就关）
    def _ensure_cam(self, settle=True):
        """打开摄像头并等它稳定（settle=False：有第一帧就行，取景用——曝光在画面里慢慢调过来就好）。返回摄像头。
        已经开着但画面断了（电脑睡眠过、USB 重新插拔、被别的程序抢走）：关掉重开；刚打开没出画面也重开一次。"""
        for attempt in range(2):
            with self._cam_lock:
                self._used = time.time()
                cam = self._cam
                if cam is not None and cam.frames() > 0 and time.time() - cam.last > STALE_S:
                    self._say("画面断了 %d 秒，重新打开摄像头" % int(time.time() - cam.last))
                    cam = None
                    self._close_cam_locked()
                if cam is None:
                    b = backend()
                    if b is None:
                        raise CamError("这台电脑用不了摄像头")
                    if b.permission() == "denied":
                        raise CamError("没有摄像头权限：在系统设置 → 隐私与安全性 → 摄像头 里打开 Open Your Eyes")
                    cam = b.open(self.uid)
                    cam.start()
                    self._cam = cam
            t0 = time.time()
            while time.time() - t0 < 6:   # 等第一帧，再等自动曝光稳定（刚打开时画面偏暗、偏色）
                if cam.frames() > 0 and (not settle or time.time() - cam.started >= SETTLE_S):
                    return cam
                time.sleep(0.05)
            with self._cam_lock:   # 没出画面：关掉，下一轮（或下次）重新打开，不能一直拿着坏掉的
                if self._cam is cam:
                    self._close_cam_locked()
        raise CamError("摄像头没有画面（可能正被别的程序占用，或者拔掉了）")

    def _close_cam_locked(self):
        cam, self._cam = self._cam, None
        if cam is not None:
            try:
                cam.stop()
            except Exception:
                pass

    def _close_cam(self):
        with self._cam_lock:
            self._close_cam_locked()

    def _idle_check(self):
        if self._cam is not None and not self._live_sid and time.time() - self._used > IDLE_CLOSE_S:
            self._close_cam()

    # -- 主循环
    def stop(self):
        self._stop_ev.set()
        self._live_sid = None

    def run(self):
        fails = 0
        try:
            while not self._stop_ev.is_set():
                try:
                    q = {"app": 1, "vis": 1, "hold": 3, "state": "ready", "facing": "environment", "torch": 0,
                         "id": self.pid, "live": self._live_sid or "", "cap": "liveshot", "pc": 1,
                         "defname": self.cam_name}   # 只在第一次加入时当默认名；用户改过名就不会被改回去
                    j = self._get("/api/phone/poll?" + urllib.parse.urlencode(q), timeout=10)
                    fails = 0
                    self._handle_live(j.get("live") or [])
                    cr = j.get("coverReq")
                    if cr is not None and self._cover_req is not None and cr > self._cover_req:
                        self._cover()
                    self._cover_req = cr if cr is not None else self._cover_req
                    req = j.get("request")
                    if req:
                        self._handle_request(req)
                    self._idle_check()
                except Exception:
                    fails += 1
                    self._stop_ev.wait(min(5, fails))
        finally:
            self._live_sid = None
            self._close_cam()

    # -- 实时画面
    def _handle_live(self, live):
        sid = live[0].get("sid") if live else None
        q = "high" if live and live[0].get("q") == "high" else "low"
        if not sid:
            self._live_sid = None
            return
        if sid == self._live_sid:
            self._live_q = q
            return
        self._live_sid, self._live_q = sid, q
        threading.Thread(target=self._stream, args=(sid,), daemon=True).start()

    def _stream(self, sid):
        self._say("取景：开始（电脑摄像头）")
        try:
            cam = self._ensure_cam(settle=False)
        except Exception as exc:
            self._say("取景失败：%s" % exc)
            self._live_sid = None
            return
        last = -1
        while self._live_sid == sid and not self._stop_ev.is_set():
            t0 = time.time()
            self._used = t0
            if cam.last and t0 - cam.last > STALE_S:   # 画面断了：重开
                try:
                    cam = self._ensure_cam(settle=False)
                    last = -1
                except Exception as exc:
                    self._say("取景中断：%s" % exc)
                    if self._live_sid == sid:
                        self._live_sid = None
                    break
            if cam.frames() != last:
                last = cam.frames()
                jpg = cam.jpeg(LIVE_SIDE[self._live_q], 0.7)
                if jpg:
                    code = self._post("/api/live/frame?sid=%s&phone=%s" % (sid, self.pid), jpg, "image/jpeg", timeout=5)
                    if code == 410:   # 电脑那边结束了
                        if self._live_sid == sid:
                            self._live_sid = None
                        break
            self._stop_ev.wait(max(0.0, 1.0 / LIVE_FPS[self._live_q] - (time.time() - t0)))

    # -- 拍照
    def _handle_request(self, req):
        rid = req.get("id")
        try:
            cam = self._ensure_cam()
            meta = {"requestId": rid, "phoneId": self.pid, "app": "pc %s" % self.version,
                    "camera": {"facing": "environment", "device": self.cam_name}}
            if req.get("kind") == "clip":
                frames = max(2, min(int(req.get("frames") or 4), 8))
                secs = float(req.get("seconds") or 2)
                gap = secs / max(1, frames - 1)
                shots = []
                for i in range(frames):
                    t0 = time.time()
                    self._used = t0
                    shots.append(base64.b64encode(cam.jpeg(None, 0.9) or b"").decode("ascii"))
                    if i < frames - 1:
                        time.sleep(max(0.0, gap - (time.time() - t0)))
                meta["clipSeconds"] = secs
                body = {"meta": meta, "frames": shots}
            else:
                jpg = cam.jpeg(None, 0.92)
                if not jpg:
                    raise CamError("摄像头没有画面")
                body = {"meta": meta, "jpegBase64": base64.b64encode(jpg).decode("ascii")}
            self._used = time.time()
            code = self._post("/api/snap", body, timeout=60)
            if code != 200:
                self._say("照片没交上去（HTTP %s）" % code)
            self._cover(cam)
        except Exception as exc:
            self._say("拍照失败：%s" % exc)

    def _cover(self, cam=None):
        """设置页上显示的小图（只在拍照后、或用户点了「刷新画面」时更新）。"""
        try:
            cam = cam or self._ensure_cam()
            jpg = cam.jpeg(COVER_SIDE, 0.7)
            if jpg:
                self._post("/api/phone/cover", {"id": self.pid, "jpegBase64": base64.b64encode(jpg).decode("ascii")}, timeout=10)
        except Exception as exc:
            self._say("画面没更新：%s" % exc)


# ---------------------------------------------------------------- 管理：按设置启动 / 停止

class Manager:
    def __init__(self, port, key_fn, version, log=None):
        self.port, self.key_fn, self.version, self._log = port, key_fn, version, log
        self._agents = {}
        self._lock = threading.Lock()

    def apply(self, enabled, wait=0.0):
        """enabled：{uid: 名字}。启动新打开的、停掉关掉的。
        wait>0：等停掉的线程真正退出（它可能还有一次轮询在路上，不等的话会在移除之后又把自己登记回来）。"""
        stopped = []
        with self._lock:
            for uid in list(self._agents):
                if uid not in enabled:
                    a = self._agents.pop(uid)
                    a.stop()
                    stopped.append(a)
            if enabled and backend() is not None:
                for uid, name in enabled.items():
                    a = self._agents.get(uid)
                    if a is None or not a.is_alive():
                        a = Agent(uid, name, self.port, self.key_fn(), self.version, self._log)
                        self._agents[uid] = a
                        a.start()
        end = time.time() + wait
        for a in stopped:
            a.join(max(0.0, end - time.time()))

    def stop_all(self):
        self.apply({})
