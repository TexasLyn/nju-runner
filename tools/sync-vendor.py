#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把上游命令行工具同步进 vendor/，让仓库保持自包含。

    python tools/sync-vendor.py                 # 从 ~/.local/bin 同步
    GPXART_SRC=/path/to/bin python tools/sync-vendor.py

做的是「搬过来 + 打几个跨平台补丁」。补丁一律用**带断言的字面替换**：
上游改了措辞、锚点找不到时会直接报错退出，而不是静默生成一份坏的副本。
（以前的版本是盲替换，改漏了也看不出来。）
"""
import os
import pathlib
import re
import shutil
import stat
import sys

HERE = pathlib.Path(__file__).resolve().parent.parent
VENDOR = HERE / "vendor"


def die(msg):
    print("错误: " + msg, file=sys.stderr)
    raise SystemExit(1)


def patch(text, pairs, label):
    """按顺序做字面替换；每个锚点必须命中，否则报错。"""
    for needle, repl, count in pairs:
        n = text.count(needle)
        if n == 0:
            die("%s: 锚点没找到（上游可能改过），需要同步修一下这个脚本：\n  %r"
                % (label, needle[:100]))
        if count == "all":
            text = text.replace(needle, repl)
        else:
            if n < count:
                die("%s: 锚点只出现 %d 次，期望至少 %d 次：\n  %r"
                    % (label, n, count, needle[:100]))
            text = text.replace(needle, repl, count)
    return text


# ----------------------------------------------------------------- runroute

RUNROUTE_PMD_BLOCK = '''IS_WIN = os.name == "nt"
PY_EXE = sys.executable or ("python" if IS_WIN else "python3")


def _find_pmd():
    """定位 pymobiledevice3：环境变量 \u2192 PATH \u2192 常见安装位置。

    以前这里是写死的绝对路径，导致别人 clone 下来点「开始模拟」直接报
    「找不到 pymobiledevice3」，`clear` 还会静默变成空操作。
    """
    for var in ("PMD", "PYMOBILEDEVICE3"):
        p = os.environ.get(var)
        if p and os.path.isfile(p):
            return p
    for name in ("pymobiledevice3", "pymobiledevice3.exe"):
        found = shutil.which(name)
        if found:
            return found
    for cand in (os.path.expanduser("~/.local/bin/pymobiledevice3"),
                 os.path.join(os.path.dirname(sys.executable), "pymobiledevice3"),
                 os.path.join(os.path.dirname(sys.executable), "pymobiledevice3.exe")):
        if os.path.isfile(cand):
            return cand
    return "pymobiledevice3"


PMD = _find_pmd()


def _pmd_available():
    return bool(shutil.which(PMD))


def _pmd_cmd(*args):
    """拼出可直接交给 subprocess 的命令行。

    PMD 若是 `.py` 脚本、或 Windows 上那种无扩展名的入口脚本，
    必须用解释器起 —— 直接执行会 WinError 193。
    """
    ext = os.path.splitext(PMD)[1].lower()
    if ext == ".py" or (IS_WIN and not ext):
        return [PY_EXE, PMD] + list(args)
    return [PMD] + list(args)'''

# 上游那行是写死的绝对路径（机器不同、用户名不同），所以用正则匹配，
# 而不是把某台机器的路径原文抄进仓库。
RUNROUTE_PMD_RE = re.compile(r'(?m)^PMD = "[^"\n]*pymobiledevice3"\s*\n')

RUNROUTE_PATCHES = [
    # 1) 补 shutil（PMD 解析要用）
    ('import random\nimport subprocess\n',
     'import random\nimport shutil\nimport subprocess\n', 1),
    # 2) 可用性判断走 PATH
    ('if not os.path.exists(PMD):', 'if not _pmd_available():', "all"),
    # 3) 找不到时给一句能照着做的提示
    ('sys.exit("找不到 pymobiledevice3: %s" % PMD)',
     'sys.exit("找不到 pymobiledevice3: %s\\n"\n'
     '                 "请 pip install pymobiledevice3，或用环境变量 PMD=<路径> 指定。" % PMD)', 1),
    # 4) 播放入口用 _pmd_cmd
    ('cmd = [PMD, "developer", "dvt", "simulate-location", "play", args.file]',
     'cmd = _pmd_cmd("developer", "dvt", "simulate-location", "play", args.file)', 1),
    # 5) clear 入口用 _pmd_cmd
    ('subprocess.run([PMD, "developer", "dvt", "simulate-location", "clear"],',
     'subprocess.run(_pmd_cmd("developer", "dvt", "simulate-location", "clear"),', 1),
]


# ------------------------------------------------------------------- gpxart

GPXART_RUNROUTE_BLOCK = '''def _fn_find_runroute():
    """优先用与本脚本同目录的 runroute（仓库自包含），再退回 ~/.local/bin。"""
    here = os.path.dirname(os.path.abspath(__file__))
    for p in (os.path.join(here, "runroute"),
              os.path.expanduser("~/.local/bin/runroute")):
        if os.path.isfile(p):
            return p
    return os.path.expanduser("~/.local/bin/runroute")


RUNROUTE = _fn_find_runroute()

# runroute 是无扩展名的 Python 脚本：Windows 上不能直接执行（WinError 193），
# 统一用当前解释器起，POSIX 下也一并统一，少一条平台分支。
PY_EXE = sys.executable or ("python" if os.name == "nt" else "python3")'''

GPXART_PATCHES = [
    ('RUNROUTE = os.path.expanduser("~/.local/bin/runroute")',
     GPXART_RUNROUTE_BLOCK, 1),
    ('cmd = [RUNROUTE, "make", "--from-gpx", tmp, "--pace", args.pace,',
     'cmd = [PY_EXE, RUNROUTE, "make", "--from-gpx", tmp, "--pace", args.pace,', 1),
]


# --------------------------------------------------------------------- 主流程

def main():
    src = pathlib.Path(os.environ.get("GPXART_SRC") or (pathlib.Path.home() / ".local/bin"))
    if not VENDOR.is_dir():
        VENDOR.mkdir(parents=True, exist_ok=True)

    for name, patches in (("runroute", RUNROUTE_PATCHES), ("gpxart", GPXART_PATCHES)):
        up = src / name
        if not up.is_file():
            die("找不到上游 %s" % up)
        text = up.read_text(encoding="utf-8")
        text = re.sub(r"^#![^\n]*", "#!/usr/bin/env python3", text, count=1)

        if name == "runroute":
            # PMD 那行是写死的绝对路径（各机器不同），必须用正则换掉
            text, n = RUNROUTE_PMD_RE.subn(RUNROUTE_PMD_BLOCK + "\n", text)
            if n != 1:
                die("runroute: 没找到 `PMD = \".../pymobiledevice3\"` 这一行"
                    "（上游可能改过），需要同步修一下这个脚本")

        text = patch(text, patches, name)
        out = VENDOR / name
        out.write_text(text, encoding="utf-8")
        out.chmod(out.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        print("已同步 vendor/%s（%d 行）" % (name, text.count("\n") + 1))

    # 兜底横扫：确认没有本机绝对路径泄漏进仓库
    leak_re = re.compile(r"/Users/[A-Za-z0-9._-]+/|/home/[A-Za-z0-9._-]+/|[A-Za-z]:\\\\Users\\\\")
    leaks = []
    for f in sorted(VENDOR.iterdir()):
        if f.is_file():
            for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                if leak_re.search(line):
                    leaks.append("vendor/%s:%d  %s" % (f.name, i, line.strip()[:80]))
    if leaks:
        die("vendor 里仍有本机绝对路径，别提交：\n  " + "\n  ".join(leaks))
    print("检查通过：vendor 里无本机绝对路径残留")


if __name__ == "__main__":
    main()
