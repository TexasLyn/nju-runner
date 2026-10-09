#!/bin/zsh
# NJU Runner · 一键启动
#   本机内置 python，直接看界面、离线可用。
#
#   ./start.sh          用默认端口 8788
#   ./start.sh 9000     指定端口
#   双击「启动.command」同效（会自动打开浏览器）
#
# 指定解释器： NJURUNNER_PY=/path/to/python ./start.sh

set -e
cd "$(dirname "$0")"
PORT="${1:-8788}"

# ---- 找一个带 numpy 的 Python ----
# 顺序：环境变量 → 本机 WorkBuddy 虚拟环境 → PATH 上的 python3 → Homebrew/系统
CANDIDATES=(
  "${NJURUNNER_PY:-}"
  "$HOME/.workbuddy/binaries/python/envs/default/bin/python"
  "$(command -v python3 || true)"
  "/opt/homebrew/bin/python3"
  "/usr/local/bin/python3"
)
PY=""
for c in "${CANDIDATES[@]}"; do
  [[ -n "$c" && -x "$c" ]] || continue
  if "$c" -c "import numpy" 2>/dev/null; then PY="$c"; break; fi
done
if [[ -z "$PY" ]]; then
  echo "找不到带 numpy 的 Python。装一个：python3 -m pip install numpy"
  echo "或用 NJURUNNER_PY=/path/to/python ./start.sh 指定。"
  exit 1
fi

# ---- 依赖自检 ----
for f in vendor/gpxart vendor/runroute; do
  [[ -f "$f" ]] || { echo "缺 $f（图形 / 配速算法），仓库不完整？"; exit 1; }
done
if ! command -v pymobiledevice3 >/dev/null 2>&1 \
   && [[ ! -x "$HOME/.workbuddy/binaries/python/envs/default/bin/pymobiledevice3" ]]; then
  echo "提示：找不到 pymobiledevice3 —— 仍可画图、生成 GPX，但没法把轨迹推到手机。"
fi

# ---- 已经在跑就直接开浏览器 ----
if curl -s --noproxy '*' --max-time 2 "http://127.0.0.1:$PORT/api/shapes" >/dev/null 2>&1; then
  echo "服务已在运行 → http://127.0.0.1:$PORT"
  open "http://127.0.0.1:$PORT"
  exit 0
fi

# ---- 起服务，稍后自动打开浏览器 ----
"$PY" server.py "$PORT" &
SRV=$!
( sleep 2; open "http://127.0.0.1:$PORT" ) &
echo "NJU Runner 已启动 → http://127.0.0.1:$PORT    （Ctrl+C 结束）"
wait $SRV
