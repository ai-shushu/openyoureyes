#!/usr/bin/env bash
# 开发用快捷方式（用户直接打开 Open Your Eyes 程序即可，不需要这个脚本）
#
#   ./eyes.sh up       后台启动（证书、地址变化、崩溃重启都由程序自己处理）
#   ./eyes.sh status   查看状态
#   ./eyes.sh down     停止
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$ROOT/.venv/bin/python"; [ -x "$PY" ] || PY=python3
mkdir -p "$ROOT/server/logs"

case "${1:-status}" in
  up)
    if "$PY" "$ROOT/app/openyoureyes.py" status | grep -q 运行中; then echo "Open Your Eyes 已在运行"; else
      nohup "$PY" "$ROOT/app/openyoureyes.py" --no-browser > "$ROOT/server/logs/launcher.log" 2>&1 &
      for _ in $(seq 1 40); do "$PY" "$ROOT/app/openyoureyes.py" status | grep -q 运行中 && break; sleep 0.25; done
      echo "Open Your Eyes 已启动"
    fi
    echo "设置页：http://127.0.0.1:${T9_WEB_PORT:-39757}/settings"
    ;;
  status)
    "$PY" "$ROOT/app/openyoureyes.py" status
    curl --noproxy '*' -fsS -m 2 "http://127.0.0.1:${T9_WEB_PORT:-39757}/api/status" 2>/dev/null | "$PY" -c '
import json, sys
d = json.load(sys.stdin)
for p in d["phones"] or [{"name": "手机", "online": False}]:
    print("%s：" % p["name"] + ("在线" if p["online"] else "离线") + ("，拍照页在前台" if p.get("visible") else ""))' 2>/dev/null || true
    ;;
  down)
    "$PY" "$ROOT/app/openyoureyes.py" stop
    pkill -f "$ROOT/server/serve.py" 2>/dev/null || true   # 兼容旧方式直接启动的页面服务
    ;;
  *) sed -n 2,7p "$0"; exit 1 ;;
esac
