#!/bin/zsh
# 把上游命令行工具同步进 vendor/，让仓库保持自包含。
#
#   ./tools/sync-vendor.sh              # 从 ~/.local/bin 同步
#   GPXART_SRC=/path/to/bin ./tools/sync-vendor.sh
#
# 只做两处必要修改（其余原样搬运）：
#   1) shebang 里的写死解释器路径  ->  #!/usr/bin/env python3
#   2) gpxart 内的 RUNROUTE 常量   ->  优先找同目录的 runroute
set -e
cd "$(dirname "$0")/.."

SRC="${GPXART_SRC:-$HOME/.local/bin}"
for f in gpxart runroute; do
  [[ -f "$SRC/$f" ]] || { echo "找不到 $SRC/$f"; exit 1; }
  cp "$SRC/$f" "vendor/$f"
  chmod +x "vendor/$f"
done

python3 - <<'PY'
import pathlib, re

g = pathlib.Path("vendor/gpxart")
t = g.read_text(encoding="utf-8")
t = re.sub(r"^#![^\n]*", "#!/usr/bin/env python3", t, count=1)
t = t.replace(
    'RUNROUTE = os.path.expanduser("~/.local/bin/runroute")',
    'def _fn_find_runroute():\n'
    '    """优先用与本脚本同目录的 runroute（仓库自包含），再退回 ~/.local/bin。"""\n'
    '    here = os.path.dirname(os.path.abspath(__file__))\n'
    '    for p in (os.path.join(here, "runroute"),\n'
    '              os.path.expanduser("~/.local/bin/runroute")):\n'
    '        if os.path.isfile(p):\n'
    '            return p\n'
    '    return os.path.expanduser("~/.local/bin/runroute")\n'
    '\n'
    '\n'
    'RUNROUTE = _fn_find_runroute()')
g.write_text(t, encoding="utf-8")

r = pathlib.Path("vendor/runroute")
r.write_text(re.sub(r"^#![^\n]*", "#!/usr/bin/env python3",
                    r.read_text(encoding="utf-8"), count=1), encoding="utf-8")
print("已同步 vendor/gpxart、vendor/runroute")
PY
