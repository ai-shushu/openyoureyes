"""端到端测试：在临时目录、临时端口上起一个真实的服务，模拟手机和 Claude 调用。

运行：.venv/bin/python -m unittest discover -s tests -v
"""
import base64
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _jpeg():
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", (64, 48), (120, 90, 60)).save(b, "JPEG")
    return b.getvalue()


class Server:
    def __init__(self, snap_dir=None, env=None):
        self.tmp = tempfile.mkdtemp(prefix="t9test-")
        self.snap = snap_dir or os.path.join(self.tmp, "snaps")
        os.makedirs(self.snap, exist_ok=True)
        self.port, self.https = _free_port(), _free_port()
        env = dict(os.environ, T9_WEB_PORT=str(self.port), T9_HTTPS_PORT=str(self.https), T9_SNAP_DIR=self.snap,
                   T9_CERT_DIR=os.path.join(self.tmp, "certs"), T9_NO_POWER="1", **(env or {}))
        self.proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "server", "serve.py")], env=env,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        for _ in range(100):
            try:
                self.get("/config.json")
                return
            except Exception:
                time.sleep(0.1)
        raise RuntimeError("server did not start: " + self.proc.stdout.read1(4000).decode(errors="replace"))

    def url(self, path):
        return "http://127.0.0.1:%d%s" % (self.port, path)

    def get(self, path, headers=None):
        req = urllib.request.Request(self.url(path), headers=headers or {})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read() or b"{}")

    def post(self, path, body, headers=None, local=True):
        h = {"Content-Type": "application/json"}
        if local:
            h["X-T9"] = "1"
        h.update(headers or {})
        req = urllib.request.Request(self.url(path), data=json.dumps(body).encode(), headers=h, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def key(self):
        return self.get("/api/settings")["setupUrl"].split("#k=")[1]

    def poll(self, pid, key=None, state="ready"):
        h = {"X-T9-Key": key} if key else {}
        return self.get("/api/phone/poll?id=%s&vis=1&state=%s&hold=0.2&facing=environment" % (pid, state), h)

    def close(self):
        self.proc.terminate()
        self.proc.wait(5)
        self.proc.stdout.close()
        shutil.rmtree(self.tmp, ignore_errors=True)


class PairingTest(unittest.TestCase):
    def setUp(self):
        self.s = Server()

    def tearDown(self):
        self.s.close()

    def test_unpaired_phone_waits_for_approval(self):
        j = self.s.poll("phoneaaaa01")
        self.assertTrue(j.get("pending"))
        self.assertIsNone(j.get("request"))
        phones = self.s.get("/api/settings")["phones"]
        self.assertEqual([p["pending"] for p in phones if p["id"] == "phoneaaaa01"], [True])
        # 没允许的手机不算在线，Claude 拍照不会找它
        st, j = self.s.post("/api/request", {"what": "x"})
        self.assertFalse(j.get("ok"))
        self.s.post("/api/phones/approve", {"id": "phoneaaaa01"})
        self.assertFalse(self.s.poll("phoneaaaa01").get("pending"))

    def test_qr_key_pairs_automatically(self):
        self.assertFalse(self.s.poll("phonebbbb02", key=self.s.key()).get("pending"))
        self.assertTrue(self.s.poll("phonecccc03", key="wrong-key-123456789").get("pending"))

    def test_rejected_phone_stays_out(self):
        self.s.poll("phonedddd04")
        self.s.post("/api/phones/remove", {"id": "phonedddd04"})
        self.assertTrue(self.s.poll("phonedddd04").get("pending"))
        self.assertNotIn("phonedddd04", [p["id"] for p in self.s.get("/api/settings")["phones"]])

    def test_pending_flood_is_capped(self):
        for i in range(12):
            self.s.poll("flood%06d" % i)
        pend = [p for p in self.s.get("/api/settings")["phones"] if p.get("pending")]
        self.assertLessEqual(len(pend), 5)

    def test_settings_api_is_local_only_for_writes(self):
        st, _ = self.s.post("/api/prefs", {"retentionDays": 3}, local=False)
        self.assertEqual(st, 403)


class PhotoFlowTest(unittest.TestCase):
    def setUp(self):
        self.s = Server()
        self.key = self.s.key()

    def tearDown(self):
        self.s.close()

    def test_request_photo_roundtrip(self):
        self.s.poll("phoneeeee05", key=self.key)
        st, j = self.s.post("/api/request", {"what": "电路板"})
        self.assertTrue(j["ok"], j)
        rid = j["id"]
        self.assertGreaterEqual(len(rid), 12)   # 随机编号，猜不到
        req = self.s.poll("phoneeeee05", key=self.key)["request"]
        self.assertEqual(req["id"], rid)
        meta = {"requestId": rid, "phoneId": "phoneeeee05", "phone": {"name": "1 号机"}}
        st, j = self.s.post("/api/snap", {"meta": meta, "jpegBase64": base64.b64encode(_jpeg()).decode()},
                            headers={"X-T9-Key": self.key}, local=False)
        self.assertEqual(st, 200, j)
        w = self.s.get("/api/request?id=%s&wait=5" % rid)
        self.assertTrue(any(x.get("file") for x in (w.get("shots") or [])) or w.get("done"), w)

    def test_photo_without_request_rejected(self):
        self.s.poll("phoneffff06", key=self.key)
        meta = {"requestId": "r0000000000000000", "phoneId": "phoneffff06"}
        st, _ = self.s.post("/api/snap", {"meta": meta, "jpegBase64": base64.b64encode(_jpeg()).decode()}, local=False)
        self.assertEqual(st, 409)

    def test_cover_only_from_approved_phone(self):
        img = base64.b64encode(_jpeg()).decode()
        self.s.poll("phonegggg07")   # 没允许
        st, _ = self.s.post("/api/phone/cover", {"id": "phonegggg07", "jpegBase64": img}, local=False)
        self.assertEqual(st, 400)
        self.s.poll("phonehhhh08", key=self.key)
        st, _ = self.s.post("/api/phone/cover", {"id": "phonehhhh08", "jpegBase64": img}, local=False)
        self.assertEqual(st, 200)
        p = [x for x in self.s.get("/api/settings")["phones"] if x["id"] == "phonehhhh08"][0]
        self.assertTrue(p["cover"])


class SavedPhonesTest(unittest.TestCase):
    def test_saved_phones_keep_their_approval(self):
        """重启后：允许过的手机照常能用，没允许过的仍要在电脑上点「允许」。"""
        tmp = tempfile.mkdtemp(prefix="t9snap-")
        with open(os.path.join(tmp, ".phones.json"), "w") as f:
            json.dump({"okphone0001": {"name": "1 号机", "ok": True},
                       "newphone002": {"name": "2 号机", "ok": False}}, f)
        s = Server(snap_dir=tmp)
        try:
            self.assertFalse(s.poll("okphone0001").get("pending"))
            self.assertTrue(s.poll("newphone002").get("pending"))
        finally:
            s.close()
            shutil.rmtree(tmp, ignore_errors=True)


class StartupWaitTest(unittest.TestCase):
    def test_request_right_after_start_waits_for_known_phone(self):
        """程序刚启动、已登记的手机还没连回来：拍照请求先等它，而不是马上说「没有在线的手机」。"""
        tmp = tempfile.mkdtemp(prefix="t9snap-")
        with open(os.path.join(tmp, ".phones.json"), "w") as f:
            json.dump({"okphone0001": {"name": "1 号机", "ok": True}}, f)
        s = Server(snap_dir=tmp)
        try:
            threading.Timer(2.0, lambda: s.poll("okphone0001")).start()   # 2 秒后手机连回来
            t0 = time.time()
            st, j = s.post("/api/request", {"what": "电路板"})
            self.assertEqual(st, 200, j)
            self.assertTrue(j.get("id"), j)
            self.assertGreaterEqual(time.time() - t0, 1.5)
            t1 = time.time()
            st, j = s.post("/api/request", {"what": "x", "phone": "侧拍"})   # 不认识的机位名：不等
            self.assertFalse(j.get("ok"), j)
            self.assertLess(time.time() - t1, 2)
        finally:
            s.close()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_no_known_phones_fails_at_once(self):
        s = Server()
        try:
            t0 = time.time()
            st, j = s.post("/api/request", {"what": "x"})
            self.assertFalse(j.get("ok"), j)
            self.assertLess(time.time() - t0, 2)
        finally:
            s.close()


class LocalCamTest(unittest.TestCase):
    """电脑自己的摄像头当机位（用假摄像头，不碰真摄像头）。"""

    def setUp(self):
        self.s = Server(env={"T9_FAKE_LOCALCAM": "1"})

    def tearDown(self):
        self.s.close()

    def _wait_phone(self, cond, secs=10):
        end = time.time() + secs
        while time.time() < end:
            phones = self.s.get("/api/settings")["phones"]
            hit = [p for p in phones if cond(p)]
            if hit:
                return hit[0]
            time.sleep(0.2)
        return None

    def test_enable_shoot_and_remove(self):
        info = self.s.get("/api/localcams")
        self.assertTrue(info["supported"])
        cam = info["cameras"][0]
        self.assertFalse(cam["on"])
        self.assertIsNone(self._wait_phone(lambda p: p.get("pc"), 1.5))   # 没打开：不出现在机位里，也不开摄像头
        st, j = self.s.post("/api/localcams", {"uid": cam["uid"], "on": True})
        self.assertTrue(j["ok"], j)
        ph = self._wait_phone(lambda p: p.get("pc") and p["online"])
        self.assertIsNotNone(ph, "打开后应当作为在线机位出现")
        self.assertEqual(ph["name"], "测试摄像头")
        self.assertEqual(ph["device"], "电脑")
        st, _ = self.s.post("/api/phones/rename", {"id": ph["id"], "name": "罗技"})   # 用户改的名不会被设备名改回去
        time.sleep(4)
        self.assertEqual(self._wait_phone(lambda p: p.get("pc"))["name"], "罗技")
        st, j = self.s.post("/api/request", {"what": "电路板", "phone": "罗技"})
        self.assertTrue(j.get("ok"), j)
        w = self.s.get("/api/request?id=%s&wait=10" % j["id"])
        self.assertEqual(w["state"], "done", w)
        self.assertEqual([sh["state"] for sh in w["shots"]], ["done"])
        status = self.s.get("/api/status")
        self.assertTrue(any(p.get("pc") for p in status["phones"]))
        st, j = self.s.post("/api/phones/remove", {"id": ph["id"]})   # 在机位上点「移除」= 关掉这个摄像头
        self.assertEqual(st, 200)
        self.assertFalse(self.s.get("/api/localcams")["cameras"][0]["on"])
        time.sleep(4)   # 本机机位线程最多再挂一次轮询
        self.assertIsNone(self._wait_phone(lambda p: p.get("pc"), 1))


class MultiPhoneRequestTest(unittest.TestCase):
    """AI 一次指定几台机位：一个请求里一起拍，不用分几次调用。"""

    def setUp(self):
        self.s = Server()
        key = self.s.key()
        for pid in ("phoneaaaa01", "phonebbbb02", "phonecccc03"):
            self.s.poll(pid, key=key)

    def tearDown(self):
        self.s.close()

    def _shots(self, rid):
        return sorted(sh["phone"] for sh in self.s.get("/api/request?id=%s&wait=0" % rid)["shots"])

    def test_several_names_in_one_request(self):
        st, j = self.s.post("/api/request", {"what": "x", "phone": "1 号机，3 号机"})
        self.assertTrue(j["ok"], j)
        self.assertEqual(self._shots(j["id"]), ["1 号机", "3 号机"])
        self.assertEqual(j["missing"], [])
        st, j = self.s.post("/api/request", {"what": "x", "phone": ["2 号机", "3号机"]})   # 列表、少个空格也认
        self.assertTrue(j["ok"], j)
        self.assertEqual(self._shots(j["id"]), ["2 号机", "3 号机"])

    def test_missing_one_still_shoots_the_rest(self):
        st, j = self.s.post("/api/request", {"what": "x", "phone": "1 号机、侧拍"})
        self.assertTrue(j["ok"], j)
        self.assertEqual(self._shots(j["id"]), ["1 号机"])
        self.assertEqual(j["missing"], ["侧拍"])
        st, j = self.s.post("/api/request", {"what": "x", "phone": "侧拍,顶拍"})
        self.assertFalse(j["ok"])
        self.assertIn("侧拍", j["error"])


class CertTest(unittest.TestCase):
    def test_ca_is_name_constrained(self):
        import certs
        from cryptography import x509
        d = tempfile.mkdtemp(prefix="t9cert-")
        try:
            certs.ensure_server_cert(d, ["192.168.1.20"])
            ca = certs._load_cert(os.path.join(d, "ca.crt"))
            ca.extensions.get_extension_for_class(x509.NameConstraints)
            ok, _ = certs.server_cert_status(d, ["192.168.1.20"])
            self.assertTrue(ok)
            self.assertFalse(certs.ip_allowed("8.8.8.8"))
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()


class McpHttpTest(unittest.TestCase):
    def setUp(self):
        self.s = Server()

    def tearDown(self):
        self.s.close()

    def rpc(self, msg, sid=None, origin=None):
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if sid:
            h["Mcp-Session-Id"] = sid
        if origin:
            h["Origin"] = origin
        req = urllib.request.Request(self.s.url("/mcp"), data=json.dumps(msg).encode(), headers=h, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
                return r.status, r.headers.get("Mcp-Session-Id"), json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            return e.code, None, None

    def test_initialize_list_call(self):
        st, sid, j = self.rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                               "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}}})
        self.assertEqual(st, 200)
        self.assertTrue(sid)
        self.assertEqual(j["result"]["serverInfo"]["name"], "open-your-eyes")
        st, _, j = self.rpc({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
        self.assertEqual(st, 202)
        st, _, j = self.rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, sid)
        self.assertEqual({t["name"] for t in j["result"]["tools"]}, {"look", "look_clip", "zoom", "eyes_status"})
        st, _, j = self.rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                             "params": {"name": "eyes_status", "arguments": {}}}, sid)
        self.assertIn("content", j["result"])
        # 没有在线手机时 look 给出大白话错误，而不是卡住
        st, _, j = self.rpc({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                             "params": {"name": "look", "arguments": {"what": "x"}}}, sid)
        self.assertTrue(j["result"].get("isError"))

    def test_unknown_session_keeps_working(self):
        st, _, j = self.rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, sid="stale-session-id")
        self.assertEqual(st, 200)

    def test_foreign_origin_rejected(self):
        st, _, _ = self.rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, origin="https://evil.example")
        self.assertEqual(st, 403)


