#!/usr/bin/env python3
"""极简 Chrome DevTools 协议客户端（仅标准库），用于调试手机 Chrome 页面。

前置：adb forward tcp:9222 localabstract:chrome_devtools_remote
用法：
  python3 dev/cdp.py list
  python3 dev/cdp.py eval <页面id前缀> '<JS 表达式>'
  python3 dev/cdp.py close <页面id前缀>
"""
import base64
import json
import os
import socket
import struct
import sys
import urllib.request

BASE = "http://127.0.0.1:9222"
_np = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def pages():
    return [t for t in json.load(_np.open(BASE + "/json", timeout=3)) if t.get("type") == "page"]


def find(prefix):
    for t in pages():
        if t["id"].startswith(prefix):
            return t
    raise SystemExit("找不到页面 %s" % prefix)


def ws_call(ws_url, method, params):
    host_port, path = ws_url[len("ws://"):].split("/", 1)
    host, port = host_port.split(":")
    s = socket.create_connection((host, int(port)), timeout=10)
    key = base64.b64encode(os.urandom(16)).decode()
    s.sendall(("GET /%s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n" % (path, host_port, key)).encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        resp += s.recv(4096)
    payload = json.dumps({"id": 1, "method": method, "params": params}).encode()
    mask = os.urandom(4)
    hdr = bytes([0x81])
    n = len(payload)
    hdr += bytes([0x80 | n]) if n < 126 else (bytes([0x80 | 126]) + struct.pack(">H", n) if n < 65536 else bytes([0x80 | 127]) + struct.pack(">Q", n))
    s.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))
    buf = resp.split(b"\r\n\r\n", 1)[1]

    def need(k):
        nonlocal buf
        while len(buf) < k:
            chunk = s.recv(65536)
            if not chunk:
                raise EOFError
            buf += chunk

    while True:
        need(2)
        ln = buf[1] & 0x7F
        off = 2
        if ln == 126:
            need(4); ln = struct.unpack(">H", buf[2:4])[0]; off = 4
        elif ln == 127:
            need(10); ln = struct.unpack(">Q", buf[2:10])[0]; off = 10
        need(off + ln)
        frame, buf = buf[off:off + ln], buf[off + ln:]
        msg = json.loads(frame.decode("utf-8", "replace"))
        if msg.get("id") == 1:
            s.close()
            return msg


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    if cmd == "list":
        for t in pages():
            print(t["id"][:8], t["url"][:90])
    elif cmd == "eval":
        t = find(sys.argv[2])
        r = ws_call(t["webSocketDebuggerUrl"], "Runtime.evaluate",
                    {"expression": sys.argv[3], "returnByValue": True, "awaitPromise": True})
        res = r.get("result", {})
        print(json.dumps(res.get("result", {}).get("value", res), ensure_ascii=False, indent=1))
    elif cmd == "close":
        t = find(sys.argv[2])
        _np.open(BASE + "/json/close/" + t["id"], timeout=3).read()
        print("closed", t["url"])
