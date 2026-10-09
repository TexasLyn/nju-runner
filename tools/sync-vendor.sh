#!/bin/zsh
# 方便用的壳：真正干活的是 tools/sync-vendor.py（跨平台）。
#
#   ./tools/sync-vendor.sh              # 从 ~/.local/bin 同步
#   GPXART_SRC=/path/to/bin ./tools/sync-vendor.sh
#
# Windows 上直接跑： python tools\sync-vendor.py
set -e
cd "$(dirname "$0")/.."
exec python3 tools/sync-vendor.py