class DiagnosticsTest(unittest.TestCase):
    def test_zip_has_no_secrets(self):
        import zipfile
        s = Server()
        try:
            key = s.key()
            with urllib.request.urlopen(s.url("/api/diagnostics.zip"), timeout=10) as r:
                data = r.read()
            z = zipfile.ZipFile(io.BytesIO(data))
            info = z.read("info.json").decode()
            self.assertNotIn(key, info)
            self.assertIn("version", json.loads(info))
        finally:
            s.close()


class AppLiveTest(unittest.TestCase):
    """安卓 App：轮询带 app=1，实时取景走 JPEG 画面流。"""

    def setUp(self):
        self.s = Server()
        self.key = self.s.key()

    def tearDown(self):
        self.s.close()

    def test_config_has_ca_fingerprint(self):
        c = self.s.get("/config.json")
        self.assertEqual(len(c["caSha256"]), 64)

    def test_frame_shared_by_all_viewers(self):
        """主窗口和取景窗口同时看同一台安卓手机：手机只送一路，两边都要有画面。"""
        pid = "aapp0000002"
        self.s.get("/api/phone/poll?id=%s&vis=1&state=ready&hold=0.1&app=1" % pid, {"X-T9-Key": self.key})
        sids = [self.s.post("/api/live/start", {"phone": pid, "q": "low"})[1]["sid"] for _ in range(2)]
        req = urllib.request.Request(self.s.url("/api/live/frame?sid=%s&phone=%s" % (sids[0], pid)), data=_jpeg(),
                                     headers={"Content-Type": "image/jpeg"}, method="POST")
        with urllib.request.urlopen(req, timeout=5) as r:
            self.assertEqual(r.status, 200)
        for sid in sids:
            self.assertEqual(self.s.post("/api/live/ping", {"sid": sid})[1].get("mode"), "mjpeg")

    def test_app_flag_and_mjpeg(self):
        pid = "aapp0000001"
        self.s.get("/api/phone/poll?id=%s&vis=1&state=ready&hold=0.1&app=1" % pid, {"X-T9-Key": self.key})
        p = [x for x in self.s.get("/api/settings")["phones"] if x["id"] == pid][0]
        self.assertTrue(p["app"])
        st, j = self.s.post("/api/live/start", {"phone": pid, "q": "low"})
        sid = j["sid"]
        live = self.s.get("/api/phone/poll?id=%s&vis=1&state=ready&hold=0.5&app=1" % pid, {"X-T9-Key": self.key})["live"]
        self.assertEqual(live[0]["sid"], sid)
        frame = _jpeg()
        got = []

        def viewer():
            with urllib.request.urlopen(self.s.url("/api/live/mjpeg?sid=" + sid), timeout=10) as r:
                got.append(r.read(len(frame) + 200))

        t = threading.Thread(target=viewer)
        t.start()
        time.sleep(0.5)
        req = urllib.request.Request(self.s.url("/api/live/frame?sid=%s&phone=%s" % (sid, pid)), data=frame,
                                     headers={"Content-Type": "image/jpeg"}, method="POST")
        with urllib.request.urlopen(req, timeout=5) as r:
            self.assertEqual(r.status, 200)
        st, j = self.s.post("/api/live/ping", {"sid": sid})
        self.assertEqual(j["state"], "live")
        self.s.post("/api/live/stop", {"sid": sid})
        t.join(10)
        self.assertIn(frame[:20], got[0])
        # 别的手机拿着这个会话编号也交不了画面
        req = urllib.request.Request(self.s.url("/api/live/frame?sid=%s&phone=other" % sid), data=frame, method="POST")
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(req, timeout=5)


