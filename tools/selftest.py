#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""NJU Runner · 跨平台自检

在哪台机器上都能跑，**不碰手机、不碰定位**。四段：

    1. 环境     —— python / numpy / psutil / pymobiledevice3 是否就位
    2. 几何     —— 9 个图形：numpy 落点版 vs gpxart 原版，要求逐位一致
    3. 平台原语 —— 真起一个子进程，验证（在 Windows 上最关键的）
                   _child_pids / _suspend / _resume / _terminate
    4. 服务     —— 起 server.py，打一遍 preview / generate / check

    python tools/selftest.py            # 全跑
    python tools/selftest.py --quick    # 跳过第 4 段

退出码 0 = 全过，1 = 有失败。每段都会打 ✓ / ✗。
"""
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

FAILS = []


def head(n, title):
    print("\n" + "=" * 62)
    print("%d. %s" % (n, title))
    print("=" * 62)


def check(label, ok, detail=""):
    mark = "✓" if ok else "✗"
    print("  %s %-38s %s" % (mark, label, detail))
    if not ok:
        FAILS.append(label)
    return ok


# ------------------------------------------------------------------ 1 环境

def sec_env():
    head(1, "环境")
    print("  python  : %s" % sys.version.split()[0])
    print("  exe     : %s" % sys.executable)
    print("  os.name : %s    platform: %s" % (os.name, sys.platform))

    try:
        import numpy
        check("numpy 可用", True, "v" + numpy.__version__)
    except Exception as e:
        check("numpy 可用", False, str(e))

    import core
    check("psutil 可用（推荐）", core._psutil is not None,
          "有 —— 进程树/挂起走 psutil" if core._psutil is not None
          else "无 —— 退化到系统命令，Windows 上仍可用")
    check("检测到 pymobiledevice3", os.path.isfile(core.PMD) or bool(shutil.which(core.PMD)),
          core.PMD)
    print("  vendor  : %s" % core.RUNROUTE_PATH)
    return True


# ------------------------------------------------------------------ 2 几何

SHAPES = ["heart", "hearts", "heartnest", "heartsrow", "star",
          "infinity", "spiral", "nju", "520"]


def sec_geometry():
    head(2, "几何（numpy 落点 vs 原版）")
    import core
    print("  %-10s %-6s %12s %12s %9s" % ("shape", "ok", "Δ中心(m)", "Δ余量(m)", "加速比"))
    worst = 0.0
    for s in SHAPES:
        st = core.selftest(s)
        worst = max(worst, st["center_delta_m"], st["margin_delta_m"])
        if not st["ok"]:
            FAILS.append("几何 " + s)
        print("  %-10s %-6s %12.4f %12.4f %8.1fx"
              % (s, "✓" if st["ok"] else "✗",
                 st["center_delta_m"], st["margin_delta_m"], st["speedup"] or 0))
    check("9 个图形全部与原版一致", worst == 0.0, "最大偏差 %.4f m" % worst)
    return True


# ------------------------------------------------- 3 平台原语（不需要设备）

CHILD_SRC = '''import sys, time
out = sys.argv[1]
i = 0
while True:
    i += 1
    try:
        with open(out, "a") as f:
            f.write("%d\\n" % i)
    except Exception:
        pass
    time.sleep(0.05)
'''

PARENT_SRC = '''import subprocess, sys, time
subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]])
time.sleep(300)
'''


def _count_lines(path):
    try:
        with open(path) as f:
            return sum(1 for _ in f)
    except Exception:
        return 0


def sec_primitives():
    head(3, "平台原语（假进程，不碰设备）")
    import core

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="njur-selftest-"))
    child_py = tmp / "child.py"
    parent_py = tmp / "parent.py"
    child_py.write_text(CHILD_SRC, encoding="utf-8")
    parent_py.write_text(PARENT_SRC, encoding="utf-8")
    counter = tmp / "counter.txt"
    proc = None
    parent = None
    try:
        # --- 3a: 直接起的子进程，验挂起/恢复/终止
        proc = subprocess.Popen([sys.executable, str(child_py), str(counter)])
        time.sleep(0.6)
        n1 = _count_lines(counter)
        check("子进程在跑（计数在涨）", n1 > 0, "已计数 %d" % n1)

        ok_s = core._suspend(proc.pid)
        check("_suspend 返回成功", ok_s)
        time.sleep(0.15)
        n2 = _count_lines(counter)
        time.sleep(0.7)
        n3 = _count_lines(counter)
        check("挂起后计数冻结", n3 == n2, "%d → %d（等了 0.7s）" % (n2, n3))

        ok_r = core._resume(proc.pid)
        check("_resume 返回成功", ok_r)
        time.sleep(0.6)
        n4 = _count_lines(counter)
        check("恢复后计数继续涨", n4 > n3, "%d → %d" % (n3, n4))

        # --- 3b: 父子结构，验子进程枚举
        parent = subprocess.Popen([sys.executable, str(parent_py),
                                   str(child_py), str(tmp / "c2.txt")])
        time.sleep(1.0)
        kids = core._child_pids(parent.pid)
        check("_child_pids 找到子进程", len(kids) >= 1, "pids=%s" % kids)

        # --- 3c: 终止
        ok_t = core._terminate(proc.pid)
        time.sleep(0.6)
        check("_terminate 杀掉了进程", ok_t and proc.poll() is not None,
              "poll=%s" % proc.poll())
        for k in kids:
            core._terminate(k, force=True)
    finally:
        for p in (proc, parent):
            if p is not None and p.poll() is None:
                core._terminate(p.pid, force=True)
        shutil.rmtree(tmp, ignore_errors=True)
    return True


# ------------------------------------------------------------------ 4 服务

def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _post(port, path, payload):
    import json
    req = urllib.request.Request(
        "http://127.0.0.1:%d%s" % (port, path),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode("utf-8"))


def _get(port, path):
    import json
    with urllib.request.urlopen("http://127.0.0.1:%d%s" % (port, path), timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def sec_service():
    head(4, "服务端到端")
    port = _free_port()
    log = open(os.path.join(tempfile.gettempdir(), "njur-selftest-server.log"), "wb")
    proc = subprocess.Popen([sys.executable, str(HERE / "server.py"), str(port)],
                            cwd=str(HERE), stdout=log, stderr=subprocess.STDOUT)
    try:
        up = False
        for _ in range(40):
            time.sleep(0.25)
            try:
                _get(port, "/api/shapes")
                up = True
                break
            except Exception:
                if proc.poll() is not None:
                    break
        if not check("server.py 起来了", up, "端口 %d" % port):
            return False

        shp = _get(port, "/api/shapes")
        check("/api/shapes", bool(shp.get("shapes")), "%d 个图形" % len(shp.get("shapes") or []))

        pv = _post(port, "/api/preview", {"shape": "heart", "size": 560, "margin": 60})
        check("/api/preview", pv.get("length_m", 0) > 0,
              "%.0f m / 余量 %.0f m / %d 点"
              % (pv.get("length_m", 0), pv.get("margin_m", 0), len(pv.get("track") or [])))

        gn = _post(port, "/api/generate",
                   {"shape": "heart", "pace": "6:00", "laps": 1, "margin": 60})
        ok = gn.get("length_m", 0) > 0 and (gn.get("check") or {}).get("ok")
        check("/api/generate + 越界校验", bool(ok),
              "%s / %s / %d 点越界"
              % (gn.get("file"), gn.get("duration_hms"),
                 (gn.get("check") or {}).get("out_of_bounds", -1)))

        dev = _get(port, "/api/device")
        print("  · 设备：%s" % ("%s (iOS %s, %s)"
                                % (dev.get("name"), dev.get("ios"), dev.get("conn"))
                                if dev.get("connected") else "未连接 — " + str(dev.get("error"))))
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        log.close()
    return True


def main():
    quick = "--quick" in sys.argv
    print("NJU Runner · 自检     %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    sec_env()
    sec_geometry()
    sec_primitives()
    if not quick:
        sec_service()
    else:
        print("\n（--quick：跳过服务段）")

    print("\n" + "=" * 62)
    if FAILS:
        print("结果：%d 项失败 ✗" % len(FAILS))
        for f in FAILS:
            print("  - " + f)
        return 1
    print("结果：全部通过 ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
