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
    def __init__(self, snap_dir=None):
        self.tmp = tempfile.mkdtemp(prefix="t9test-")
        self.snap = snap_dir or os.path.join(self.tmp, "snaps")
        os.makedirs(self.snap, exist_ok=True)
        self.port, self.https = _free_port(), _free_port()
        env = dict(os.environ, T9_WEB_PORT=str(self.port), T9_HTTPS_PORT=str(self.https), T9_SNAP_DIR=self.snap,
                   T9_CERT_DIR=os.path.join(self.tmp, "certs"), T9_NO_POWER="1")
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


class LegacyMigrationTest(unittest.TestCase):
    def test_existing_phones_stay_approved(self):
        tmp = tempfile.mkdtemp(prefix="t9snap-")
        with open(os.path.join(tmp, ".phones.json"), "w") as f:
            json.dump({"oldphone0001": {"name": "1 号机", "mac": None, "device": "iPhone"}, "older00002": "2 号机"}, f)
        s = Server(snap_dir=tmp)
        try:
            self.assertFalse(s.poll("oldphone0001").get("pending"))
            self.assertFalse(s.poll("older00002").get("pending"))
        finally:
            s.close()
            shutil.rmtree(tmp, ignore_errors=True)


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