class MultiPageTest(unittest.TestCase):
    def test_two_tabs_do_not_flap(self):
        s = Server()
        try:
            key = s.key()
            pid = "ptabs0000001"
            for _ in range(3):   # 一个标签在前台、一个在后台，交替上报
                s.get("/api/phone/poll?id=%s&vis=1&state=ready&hold=0.1&page=aaaa" % pid, {"X-T9-Key": key})
                s.get("/api/phone/poll?id=%s&vis=0&state=hidden&hold=0.1&page=bbbb" % pid, {"X-T9-Key": key})
            p = [x for x in s.get("/api/settings")["phones"] if x["id"] == pid][0]
            self.assertEqual(p["state"], "ready")
            ev = [e["text"] for e in s.get("/api/settings")["events"] if "后台" in e["text"]]
            self.assertEqual(ev, [])
        finally:
            s.close()


class KeywordPromptTest(unittest.TestCase):
    def test_keyword_in_instructions_and_snap_prompt(self):
        s = Server()
        try:
            def rpc(m):
                req = urllib.request.Request(s.url("/mcp"), data=json.dumps(m).encode(),
                                             headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=10) as r:
                    return json.loads(r.read())
            init = rpc({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}}})
            self.assertIn("睁眼", init["result"]["instructions"])
            tools = rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
            self.assertIn("睁眼", [t for t in tools if t["name"] == "look"][0]["description"])
            got = rpc({"jsonrpc": "2.0", "id": 3, "method": "prompts/get", "params": {"name": "snap", "arguments": {"what": "板子背面"}}})
            self.assertIn("板子背面", got["result"]["messages"][0]["content"]["text"])
        finally:
            s.close()


class CodexLinkTest(unittest.TestCase):
    """只有 Codex 的会话在照片后面附原图链接；Claude 的返回内容不变。"""

    def test_link_only_for_codex(self):
        s = Server()
        try:
            key = s.key()
            results = {}

            def phone():   # 模拟一台手机：领到请求就交一张照片
                end = time.time() + 25
                while time.time() < end and len(results) < 2:
                    j = s.poll("plink000001", key=key)
                    req = j.get("request")
                    if req:
                        meta = {"requestId": req["id"], "phoneId": "plink000001"}
                        s.post("/api/snap", {"meta": meta, "jpegBase64": base64.b64encode(_jpeg()).decode()},
                               headers={"X-T9-Key": key}, local=False)
            s.poll("plink000001", key=key)
            threading.Thread(target=phone, daemon=True).start()
            for client in ("codex-mcp-client", "claude-code"):
                def rpc(m, sid=None):
                    h = {"Content-Type": "application/json"}
                    if sid:
                        h["Mcp-Session-Id"] = sid
                    req = urllib.request.Request(s.url("/mcp"), data=json.dumps(m).encode(), headers=h, method="POST")
                    with urllib.request.urlopen(req, timeout=40) as r:
                        return r.headers.get("Mcp-Session-Id"), json.loads(r.read() or b"{}")
                sid, _ = rpc({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                              "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": client}}})
                _, j = rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "look", "arguments": {"what": "测试"}}}, sid)
                texts = [c.get("text", "") for c in j["result"]["content"] if c["type"] == "text"]
                results[client] = any("/photo/" in t for t in texts)
            self.assertTrue(results["codex-mcp-client"])
            self.assertFalse(results["claude-code"])
            # 程序重启后 Codex 还用着老连接（没有重新打招呼）：靠请求自带的标识也能认出
            h = {"Content-Type": "application/json", "Mcp-Session-Id": "stale-codex-session", "User-Agent": "codex-mcp-client/0.167.0"}
            req = urllib.request.Request(s.url("/mcp"), data=json.dumps({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                                         "params": {"name": "look", "arguments": {"what": "测试"}}}).encode(), headers=h, method="POST")
            results.pop("claude-code")
            with urllib.request.urlopen(req, timeout=40) as r:
                j = json.loads(r.read())
            self.assertTrue(any("/photo/" in c.get("text", "") for c in j["result"]["content"] if c["type"] == "text"))
        finally:
            s.close()


class CodexRulesAndReconnectTest(unittest.TestCase):
    def rpc(self, s, m, client=None, sid=None):
        h = {"Content-Type": "application/json"}
        if sid:
            h["Mcp-Session-Id"] = sid
        req = urllib.request.Request(s.url("/mcp"), data=json.dumps(m).encode(), headers=h, method="POST")
        with urllib.request.urlopen(req, timeout=40) as r:
            return r.headers.get("Mcp-Session-Id"), json.loads(r.read() or b"{}")

    def init(self, s, name):
        return self.rpc(s, {"jsonrpc": "2.0", "id": 0, "method": "initialize",
                            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": name}}})

    def test_codex_only_rule_claude_unchanged(self):
        import eyes_mcp
        s = Server()
        try:
            sid_c, ci = self.init(s, "codex-mcp-client")
            sid_k, ck = self.init(s, "claude-code")
            self.assertIn("Computer Use", ci["result"]["instructions"])
            self.assertEqual(ck["result"]["instructions"], eyes_mcp.INSTRUCTIONS)   # Claude 收到的和原来完全一样
            look = lambda sid: [t for t in self.rpc(s, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, sid=sid)[1]["result"]["tools"]
                                if t["name"] == "look"][0]["description"]
            self.assertIn("Computer Use", look(sid_c))
            self.assertNotIn("Computer Use", look(sid_k))
        finally:
            s.close()

    def test_waits_for_phone_that_just_dropped(self):
        s = Server()
        try:
            key = s.key()
            s.poll("pdrop000001", key=key)
            time.sleep(6)   # 手机掉线（超过 5 秒没连）

            def come_back():   # 3 秒后手机自动连回来，并交照片
                time.sleep(3)
                end = time.time() + 15
                while time.time() < end:
                    j = s.poll("pdrop000001", key=key)
                    req = j.get("request")
                    if req:
                        s.post("/api/snap", {"meta": {"requestId": req["id"], "phoneId": "pdrop000001"},
                                             "jpegBase64": base64.b64encode(_jpeg()).decode()}, headers={"X-T9-Key": key}, local=False)
                        return
            threading.Thread(target=come_back, daemon=True).start()
            sid, _ = self.init(s, "claude-code")
            _, j = self.rpc(s, {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "look", "arguments": {"what": "测试"}}}, sid=sid)
            self.assertFalse(j["result"].get("isError"), j["result"])
            self.assertTrue(any(c["type"] == "image" for c in j["result"]["content"]))
        finally:
            s.close()


class ViewfinderTest(unittest.TestCase):
    """拍照前先取景：取景器开着时手机不拍，用户按快门才拍；一次调用超过时限先回「还在取景」，再调用接着取同一张。"""

    def setUp(self):
        self.s = Server(env={"T9_CALL_BUDGET_S": "3"})
        self.key = self.s.key()
        self.s.poll("pview000001", key=self.key)
        self.s.get("/api/viewfinder?wait=0")   # 电脑程序在问取景器 = 能弹取景器
        h = {"Content-Type": "application/json"}
        req = urllib.request.Request(self.s.url("/mcp"), headers=h, method="POST", data=json.dumps(
            {"jsonrpc": "2.0", "id": 0, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "claude-code"}}}).encode())
        with urllib.request.urlopen(req, timeout=10) as r:
            self.sid = r.headers.get("Mcp-Session-Id")

    def tearDown(self):
        self.s.close()

    def look(self):
        h = {"Content-Type": "application/json", "Mcp-Session-Id": self.sid}
        req = urllib.request.Request(self.s.url("/mcp"), headers=h, method="POST", data=json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "look", "arguments": {"what": "板子"}}}).encode())
        with urllib.request.urlopen(req, timeout=40) as r:
            return json.loads(r.read())["result"]

    def current(self):
        return self.s.get("/api/viewfinder?wait=0")["current"]

    def test_shutter_then_photo(self):
        out = {}
        t = threading.Thread(target=lambda: out.update(r=self.look()), daemon=True)
        t.start()
        for _ in range(50):
            cur = self.current()
            if cur:
                break
            time.sleep(0.1)
        self.assertEqual(cur["what"], "板子")
        self.assertEqual(cur["client"], "Claude Code")
        self.assertIsNone(self.s.poll("pview000001", key=self.key).get("request"))   # 还在取景：手机不拍
        t.join(10)
        texts = " ".join(c.get("text", "") for c in out["r"]["content"])
        self.assertFalse(out["r"].get("isError"))
        self.assertIn("还没按快门", texts)   # 超过一次调用的时限：先回话
        st, j = self.s.post("/api/viewfinder/shoot", {"id": cur["id"]})
        self.assertTrue(j["ok"], j)
        self.assertTrue(self.current()["shooting"])   # 照片拍到之前取景器留着，告诉用户别动
        req = None
        for _ in range(20):   # 按快门后隔 1.5 秒才让手机领（等实时画面用的摄像头放干净）
            req = self.s.poll("pview000001", key=self.key).get("request")
            if req:
                break
        self.assertEqual(req["id"], cur["id"])
        meta = {"requestId": req["id"], "phoneId": "pview000001"}
        self.s.post("/api/snap", {"meta": meta, "jpegBase64": base64.b64encode(_jpeg()).decode()},
                    headers={"X-T9-Key": self.key}, local=False)
        self.assertIsNone(self.current())   # 拍到了：取景器收起
        r = self.look()   # AI 再调用一次：接着取同一个请求的照片
        self.assertTrue(any(c["type"] == "image" for c in r["content"]), r)

    def test_cancel_and_queue(self):
        st, a = self.s.post("/api/request", {"what": "一", "frame": True})
        st, b = self.s.post("/api/request", {"what": "二", "frame": True})
        self.assertTrue(a["framing"] and b["framing"])
        j = self.s.get("/api/viewfinder?wait=0")
        self.assertEqual(j["current"]["id"], a["id"])
        self.assertEqual(j["queued"], 1)   # 第二个排队
        self.s.post("/api/viewfinder/cancel", {"id": a["id"]})
        self.assertEqual(self.current()["id"], b["id"])
        w = self.s.get("/api/request?id=%s&wait=0" % a["id"])
        self.assertEqual(w["stage"], "cancelled")
        self.assertIn("取消", w["cancelReason"])

    def test_app_shoots_on_live_stream_at_once(self):
        """新版安卓 App 正开着实时画面：按快门后马上领到拍照任务，实时画面不断（就在这一路上拍）。"""
        pid = "aliveshot01"
        app = lambda hold: self.s.get("/api/phone/poll?id=%s&vis=1&state=ready&hold=%s&app=1&cap=liveshot" % (pid, hold),
                                      {"X-T9-Key": self.key})
        app(0.1)
        st, lv = self.s.post("/api/live/start", {"phone": pid, "q": "high"})
        st, a = self.s.post("/api/request", {"what": "快拍", "frame": True, "phone": pid})
        self.assertTrue(a["framing"])
        t0 = time.time()
        self.s.post("/api/viewfinder/shoot", {"id": a["id"]})
        j = app(0.5)
        self.assertEqual((j.get("request") or {}).get("id"), a["id"])
        self.assertLess(time.time() - t0, 1.2)   # 不再等 1.5 秒
        self.assertTrue(any(x["sid"] == lv["sid"] for x in j["live"]))   # 画面还开着

    def test_turned_off_shoots_directly(self):
        self.s.post("/api/prefs", {"frameFirst": False})
        st, j = self.s.post("/api/request", {"what": "直接拍", "frame": True})
        self.assertFalse(j["framing"])
        self.assertTrue(self.s.poll("pview000001", key=self.key).get("request"))


class AutostartIsolationTest(unittest.TestCase):
    """测试实例（非默认端口）不能读写系统的开机自启：否则它会把装好的程序的自启改成指向自己。"""
    def test_test_instance_leaves_autostart_alone(self):
        srv = Server()
        try:
            s = srv.get("/api/settings")
            self.assertFalse(s["autostart"]["enabled"])
            code, _ = srv.post("/api/autostart", {"on": True})
            self.assertEqual(code, 400)
        finally:
            srv.close()


class UpdateCheckTest(unittest.TestCase):
    """电脑端新版本检查：发布地址的 latest.json 比当前版本新才提示；格式不对、网络不通都不出错。"""
    def test_feed(self):
        import http.server
        import serve
        feeds = {}

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = feeds.get(self.path, "").encode()
                self.send_response(200 if body else 404)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:%d" % srv.server_address[1]
        old = serve.UPDATE_FEED
        try:
            feeds["/new"] = json.dumps({"version": "99.0.0", "page": "https://example.org/dl", "notes": "更好用"})
            feeds["/bad"] = json.dumps({"version": "99.0.0", "page": "javascript:alert(1)"})
            serve.UPDATE_FEED = base + "/new"
            self.assertEqual(serve.check_update_once()["version"], "99.0.0")
            self.assertEqual(serve.settings_snapshot()["update"]["page"], "https://example.org/dl")
            serve._update.clear()
            serve.UPDATE_FEED = base + "/bad"
            serve.check_update_once()
            self.assertFalse(serve._update)   # 下载页不是 https，不采信
            serve.UPDATE_FEED = base + "/missing"
            self.assertIsNone(serve.check_update_once())
            self.assertIsNone(serve.settings_snapshot()["update"])
            serve.UPDATE_FEED = ""
            self.assertIsNone(serve.check_update_once())
        finally:
            serve.UPDATE_FEED = old
            serve._update.clear()
            srv.shutdown()


class VersionTest(unittest.TestCase):
    def test_one_version_everywhere(self):
        import re
        import eyes_mcp
        serve = open(os.path.join(ROOT, "server", "serve.py"), encoding="utf-8").read()
        v = re.search(r'^VERSION = "([^"]+)"', serve, re.M).group(1)
        self.assertEqual(eyes_mcp.VERSION, v)

    def test_extension_min_version(self):
        """扩展「最低要求版本」不能比当前版本还新（否则装了最新扩展的人也会一直被提示更新）。"""
        import integrations
        import eyes_mcp
        self.assertLessEqual(integrations._ver(integrations.EXT_MIN_VERSION), integrations._ver(eyes_mcp.VERSION))
        self.assertLess(integrations._ver("0.6.0"), integrations._ver(integrations.EXT_MIN_VERSION))


class BridgeTest(unittest.TestCase):
    """Codex 用的本地桥接：不走系统代理；程序重启后同一个对话接着能用；程序没开时工具照常列出、调用时说明原因。"""

    def _bridge(self, port, wait="8"):
        env = dict(os.environ, T9_PORT=str(port), T9_BRIDGE_WAIT_S=wait,
                   HTTP_PROXY="http://127.0.0.1:9", HTTPS_PROXY="http://127.0.0.1:9", NO_PROXY="")   # 代理是坏的：桥接必须不走它
        env.pop("no_proxy", None)
        p = subprocess.Popen([sys.executable, os.path.join(ROOT, "server", "mcp_bridge.py")], env=env, cwd=os.path.join(ROOT, "server"),
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
        self.addCleanup(p.kill)
        return p

    def _ask(self, p, msg):
        p.stdin.write(json.dumps(msg) + "\n")
        p.stdin.flush()
        if msg.get("id") is None:
            return None
        return json.loads(p.stdout.readline())

    def _status_text(self, p, mid):
        r = self._ask(p, {"jsonrpc": "2.0", "id": mid, "method": "tools/call", "params": {"name": "eyes_status", "arguments": {}}})
        return r["result"]["content"][0]["text"]

    def test_survives_restart(self):
        s = Server()
        port = s.port
        p = self._bridge(port)
        r = self._ask(p, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "codex-mcp-client"}}})
        self.assertEqual(r["result"]["serverInfo"]["name"], "open-your-eyes")
        self._ask(p, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertIn("Open Your Eyes", self._status_text(p, 2))
        s.close()   # 程序重启（比如更新）：同一个端口上再起一个
        tmp = tempfile.mkdtemp(prefix="t9test-")
        self.addCleanup(shutil.rmtree, tmp, True)
        env = dict(os.environ, T9_WEB_PORT=str(port), T9_HTTPS_PORT=str(_free_port()), T9_SNAP_DIR=os.path.join(tmp, "snaps"),
                   T9_CERT_DIR=os.path.join(tmp, "certs"), T9_NO_POWER="1")
        procs = []
        self.addCleanup(lambda: [x.kill() for x in procs])
        threading.Timer(1.5, lambda: procs.append(subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "server", "serve.py")], env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))).start()
        t0 = time.time()
        txt = self._status_text(p, 3)
        self.assertIn("Open Your Eyes", txt)
        self.assertNotIn("没有运行", txt)
        self.assertLess(time.time() - t0, 15)

    def test_offline_lists_tools(self):
        p = self._bridge(_free_port(), wait="1")
        r = self._ask(p, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
        self.assertIn("result", r)
        tools = self._ask(p, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
        self.assertIn("look", [t["name"] for t in tools])
        self.assertIn("没有运行", self._status_text(p, 3))


class CodexConfigTest(unittest.TestCase):
    """接入 Codex 写的是本地桥接；直接填网址的配置（比如当初从 dmg 里运行时接入的）会被自动改成桥接，用户别的设置不动。"""

    def test_repair_old_url_entry(self):
        import integrations
        d = tempfile.mkdtemp(prefix="t9codex-")
        self.addCleanup(shutil.rmtree, d, True)
        cfg = os.path.join(d, "config.toml")
        with open(cfg, "w") as f:
            f.write('model = "x"\n\n[mcp_servers.open-your-eyes]\nurl = "http://127.0.0.1:39757/mcp"\n\n[projects."/a"]\ntrust_level = "trusted"\n')
        old = os.environ.get("CODEX_HOME")
        os.environ["CODEX_HOME"] = d
        try:
            self.assertTrue(integrations.repair_codex("http://127.0.0.1:39757/mcp"))
            self.assertFalse(integrations.repair_codex("http://127.0.0.1:39757/mcp"))   # 已经是新的：不再改
            with open(cfg) as f:
                text = f.read()
        finally:
            if old is None:
                os.environ.pop("CODEX_HOME", None)
            else:
                os.environ["CODEX_HOME"] = old
        self.assertIn('"--mcp-bridge"', text)
        self.assertNotIn("url =", text)
        self.assertIn('model = "x"', text)
        self.assertIn('trust_level = "trusted"', text)
        try:
            import tomllib
        except ImportError:
            return
        j = tomllib.loads(text)
        self.assertTrue(j["mcp_servers"]["open-your-eyes"]["command"])

    def test_repair_workbuddy_keeps_other_settings(self):
        """WorkBuddy：填的网址换成桥接，名字和用户的开关设置不变，别的服务不动。"""
        import integrations
        d = tempfile.mkdtemp(prefix="t9wb-")
        self.addCleanup(shutil.rmtree, d, True)
        os.makedirs(os.path.join(d, ".workbuddy"))
        path = os.path.join(d, ".workbuddy", "mcp.json")
        with open(path, "w") as f:
            json.dump({"mcpServers": {"open-your-eyes": {"url": "http://127.0.0.1:39757/mcp", "disabled": False},
                                      "other": {"command": "x"}}}, f)
        old = integrations.HOME
        integrations.HOME = d
        try:
            self.assertTrue(integrations.repair_workbuddy("http://127.0.0.1:39757/mcp"))
            self.assertFalse(integrations.repair_workbuddy("http://127.0.0.1:39757/mcp"))
        finally:
            integrations.HOME = old
        with open(path) as f:
            j = json.load(f)["mcpServers"]
        self.assertEqual(j["other"], {"command": "x"})
        self.assertNotIn("url", j["open-your-eyes"])
        self.assertIn("--mcp-bridge", j["open-your-eyes"]["args"])
        self.assertIs(j["open-your-eyes"]["disabled"], False)

    def test_bridge_entry_for_claude_code(self):
        import integrations
        e = integrations._bridge_entry("http://127.0.0.1:39757/mcp")
        self.assertEqual(e["type"], "stdio")
        self.assertIn("--mcp-bridge", e["args"])
        self.assertTrue(integrations._is_current_bridge(e))
        self.assertFalse(integrations._is_current_bridge({"type": "http", "url": "http://127.0.0.1:39757/mcp"}))


class PageScriptSyntaxTest(unittest.TestCase):
    """每个页面里的脚本、桌面版扩展都要能通过语法检查（少一个括号，整页就跑不起来，而服务端测试发现不了）。
    需要电脑上有 node；没有就跳过。"""

    def _check(self, name, code):
        node = shutil.which("node")
        if not node:
            self.skipTest("没有 node，跳过页面脚本语法检查")
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
            f.write(code)
        try:
            r = subprocess.run([node, "--check", f.name], capture_output=True, text=True, timeout=30)
        finally:
            os.remove(f.name)
        self.assertEqual(r.returncode, 0, "%s 的脚本有语法错误：\n%s" % (name, r.stderr[-800:]))

    def test_web_pages(self):
        import re
        web = os.path.join(ROOT, "web")
        pages = sorted(n for n in os.listdir(web) if n.endswith(".html"))
        self.assertTrue(pages)
        for n in pages:
            with open(os.path.join(web, n), encoding="utf-8") as f:
                html = f.read()
            scripts = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S | re.I)
            for i, js in enumerate(scripts):
                with self.subTest(page=n, script=i):
                    # 包进 async 函数：页面脚本里可以直接写 await
                    self._check("%s 第 %d 段" % (n, i + 1), "(async function () {\n%s\n});\n" % js)

    def test_desktop_extension(self):
        p = os.path.join(ROOT, "desktop-extension", "server", "index.js")
        with open(p, encoding="utf-8") as f:
            code = f.read()
        if code.startswith("#!"):
            code = "//" + code
        self._check("desktop-extension/server/index.js", code)
