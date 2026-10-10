# -*- coding: utf-8 -*-
"""
NJU Runner · core
=================
把三样能力包成本模块，供 WebUI 调用：

    gpxart    —— 画什么形状、摆在哪里、会不会越界
    runroute  —— 走多快、时间戳、GPS 漂移、多圈往复、配速斜坡
    pymobiledevice3 —— 真机定位注入与收尾

前两者随仓库内置在 `vendor/`（上游是 `~/.local/bin/` 下的同名脚本），
所以别人 clone 下来就能跑，不必先装这两个命令行工具。

设计原则
--------
1. **不改**被依赖脚本的逻辑，只 import 它们的函数（两者都有 `__main__` 保护）；
   内置副本仅改动 shebang 与同目录查找，便于 `tools/sync-vendor.sh` 同步上游。
2. 落点搜索用 numpy 重写：原版纯 Python 一次要几秒，WebUI 实时预览扛不住。
   `selftest()` 会拿同一输入跟 `gpxart.best_placement` 对比，确保结果对齐。
3. 边界解析、候选中心都做进程内缓存；同参数结果再做一层 LRU。
"""
import hashlib
import importlib.machinery
import importlib.util
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time

import numpy as np

HOME = os.path.expanduser("~")

HERE = os.path.dirname(os.path.abspath(__file__))
VENDOR = os.path.join(HERE, "vendor")
BIN = os.path.join(HOME, ".local/bin")

OUT_DIR = os.path.join(HERE, "out")
DATA_DIR = os.path.join(HERE, "data")
BOUND_DIR = os.path.join(DATA_DIR, "boundaries")
TRACK_DIR = os.path.join(DATA_DIR, "tracks")
TILE_CACHE = os.path.join(DATA_DIR, "tilecache")
PLAY_LOG = os.path.join(OUT_DIR, "play.log")

# 校区中文短名：文件名 slug → 显示名。新增校区只要往 data/boundaries 丢文件即可，
# 这里只是让下拉里显示的短一点（长名走 GeoJSON 的 properties.name）。
CAMPUS_LABEL = {
    "gulou": "鼓楼",
    "xianlin": "仙林",
    "pukou": "浦口",
    "suzhou": "苏州",
}

os.makedirs(OUT_DIR, exist_ok=True)


def _find_tool(name, env_var):
    """三级解析：环境变量 → 仓库内置 vendor/ → ~/.local/bin。"""
    for cand in (os.environ.get(env_var),
                 os.path.join(VENDOR, name),
                 os.path.join(BIN, name)):
        if cand and os.path.isfile(cand):
            return cand
    raise SystemExit("找不到 %s（可用环境变量 %s 指定路径）" % (name, env_var))


GPXART_PATH = _find_tool("gpxart", "GPXART_PATH")
RUNROUTE_PATH = _find_tool("runroute", "RUNROUTE_PATH")


def _find_pmd():
    """pymobiledevice3 的位置：环境变量 → PATH → 常见虚拟环境。

    找不到时返回裸名字交给 PATH 解析——缺它只影响真机注入，
    生成 GPX 的整条链路依然可用。
    """
    for var in ("PMD", "PYMOBILEDEVICE3"):
        p = os.environ.get(var)
        if p and os.path.isfile(p):
            return p
    for name in ("pymobiledevice3", "pymobiledevice3.exe"):
        found = shutil.which(name)
        if found:
            return found
    for cand in (os.path.join(HOME, ".local/bin/pymobiledevice3"),
                 os.path.join(os.path.dirname(sys.executable), "pymobiledevice3"),
                 os.path.join(os.path.dirname(sys.executable), "pymobiledevice3.exe")):
        if os.path.isfile(cand):
            return cand
    return "pymobiledevice3"


PMD = _find_pmd()


# ---------------------------------------------------------------- 平台适配
#
# 这个项目原本是照着 macOS 写的：用 `pgrep` 找子进程、用 `SIGSTOP`/`SIGCONT`
# 做暂停。Windows 上这两样都没有 —— `pgrep` 是 Unix 命令，`signal.SIGSTOP`
# 在 Windows 上连属性都不存在，被 except 吞掉后会变成「暂停按钮显示成功、
# 进度照跑」的假成功，比直接报错还糟。所以统一收口到这几个函数。
#
# 优先级：psutil（三平台都有，语义最干净）→ 系统原生命令。

IS_WIN = os.name == "nt"
PY_EXE = sys.executable or ("python" if IS_WIN else "python3")

try:
    import psutil as _psutil
except Exception:                       # 没装也能跑，只是退化到系统命令
    _psutil = None


def _detached_kwargs():
    """后台起进程的参数。

    `start_new_session=True` 在 Windows 上会被**静默忽略**，得改用
    DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP。注入进程必须脱离本进程，
    否则服务一退出就把它连坐带走。
    """
    if IS_WIN:
        return {"creationflags": 0x00000008 | 0x00000200}
    return {"start_new_session": True}


def _script_cmd(path, *args):
    """拼出执行 vendor 脚本的命令行。

    这些脚本没有扩展名，Windows 的 CreateProcess 直接执行会 WinError 193，
    所以统一交给当前解释器。POSIX 下也走同一条路，少一个平台分支。
    """
    return [PY_EXE, path] + [str(a) for a in args]


def _pmd_cmd(*args):
    """拼出执行 pymobiledevice3 的命令行（同上的扩展名问题）。"""
    ext = os.path.splitext(PMD)[1].lower()
    if ext == ".py" or (IS_WIN and not ext):
        return [PY_EXE, PMD] + [str(a) for a in args]
    return [PMD] + [str(a) for a in args]


def _nt_process_call(func_name, pid):
    """Windows 挂起/恢复进程。

    kernel32 只有 TerminateProcess；挂起得调 ntdll 的 NtSuspendProcess /
    NtResumeProcess，句柄需要 PROCESS_SUSPEND_RESUME(0x0800) 权限。
    """
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        h = k32.OpenProcess(0x0800, False, int(pid))
        if not h:
            return False
        try:
            fn = getattr(ctypes.WinDLL("ntdll"), func_name)
            fn.argtypes = [wintypes.HANDLE]
            fn.restype = ctypes.c_long
            return fn(h) == 0            # NTSTATUS 0 = STATUS_SUCCESS
        finally:
            k32.CloseHandle(h)
    except Exception:
        return False


def _child_pids(pid):
    """列出某个进程的所有子进程。"""
    if _psutil is not None:
        try:
            return [c.pid for c in _psutil.Process(pid).children(recursive=True)]
        except Exception:
            return []
    if not IS_WIN:
        try:
            out = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True,
                                 text=True, timeout=5).stdout
            return [int(x) for x in out.split()]
        except Exception:
            return []
    # Windows 无 psutil：wmic（Win10 常见）→ PowerShell CIM（Win11 24H2 起 wmic 被移除）
    try:
        out = subprocess.run(
            ["wmic", "process", "where", "ParentProcessId=%d" % pid,
             "get", "ProcessId", "/value"],
            capture_output=True, text=True, timeout=10).stdout
        pids = [int(m.group(1)) for m in re.finditer(r"ProcessId=(\d+)", out)]
        if pids:
            return pids
    except Exception:
        pass
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-CimInstance Win32_Process -Filter "
             '"ParentProcessId=%d" | Select-Object -ExpandProperty ProcessId' % pid],
            capture_output=True, text=True, timeout=20).stdout
        return [int(x) for x in out.split()]
    except Exception:
        return []


def _suspend(pid):
    """冻结进程（连它的 sleep 计时一起冻）。"""
    if _psutil is not None:
        try:
            _psutil.Process(int(pid)).suspend()
            return True
        except Exception:
            return False
    if IS_WIN:
        return _nt_process_call("NtSuspendProcess", pid)
    try:
        os.kill(int(pid), signal.SIGSTOP)
        return True
    except Exception:
        return False


def _resume(pid):
    """解冻进程。"""
    if _psutil is not None:
        try:
            _psutil.Process(int(pid)).resume()
            return True
        except Exception:
            return False
    if IS_WIN:
        return _nt_process_call("NtResumeProcess", pid)
    try:
        os.kill(int(pid), signal.SIGCONT)
        return True
    except Exception:
        return False


def _terminate(pid, force=False):
    """结束进程。force=True 时连子进程一起。"""
    if _psutil is not None:
        try:
            p = _psutil.Process(int(pid))
            if force:
                for c in p.children(recursive=True):
                    try:
                        c.kill()
                    except Exception:
                        pass
            (p.kill if force else p.terminate)()
            return True
        except Exception:
            return False
    if not IS_WIN:
        try:
            os.kill(int(pid), signal.SIGKILL if force else signal.SIGTERM)
            return True
        except Exception:
            return False
    try:
        cmd = ["taskkill", "/PID", str(int(pid)), "/F"] + (["/T"] if force else [])
        subprocess.run(cmd, capture_output=True, timeout=15)
        return True
    except Exception:
        return False


def _scan_simulate_play():
    """扫描系统里所有 `simulate-location play` 进程，返回 [(pid, gpx 路径), ...]。"""
    hits = []
    if _psutil is not None:
        for p in _psutil.process_iter(["pid", "cmdline"]):
            try:
                cl = p.info.get("cmdline") or []
            except Exception:
                continue
            if len(cl) < 2:
                continue
            s = " ".join(cl)
            if "simulate-location" not in s or " play" not in s:
                continue
            gpxs = [a for a in cl if a.lower().endswith(".gpx")]
            if gpxs:
                hits.append((int(p.info["pid"]), gpxs[-1]))
        return hits
    if IS_WIN:
        return []                        # 无 psutil 时不做外部会话探测，避免拖慢
    try:
        res = subprocess.run(["pgrep", "-fl", "simulate-location play"],
                             capture_output=True, text=True, timeout=5)
    except Exception:
        return []
    for line in (res.stdout or "").splitlines():
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        m = re.search(r"simulate-location play\s+(.+?)\s*$", parts[1])
        if m:
            hits.append((int(parts[0]), m.group(1).strip()))
    return hits


# ---------------------------------------------------------------- 加载原脚本

def _load_module(name, path):
    loader = importlib.machinery.SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    loader.exec_module(mod)
    return mod


gpxart = _load_module("_gpxart_core", GPXART_PATH)
runroute = _load_module("_runroute_core", RUNROUTE_PATH)

M_PER_DEG = gpxart.M_PER_DEG


# ---------------------------------------------------------------- 几何：numpy 加速

def _poly_arrays(poly):
    """把多边形转成一组广播用的 ndarray。"""
    Q = np.asarray(poly, dtype=float)
    A = Q
    B = np.roll(Q, -1, axis=0)
    D = B - A
    L2 = (D * D).sum(axis=1)
    L2s = np.where(L2 == 0.0, 1.0, L2)
    return Q, A, B, D, L2s


def _dist_batch(P, A, B, D, L2s):
    """P: (N,2) -> (N,) 点到多边形边界的带符号距离（正 = 在多边形内）。

    语义与 gpxart.dist_to_poly 完全一致：距离取到最近边的欧氏距离，
    内外用射线法判定。
    """
    if len(P) == 0:
        return np.zeros(0)
    V = P[:, None, :] - A[None, :, :]                      # (N,M,2)
    t = (V * D[None, :, :]).sum(axis=2) / L2s[None, :]      # (N,M)
    np.clip(t, 0.0, 1.0, out=t)
    C = A[None, :, :] + t[:, :, None] * D[None, :, :]       # (N,M,2)
    diff = P[:, None, :] - C
    dist = np.sqrt((diff * diff).sum(axis=2))               # (N,M)
    best = dist.min(axis=1)

    # 射线法判内外（与 dist_to_poly 同规则，含 (y1 > y) != (y2 > y) 的半开区间约定）
    x = P[:, 0][:, None]
    y = P[:, 1][:, None]
    y1 = A[None, :, 1]
    y2 = B[None, :, 1]
    x1 = A[None, :, 0]
    x2 = B[None, :, 0]
    dy = y2 - y1
    safe = np.where(dy == 0, 1.0, dy)
    xin = (x2 - x1) * (y - y1) / safe + x1
    cond = (y1 > y) != (y2 > y)
    cross = cond & (x < xin)
    inside = (cross.sum(axis=1) % 2) == 1
    return np.where(inside, best, -best)


def _candidate_centers(poly, step=12.0):
    """按到边界的距离降序列出候选中心点，等价 gpxart.candidate_centers。"""
    Q, A, B, D, L2s = _poly_arrays(poly)
    xs, ys = Q[:, 0], Q[:, 1]
    x0, x1 = xs.min(), xs.max()
    y0, y1 = ys.min(), ys.max()
    gx = np.arange(0.0, (x1 - x0) + 1e-9, step)
    gy = np.arange(0.0, (y1 - y0) + 1e-9, step)
    XX, YY = np.meshgrid(x0 + gx, y0 + gy)
    P = np.stack([XX.ravel(), YY.ravel()], axis=1)

    chunks = []
    for i in range(0, len(P), 4000):
        blk = P[i:i + 4000]
        d = _dist_batch(blk, A, B, D, L2s)
        m = d > 0
        if m.any():
            chunks.append(np.concatenate([blk[m], d[m][:, None]], axis=1))
    arr = np.concatenate(chunks, axis=0) if chunks else np.zeros((0, 3))
    order = np.argsort(-arr[:, 2])
    arr = arr[order]
    return [(float(r[2]), (float(r[0]), float(r[1]))) for r in arr]


def _local_maxima(cands, sep=110.0, limit=90):
    picked = []
    for d, p in cands:
        if all((p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2 > sep * sep for _, q in picked):
            picked.append((d, p))
        if len(picked) >= limit:
            break
    return picked


_boundary_cache = {}
_cand_cache = {}
_cache_lock = threading.Lock()


def load_boundary(boundary_path):
    """返回 (ring, frame, poly)，带缓存。radius 用不着时也一并缓存候选中心。"""
    key = os.path.abspath(boundary_path)
    with _cache_lock:
        if key in _boundary_cache:
            return _boundary_cache[key]
    ring = gpxart.read_boundary(key)
    frame = gpxart.Frame(ring)
    poly = [frame.fwd(lo, la) for lo, la in ring]
    val = (ring, frame, poly)
    with _cache_lock:
        _boundary_cache[key] = val
    return val


def candidate_centers_cached(poly, step=12.0, limit=90):
    key = (round(poly[0][0], 3), round(poly[0][1], 3), len(poly), step, limit)
    with _cache_lock:
        if key in _cand_cache:
            return _cand_cache[key]
    val = _local_maxima(_candidate_centers(poly, step), limit=limit)
    with _cache_lock:
        _cand_cache[key] = val
    return val


def place_shape(poly, shape_pts, name, need=60.0, avoid=None, limit=90, step=12.0):
    """在边界内找最优落点。返回 (margin, rot, cx, cy, placed_pts)。

    语义照搬 gpxart.best_placement：**正放优先**，倾斜只在正放余量不足 need 时兜底。
    """
    Q, A, B, D, L2s = _poly_arrays(poly)
    cands = candidate_centers_cached(poly, step=step, limit=limit)
    if avoid:
        keep = [c for c in cands
                if not any((c[1][0] - ax) ** 2 + (c[1][1] - ay) ** 2 < r * r
                           for ax, ay, r in avoid)]
        if keep:
            cands = keep

    base = np.asarray(shape_pts, dtype=float)

    def scan(rot):
        """两级搜索：抽稀点粗筛出候选中心，再用全点精算最优的那个。

        直接全点扫（原版做法）在 heartsrow 这种 1000+ 点的图形上要 1.3 s，
        拖不动滑块。粗筛只用来排序，最终余量一定由全点算出，不会虚高。
        """
        rot_pts = base if rot == 0 else np.asarray(gpxart.rotate(shape_pts, rot), dtype=float)
        k = max(1, len(rot_pts) // 300)
        coarse = rot_pts[::k]
        scored = []
        for _, (cx, cy) in cands:
            m = float(_dist_batch(coarse + np.array([cx, cy]), A, B, D, L2s).min())
            scored.append((m, cx, cy))
        scored.sort(key=lambda z: -z[0])

        bestm = None
        for _, cx, cy in scored[:6]:
            pts = rot_pts + np.array([cx, cy])
            m = float(_dist_batch(pts, A, B, D, L2s).min())
            if bestm is None or m > bestm[0]:
                bestm = (m, cx, cy, pts)
        return bestm

    m0, cx0, cy0, pts0 = scan(0)
    if m0 >= need:
        return (m0, 0, cx0, cy0, [tuple(p) for p in pts0])

    best = (m0, 0, cx0, cy0, pts0)
    for rot in sorted(gpxart.ROTATIONS.get(name, [0]), key=lambda r: (abs(r), r)):
        if rot == 0:
            continue
        m, cx, cy, pts = scan(rot)
        if m - 0.8 * abs(rot) > best[0] - 0.8 * abs(best[1]):
            best = (m, rot, cx, cy, pts)
    return (best[0], best[1], best[2], best[3], [tuple(p) for p in best[4]])


# ---------------------------------------------------------------- 对外：图形与边界

def list_shapes():
    out = []
    for key, (fn, size, cn) in gpxart.SHAPES.items():
        out.append({
            "key": key, "name": cn, "default_size": size,
            "rotations": gpxart.ROTATIONS.get(key, [0]),
        })
    return sorted(out, key=lambda s: s["default_size"])


def _read_props(path):
    """读 GeoJSON 的 properties（拿 name / source / campus 这类元数据）。失败返回 {}。"""
    try:
        raw = json.load(open(path, encoding="utf-8"))
        if isinstance(raw, dict) and raw.get("type") == "FeatureCollection":
            return raw["features"][0].get("properties") or {}
        if isinstance(raw, dict) and raw.get("type") == "Feature":
            return raw.get("properties") or {}
    except Exception:
        pass
    return {}


def _campus_of(slug):
    """从文件名里抠出校区短名：boundary-nju-xianlin / track-nju-xianlin-1 → xianlin。"""
    for key in CAMPUS_LABEL:
        if key in (slug or ""):
            return key
    return ""


def list_boundaries():
    """列出可用边界（= 校区）。带显示名、校区短名、面积与中心。"""
    out = []
    if os.path.isdir(BOUND_DIR):
        for fn in sorted(os.listdir(BOUND_DIR)):
            if not fn.lower().endswith((".geojson", ".json")):
                continue
            path = os.path.join(BOUND_DIR, fn)
            props = _read_props(path)
            slug = fn.rsplit(".", 1)[0].replace("boundary-", "").replace("nju-", "")
            campus = _campus_of(fn)
            try:
                ring = gpxart.read_boundary(path)
                frame = gpxart.Frame(ring)
                poly = [frame.fwd(lo, la) for lo, la in ring]
                area = abs(gpxart.shoelace(poly)) / 1e6
                clon, clat = frame.inv(
                    sum(p[0] for p in poly) / len(poly),
                    sum(p[1] for p in poly) / len(poly))
            except Exception:
                area, clat, clon = 0.0, 0.0, 0.0
            out.append({
                "file": fn,
                "name": props.get("name") or slug,
                "label": CAMPUS_LABEL.get(campus, slug),
                "campus": campus,
                "source": props.get("source") or "",
                "area_km2": round(area, 3),
                "center": [round(clat, 6), round(clon, 6)],
                "points": 0,
            })
            try:
                out[-1]["points"] = len(gpxart.read_boundary(path))
            except Exception:
                pass
    return out


# ---------------------------------------------------------------- 真实操场（OSM 轮廓）

def read_track(path):
    """读操场轮廓，返回 (lon, lat) 点列。支持 LineString / Polygon / FeatureCollection。"""
    raw = json.load(open(path, encoding="utf-8"))
    if isinstance(raw, dict) and raw.get("type") == "FeatureCollection":
        geom = raw["features"][0]["geometry"]
    elif isinstance(raw, dict) and raw.get("type") == "Feature":
        geom = raw["geometry"]
    else:
        geom = raw
    t = geom.get("type")
    if t == "LineString":
        return [(float(p[0]), float(p[1])) for p in geom["coordinates"]]
    if t == "Polygon":
        return [(float(p[0]), float(p[1])) for p in geom["coordinates"][0]]
    raise ValueError("操场文件需为 LineString/Polygon，实际是 %s" % t)


def _close_ring(pts):
    """把首尾接上（OSM 的 way 有时不显式闭合）。"""
    if len(pts) >= 2 and (abs(pts[0][0] - pts[-1][0]) > 1e-9 or abs(pts[0][1] - pts[-1][1]) > 1e-9):
        return list(pts) + [pts[0]]
    return list(pts)


def _track_path(track_file):
    return track_file if os.path.isabs(track_file) else os.path.join(TRACK_DIR, track_file)


def _bbox_view(pts_ll, pad_ratio=0.22, pad_min=70.0):
    """给操场算一个「取景框」矩形（lat/lon），供前端把画面套上去。

    用操场自己的外接矩形 + 内边距，而不是整个校区边界 —— 标准操场只占校区一角，
    按校界取景会缩得太小、跑道只剩一条线。
    """
    lons = [p[0] for p in pts_ll]
    lats = [p[1] for p in pts_ll]
    lon0, lon1 = min(lons), max(lons)
    lat0, lat1 = min(lats), max(lats)
    w = (lon1 - lon0) * gpxart.m_per_deg_lon((lat0 + lat1) / 2)
    h = (lat1 - lat0) * M_PER_DEG
    pad = max(pad_min, pad_ratio * max(w, h))
    dl = pad / gpxart.m_per_deg_lon((lat0 + lat1) / 2)
    dla = pad / M_PER_DEG
    return [[lat0 - dla, lon0 - dl], [lat0 - dla, lon1 + dl],
            [lat1 + dla, lon1 + dl], [lat1 + dla, lon0 - dl]]


def list_tracks():
    """列出内置的真实操场。每条带：所属校区、名称、长度、中心、圈数建议。"""
    out = []
    if not os.path.isdir(TRACK_DIR):
        return out
    for fn in sorted(os.listdir(TRACK_DIR)):
        if not fn.lower().endswith((".geojson", ".json")):
            continue
        path = os.path.join(TRACK_DIR, fn)
        props = _read_props(path)
        try:
            pts = _close_ring(read_track(path))
            frame = gpxart.Frame(pts)
            xy = [frame.fwd(lo, la) for lo, la in pts]
            length = gpxart.path_len(xy)
            clon, clat = frame.inv(
                sum(p[0] for p in xy) / len(xy), sum(p[1] for p in xy) / len(xy))
        except Exception as e:
            out.append({"file": fn, "name": props.get("name") or fn, "error": str(e)})
            continue
        campus = props.get("campus") or _campus_of(fn)
        out.append({
            "file": fn,
            "name": props.get("name") or fn,
            "campus": campus,
            "label": CAMPUS_LABEL.get(campus, campus),
            "lane": props.get("lane") or "",
            "source": props.get("source") or "",
            "length_m": round(length, 1),
            "center": [round(clat, 6), round(clon, 6)],
            "points": len(pts),
        })
    # 按「校区顺序（鼓楼→仙林→浦口→苏州）→ 长度」排
    order = {k: i for i, k in enumerate(["gulou", "xianlin", "pukou", "suzhou"])}
    out.sort(key=lambda t: (order.get(t.get("campus"), 99), t.get("length_m") or 0))
    return out


def preview_track(track_file, direction="ccw"):
    """操场预览：不做落点搜索 —— 真实跑道就长在那儿。

    返回结构与 preview() 对齐（track / view / center / length_m），
    前端那套绘制代码不用改。
    """
    path = _track_path(track_file)
    if not os.path.exists(path):
        raise FileNotFoundError("找不到操场文件：%s" % track_file)
    pts = _close_ring(read_track(path))
    if direction == "cw":
        pts = list(reversed(pts))

    frame = gpxart.Frame(pts)
    xy = [frame.fwd(lo, la) for lo, la in pts]
    length = gpxart.path_len(xy)
    clon, clat = frame.inv(sum(p[0] for p in xy) / len(xy),
                           sum(p[1] for p in xy) / len(xy))
    props = _read_props(path)
    campus = props.get("campus") or _campus_of(track_file)

    campus_ring = None
    campus_name = ""
    for b in list_boundaries():
        if b["campus"] == campus:
            try:
                r = gpxart.read_boundary(os.path.join(BOUND_DIR, b["file"]))
                campus_ring = [[p[1], p[0]] for p in r]
                campus_name = b["name"]
            except Exception:
                pass
            break

    view = _bbox_view(pts)
    return {
        "kind": "track",
        "shape": "track", "shape_name": props.get("name") or "操场跑道",
        "size": length, "rotation": 0,
        "track_file": track_file,
        "campus": campus, "campus_name": campus_name,
        "center": [clat, clon],
        "length_m": length,
        "margin_m": 0.0,
        "closed": True,
        "area_km2": 0.0,
        "direction": direction,
        "view": view,
        # 操场模式没有「校界内落点」这回事：boundary 留空，校园轮廓单独给
        "boundary": None,
        "campus_boundary": campus_ring,
        "track": [[p[1], p[0]] for p in pts],
        "ok": True,
    }



# ---------------------------------------------------------------- 余量与越界分级

# 越界深度 ≤ 1 km 算黄色警告，超过 1 km 算红色警告（前端按 level 上色）
BOUND_WARN_M = 1000.0


def margin_level(margin_m):
    """把「离边界最小余量」分成三级。

    margin_m 正数 = 界内还剩多少米；负数 = 越界多少米。
    越界 1 km 以内 → "warn"（黄）；超过 1 km → "danger"（红）。
    """
    over = -float(margin_m)
    if over <= 0:
        return "ok"
    return "warn" if over <= BOUND_WARN_M else "danger"


def margin_note(margin_m, margin=60.0):
    """给 UI 的一句话说明。前端只按 level 决定配色，文案由这里统一给。"""
    level = margin_level(margin_m)
    over = -float(margin_m)
    if level == "ok":
        if margin_m < margin * 0.9:
            return "余量只有 %.0f m（目标 %.0f m），已尽量贴边放置" % (margin_m, margin)
        return None
    if level == "warn":
        return ("图形越过校界约 %.0f m —— 校界只是个参考，照样能生成；"
                "想完全落在校内，把「图形大小」调小" % over)
    return ("图形越过校界约 %.1f km —— 离校区已经很远，"
            "手机上的轨迹会明显跑到校外" % (over / 1000.0))


def _margin_fields(margin_m, margin=60.0):
    return {
        "margin_m": float(margin_m),
        "over_m": max(0.0, -float(margin_m)),
        "level": margin_level(margin_m),
        "ok": float(margin_m) > 0,
        "note": margin_note(margin_m, margin),
    }


def eval_points(points, boundary_file, margin=60.0):
    """给一串 [lat,lon] 点列，算它相对校界的余量。**不落盘、不调 runroute。**

    前端拖完轨迹后复核用：拖动是浏览器里本地平移的，而精确余量
    （点到边界的最短距离）得交给 numpy 算，所以松手后回这一趟。
    """
    boundary_path = boundary_file if os.path.isabs(boundary_file) \
        else os.path.join(BOUND_DIR, boundary_file)
    ring, frame, poly = load_boundary(boundary_path)
    Q, A, B, D, L2s = _poly_arrays(poly)

    pts = [(float(p[0]), float(p[1])) for p in points]      # [lat,lon]
    P = np.asarray([frame.fwd(lo, la) for la, lo in pts], dtype=float)
    d = _dist_batch(P, A, B, D, L2s)
    margin_m = float(d.min()) if len(d) else 0.0
    lats = [p[0] for p in pts]
    lons = [p[1] for p in pts]

    out = {
        "points": len(pts),
        "out_of_bounds": int((d < 0).sum()) if len(d) else 0,
        "center": [sum(lats) / len(lats), sum(lons) / len(lons)],
        "boundary": [[p[1], p[0]] for p in ring],
    }
    out.update(_margin_fields(margin_m, margin))
    return out


# ---------------------------------------------------------------- 预览（纯几何，不补时间）

def preview(shape, boundary_file, size=None, margin=60.0, seed_ignore=None,
            avoid=None, quality="fast"):
    """只算几何：形状 + 落点 + 余量。不调 runroute，所以毫秒级。"""
    boundary_path = boundary_file if os.path.isabs(boundary_file) \
        else os.path.join(BOUND_DIR, boundary_file)
    ring, frame, poly = load_boundary(boundary_path)

    if shape not in gpxart.SHAPES:
        raise ValueError("未知图形: %s" % shape)
    fn, default_size, cn = gpxart.SHAPES[shape]
    size = float(size or default_size)
    shape_pts = gpxart.normalize(fn(), size)

    limit, step = (60, 14.0) if quality == "fast" else (90, 12.0)
    margin_m, rot, cx, cy, placed = place_shape(
        poly, shape_pts, shape, need=max(margin, 0.0), avoid=avoid,
        limit=limit, step=step)

    clon, clat = frame.inv(cx, cy)
    pts_ll = [frame.inv(x, y) for (x, y) in placed]

    # 天然闭合才收尾；开放笔画（字母/螺旋）强行闭合会多出一条横贯全图的直线
    gap = math.hypot(placed[-1][0] - placed[0][0], placed[-1][1] - placed[0][1])
    closed = gap < 25.0
    if closed:
        pts_ll = pts_ll + [pts_ll[0]]

    length = gpxart.path_len(placed)
    area = abs(gpxart.shoelace(poly)) / 1e6
    out = {
        "kind": "shape",
        "shape": shape, "shape_name": cn,
        "size": size, "rotation": rot,
        "center": [clat, clon],
        "length_m": length,
        "closed": closed,
        "area_km2": area,
        # view = 取景框（图形版就是校界本身）；前端统一按 view 投影
        "view": [[p[1], p[0]] for p in ring],
        "boundary": [[p[1], p[0]] for p in ring],       # [lat,lon]
        "campus_boundary": None,
        "track": [[p[1], p[0]] for p in pts_ll],        # [lat,lon]
    }
    out.update(_margin_fields(margin_m, margin))
    return out


# ---------------------------------------------------------------- 越界校验（沿线段加密）

def check_bounds(gpx_path, boundary_file, margin=50.0, step=5.0):
    boundary_path = boundary_file if os.path.isabs(boundary_file) \
        else os.path.join(BOUND_DIR, boundary_file)
    ring, frame, poly = load_boundary(boundary_path)
    Q, A, B, D, L2s = _poly_arrays(poly)

    pts = gpxart.read_gpx_ll(gpx_path)
    ll = [(p[0], p[1]) for p in pts]
    dense = gpxart.densify(ll, step=step)
    P = np.asarray([frame.fwd(lo, la) for lo, la in dense], dtype=float)
    d = _dist_batch(P, A, B, D, L2s)
    off = int((d < 0).sum())
    return {
        "samples": len(dense), "out_of_bounds": off,
        "min_margin": float(d.min()), "max_margin": float(d.max()),
        "required": margin, "ok": off == 0 and float(d.min()) >= margin,
    }


# ---------------------------------------------------------------- 生成 GPX（补时间戳）

# 前端为了好看，配速发的是 `6'00"`；vendor/runroute 的 parse_pace 只认 `6:00`
# 和纯秒数（`360`）。所以在这一层统一成 `M:SS` 再交给 vendor —— 不去改 vendor，
# 免得 tools/sync-vendor.py 重同步时把补丁冲掉。
_PACE_RE = re.compile(r"^\s*(\d+)\s*[:'\u2032]\s*(\d+(?:\.\d+)?)\s*[\"\u2033]?\s*$")
_PACE_PLAIN_RE = re.compile(r"^\s*\d+(?:\.\d+)?\s*$")


def _norm_pace(text, what="配速"):
    """各种写法 → vendor 认的 `M:SS`（或原样放行纯秒数）。"""
    s = str(text).strip()
    m = _PACE_RE.match(s)
    if m:
        return "%d:%02d" % (int(m.group(1)), int(round(float(m.group(2)))))
    if _PACE_PLAIN_RE.match(s):
        return s                      # 纯数字：vendor 自己按秒/公里解释
    raise ValueError("看不懂的%s：%r（应该像 6:00 / 6'00\" / 360）" % (what, text))


def _norm_pace_ramp(text):
    """`7'30",4'30"` → `7:30,4:30`。"""
    if not text:
        return text
    parts = [p for p in str(text).replace("，", ",").replace(" ", ",").split(",") if p.strip()]
    if len(parts) != 2:
        raise ValueError("配速渐变要两个值，例如 7:30,4:30（收到 %r）" % text)
    return ",".join(_norm_pace(p, "配速渐变值") for p in parts)


def generate(shape, boundary_file, out_name=None, size=None, pace="6:00",
             laps=1, laps_mode="retrace", pace_ramp=None, seed=42,
             margin=60.0, avoid=None, jitter=None, pace_noise=None, allow_out=True,
             points=None):
    """生成图形 GPX。

    allow_out=True（默认）：**越界不再拒绝**。以前余量为负会直接报错，
    但校界只是个参考 —— 有人国庆回老家照样跑，那边根本没有「南大边界」。
    现在越界照生成，只回一条 warning 让用户知道。

    points 给了（前端把拖动摆位后的点列发过来）就**跳过自动落点搜索**，
    直接按这点列出 GPX —— 所见即所得，屏幕上是哪就是哪。
    """
    boundary_path = boundary_file if os.path.isabs(boundary_file) \
        else os.path.join(BOUND_DIR, boundary_file)
    ring, frame, poly = load_boundary(boundary_path)

    fn, default_size, cn = gpxart.SHAPES.get(shape, (None, 0.0, shape))

    if points:
        # 手动摆位：点列由前端给，格式 [lat,lon]，与原 track 一致
        pts_ll = [(float(p[1]), float(p[0])) for p in points]       # (lon,lat)
        placed = [frame.fwd(lo, la) for lo, la in pts_ll]
        Q, A, B, D, L2s = _poly_arrays(poly)
        margin_m = float(_dist_batch(np.asarray(placed, dtype=float), A, B, D, L2s).min())
        rot = 0
    else:
        size = float(size or default_size)
        shape_pts = gpxart.normalize(fn(), size)
        margin_m, rot, cx, cy, placed = place_shape(
            poly, shape_pts, shape, need=max(margin, 0.0), avoid=avoid)
        pts_ll = [frame.inv(x, y) for (x, y) in placed]
        gap = math.hypot(placed[-1][0] - placed[0][0], placed[-1][1] - placed[0][1])
        if gap < 25.0:      # 天然闭合才收尾（字母/螺旋强行闭合会多一条横贯直线）
            pts_ll = pts_ll + [pts_ll[0]]

    if not allow_out and margin_m <= 0:
        raise ValueError("图形越界了（余量 %.0f m），把尺寸调小" % margin_m)
    warning = margin_note(margin_m, margin)

    out_name = out_name or "%s-%s.gpx" % (shape, time.strftime("%m%d-%H%M%S"))
    if not out_name.endswith(".gpx"):
        out_name += ".gpx"
    out_path = os.path.join(OUT_DIR, os.path.basename(out_name))

    tmp = os.path.join(OUT_DIR, ".geom-%d.gpx" % os.getpid())
    gpxart.write_geom_gpx(pts_ll, tmp)
    pace = _norm_pace(pace)
    pace_ramp = _norm_pace_ramp(pace_ramp)
    cmd = _script_cmd(RUNROUTE_PATH, "make", "--from-gpx", tmp, "--pace", str(pace),
           "--laps", str(laps), "--laps-mode", laps_mode,
           "--seed", str(seed), "-o", out_path)
    if pace_ramp:
        cmd += ["--pace-ramp", pace_ramp]
    if jitter is not None:
        cmd += ["--jitter", str(jitter)]
    if pace_noise is not None:
        cmd += ["--pace-noise", str(pace_noise)]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    if res.returncode != 0:
        raise RuntimeError((res.stderr or res.stdout or "runroute 失败").strip())

    st = runroute.gpx_stats(out_path)
    chk = check_bounds(out_path, boundary_path, margin=margin)
    out = {
        "file": os.path.basename(out_path),
        "path": out_path,
        "kind": "shape",
        "duration": st.get("duration"),
        "duration_hms": runroute.fmt_hms(st.get("duration") or 0),
        "length_m": st.get("distance"),
        "points": st.get("points"),
        "pace": pace, "laps": laps, "laps_mode": laps_mode,
        "pace_ramp": pace_ramp,
        "rotation": rot,
        "warning": warning,
        "check": chk,
        "raw": (res.stdout or "").strip().splitlines(),
    }
    out.update(_margin_fields(margin_m, margin))
    return out


# ---------------------------------------------------------------- 生成 GPX（操场跑道）

def generate_track(track_file, out_name=None, pace="6:00", laps=4, direction="ccw",
                   pace_ramp=None, seed=42, jitter=None, pace_noise=None):
    """**操场跑步**：沿真实跑道轮廓跑 N 圈。

    跑道是真实存在的闭合环，不存在「落点」「越界」这套 —— 直接把轮廓
    交给 runroute，用 `--laps N --laps-mode forward` 连圈（闭合环首尾重合，
    圈与圈之间不会有额外连线）。
    """
    path = _track_path(track_file)
    if not os.path.exists(path):
        raise FileNotFoundError("找不到操场文件：%s" % track_file)
    pts_ll = _close_ring(read_track(path))
    if direction == "cw":
        pts_ll = list(reversed(pts_ll))
    if len(pts_ll) < 4:
        raise ValueError("跑道轮廓点太少（%d），没法生成" % len(pts_ll))

    props = _read_props(path)
    campus = props.get("campus") or _campus_of(track_file)
    base = props.get("name") or os.path.splitext(os.path.basename(track_file))[0]
    slug = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", base).strip("-")

    out_name = out_name or "track-%s-%s.gpx" % (slug, time.strftime("%m%d-%H%M%S"))
    if not out_name.endswith(".gpx"):
        out_name += ".gpx"
    out_path = os.path.join(OUT_DIR, os.path.basename(out_name))

    tmp = os.path.join(OUT_DIR, ".track-%d.gpx" % os.getpid())
    gpxart.write_geom_gpx(pts_ll, tmp)
    pace = _norm_pace(pace)
    pace_ramp = _norm_pace_ramp(pace_ramp)
    cmd = _script_cmd(RUNROUTE_PATH, "make", "--from-gpx", tmp, "--pace", str(pace),
                      "--laps", str(int(laps)), "--laps-mode", "forward",
                      "--seed", str(seed), "-o", out_path)
    if pace_ramp:
        cmd += ["--pace-ramp", pace_ramp]
    if jitter is not None:
        cmd += ["--jitter", str(jitter)]
    if pace_noise is not None:
        cmd += ["--pace-noise", str(pace_noise)]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    if res.returncode != 0:
        raise RuntimeError((res.stderr or res.stdout or "runroute 失败").strip())

    st = runroute.gpx_stats(out_path)
    frame = gpxart.Frame(pts_ll)
    xy = [frame.fwd(lo, la) for lo, la in pts_ll]
    return {
        "file": os.path.basename(out_path),
        "path": out_path,
        "kind": "track",
        "duration": st.get("duration"),
        "duration_hms": runroute.fmt_hms(st.get("duration") or 0),
        "length_m": st.get("distance"),
        "loop_m": gpxart.path_len(xy),
        "points": st.get("points"),
        "pace": pace, "laps": int(laps), "laps_mode": "forward",
        "direction": direction,
        "pace_ramp": pace_ramp,
        "track_file": track_file, "campus": campus,
        "warning": None, "check": None,
        "raw": (res.stdout or "").strip().splitlines(),
    }


# ---------------------------------------------------------------- 设备与回放

_state = {
    "proc": None,          # runroute play 的 Popen
    "gpx": None,
    "started": None,
    "duration": None,
    "total_points": None,
    "tag": None,
    "paused": False,       # 是否处于暂停（注入进程被 SIGSTOP 冻结）
    "paused_at": None,     # 本次暂停开始的时刻
    "paused_total": 0.0,   # 累计暂停时长（用于把 elapsed 里的暂停时间扣掉）
    "frozen": [],          # 被 SIGSTOP 的注入进程 pid 列表
    "holder": None,        # 常驻定点（注入初始位置）的 Popen
    "holder_ll": None,     # 定点坐标 (lat, lon)
}


def device_info():
    try:
        res = subprocess.run(_pmd_cmd("usbmux", "list"), capture_output=True,
                             text=True, timeout=15)
        data = json.loads(res.stdout or "[]")
    except Exception as e:
        return {"connected": False, "error": str(e)}
    if not data:
        return {"connected": False, "error": "未发现设备（USB 未连或未解锁）"}
    d = data[0]
    return {
        "connected": True,
        "name": d.get("DeviceName"), "udid": d.get("Identifier"),
        "product": d.get("ProductType"), "ios": d.get("ProductVersion"),
        "conn": d.get("ConnectionType"),
    }


def _count_log_points():
    """数 play.log 里已经推送了多少个点。"""
    if not os.path.exists(PLAY_LOG):
        return 0, None
    n = 0
    last = None
    try:
        with open(PLAY_LOG, "r", errors="ignore") as f:
            for line in f:
                if "set location to" in line:
                    n += 1
                    last = line.strip()
    except Exception:
        pass
    return n, last


def play_status():
    proc = _state["proc"]
    running = proc is not None and proc.poll() is None
    pushed, last = _count_log_points()
    total = _state["total_points"] or 1
    paused = bool(_state.get("paused")) and running
    elapsed = None
    if _state["started"]:
        elapsed = time.time() - _state["started"] - _state.get("paused_total", 0.0)
        if paused and _state.get("paused_at"):
            elapsed -= time.time() - _state["paused_at"]
        elapsed = max(0.0, elapsed)
    out = {
        "running": running,
        "paused": paused,
        "gpx": os.path.basename(_state["gpx"]) if _state["gpx"] else None,
        "progress": min(1.0, pushed / total) if total else 0.0,
        "pushed": pushed, "total": total,
        "elapsed": elapsed,
        "elapsed_hms": runroute.fmt_hms(elapsed) if elapsed else None,
        "expected_hms": runroute.fmt_hms(_state["duration"]) if _state["duration"] else None,
        "last": last,
        "holder": _state.get("holder_ll"),
        "external": None,
    }
    if not running:
        out["external"] = external_play()
    return out


def _spawn_set(lat, lon):
    """起一个 simulate-location set 常驻进程（前台阻塞型）。返回 Popen。"""
    return subprocess.Popen(
        _pmd_cmd("developer", "dvt", "simulate-location", "set",
                 "%.7f" % lat, "%.7f" % lon),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        **_detached_kwargs())


def gpx_first_point(gpx_path):
    """返回 (lat, lon)。"""
    pts = gpxart.read_gpx_ll(gpx_path)          # 返回 (lon, lat)
    if not pts:
        raise ValueError("这个 GPX 没有轨迹点")
    lon, lat = pts[0]
    return float(lat), float(lon)


def pin_to_start(gpx_path, hold=True):
    """**注入初始位置**：把设备定位钉到轨迹首点。

    hold=True  → 常驻保持（用户手动点「注入初始位置」时用，位置一直停在那儿，
                 直到「结束模拟」）。
    hold=False → 短暂 set 后释放，仅用于起播前的无缝交接。
    """
    lat, lon = gpx_first_point(gpx_path)
    kill_holder()                                # 先清掉上一个定点，避免叠加
    p = _spawn_set(lat, lon)
    time.sleep(1.2)
    if hold:
        _state["holder"] = p
        _state["holder_ll"] = [lat, lon]
        return {"lat": lat, "lon": lon, "pid": p.pid, "hold": True}
    try:
        p.terminate()
        p.wait(timeout=3)
    except Exception:
        try:
            p.kill()
        except Exception:
            pass
    return {"lat": lat, "lon": lon, "pid": None, "hold": False}


def set_point(lat, lon):
    """**注入初始位置（自定义坐标）**：钉到任意经纬度并保持。"""
    kill_holder()
    p = _spawn_set(float(lat), float(lon))
    time.sleep(1.0)
    _state["holder"] = p
    _state["holder_ll"] = [float(lat), float(lon)]
    return {"lat": float(lat), "lon": float(lon), "pid": p.pid, "hold": True}


def kill_holder():
    p = _state.get("holder")
    if p is not None and p.poll() is None:
        try:
            p.terminate()
            p.wait(timeout=3)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
    _state["holder"] = None
    _state["holder_ll"] = None


def _injector_pids():
    """runroute play 派生出来的真正注入进程（pymobiledevice3 play）。"""
    if _state.get("frozen"):
        return list(_state["frozen"])
    proc = _state["proc"]
    if proc is None:
        return []
    return _child_pids(proc.pid)


def pause_play():
    """**暂停**：对注入进程发 SIGSTOP 冻结。

    为什么可行：`simulate-location play` 是「按相邻点时间差 sleep 后 set」的循环，
    冻结进程 = 连计时一起冻住，恢复后从断点继续，不丢进度、不残留。
    """
    proc = _state["proc"]
    if proc is None or proc.poll() is not None:
        raise RuntimeError("当前没有在跑的回放，无法暂停")
    if _state.get("paused"):
        return {"paused": True, "frozen": len(_state.get("frozen") or []),
                "messages": ["已经是暂停状态"]}
    pids = _injector_pids() or [proc.pid]
    n = 0
    for pid in pids:
        if _suspend(pid):
            n += 1
    _state["paused"] = True
    _state["paused_at"] = time.time()
    _state["frozen"] = pids
    return {"paused": True, "frozen": n,
            "messages": ["已暂停（冻结注入进程 %d 个）" % n]}


def resume_play():
    """**继续**：SIGCONT 解冻。"""
    proc = _state["proc"]
    if proc is None or proc.poll() is not None:
        _state["paused"] = False
        _state["frozen"] = []
        raise RuntimeError("回放已经结束了，没什么可继续的")
    pids = _state.get("frozen") or _injector_pids() or [proc.pid]
    n = 0
    for pid in pids:
        if _resume(pid):
            n += 1
    if _state.get("paused_at"):
        _state["paused_total"] = _state.get("paused_total", 0.0) + \
            (time.time() - _state["paused_at"])
    _state["paused"] = False
    _state["paused_at"] = None
    _state["frozen"] = []
    return {"paused": False, "resumed": n,
            "messages": ["已继续（解冻 %d 个进程）" % n]}


def start_play(gpx_path, prime=True, prime_wait=1.2):
    """**开始模拟**。

    prime=True 时**先把定位钉到轨迹首点**，再交接给 play。
    少了这步，设备当前位置与轨迹起点之间的瞬移会被运动 App 记成一条杂线
    （命令行流程一直手工做这步，WebUI 早期版本漏了）。
    """
    if _state["proc"] is not None and _state["proc"].poll() is None:
        raise RuntimeError("已有回放在跑，先停止")
    if not os.path.exists(gpx_path):
        raise FileNotFoundError(gpx_path)

    st = runroute.gpx_stats(gpx_path)
    if st.get("timed") is False:
        raise RuntimeError("这个 GPX 没有时间戳，回放会瞬间推完")

    # 重置播放期状态
    _state.update(paused=False, paused_at=None, paused_total=0.0, frozen=[])

    primed = False
    if prime:
        try:
            pin_to_start(gpx_path, hold=False)
            primed = True
        except Exception:
            primed = False

    open(PLAY_LOG, "w").close()
    logf = open(PLAY_LOG, "ab")
    proc = subprocess.Popen(_script_cmd(RUNROUTE_PATH, "play", gpx_path),
                            stdout=logf, stderr=subprocess.STDOUT,
                            cwd=HERE, **_detached_kwargs())
    _state.update(proc=proc, gpx=gpx_path, started=time.time(),
                  duration=st.get("duration"), total_points=st.get("points"))
    return {"pid": proc.pid, "primed": primed,
            "duration_hms": runroute.fmt_hms(st.get("duration") or 0)}


def release_all(clear=True):
    """**结束模拟（恢复定位）**：停回放 + 杀常驻定点 + 发 clear。

    覆盖三种情形：正在跑的回放、只注入过初始位置、两者都没有。
    """
    msgs = []
    proc = _state["proc"]
    if proc is not None and proc.poll() is None:
        _resume_if_frozen(proc)
        try:
            r = stop_play(clear=False)
            msgs.extend(r.get("messages", []))
        except Exception as e:
            msgs.append("停止回放出错：%s" % e)
    else:
        # 进程已自己结束，但状态没清（比如自然跑完）
        _state.update(proc=None, started=None, paused=False, frozen=[])

    if _state.get("holder") is not None:
        kill_holder()
        msgs.append("已释放常驻定点")

    if clear:
        clear_location()
        msgs.append("已发送 stopLocationSimulation（无回执）")
    msgs.append("若手机地图仍停在模拟位置 → 拔一次数据线（唯一 100% 可靠）")
    return {"messages": msgs}


def _resume_if_frozen(proc):
    """杀进程前先解冻 —— 被冻结的进程收不到终止信号。"""
    for pid in (_state.get("frozen") or []):
        _resume(pid)
    _state["paused"] = False
    _state["frozen"] = []


def external_play():
    """检测命令行另起的 play 会话（不归本服务管，只做提示）。"""
    mine = _state["gpx"]
    for pid, p in _scan_simulate_play():
        if not os.path.exists(p):
            continue
        if mine and os.path.abspath(p) == os.path.abspath(mine):
            continue                                    # 本服务起的，不算外部
        try:
            dur = runroute.gpx_stats(p).get("duration")
        except Exception:
            dur = None
        return {"pid": pid, "file": os.path.basename(p),
                "duration_hms": runroute.fmt_hms(dur) if dur else None}
    return None


def stop_play(clear=True):
    """停回放。

    关键：杀 runroute（父）不会触发它的 finally clear，必须杀它派生的
    pymobiledevice3 子进程 —— 父进程收不到子进程后才会往下走并执行 clear。

    **只处理本进程启动的回放**：命令行里另起的 play 不归这里管，
    否则点一下「停止」就会误伤别人的会话。
    """
    proc = _state["proc"]
    if proc is None:
        return {"messages": ["本服务当前没有在跑的回放（命令行另起的会话不归这里管）"]}

    msgs = []
    if proc.poll() is None:
        _resume_if_frozen(proc)          # 冻结中的进程要先解冻才杀得动
        kids = _child_pids(proc.pid)
        for k in kids:
            _terminate(k)
        if not kids:
            try:
                proc.terminate()
            except Exception:
                pass
        try:
            proc.wait(timeout=8)
            msgs.append("回放进程已退出（父进程会自行 clear）")
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
            msgs.append("回放进程强制终止（可能残留，需拔线）")
    _state.update(proc=None, started=None, paused=False, paused_at=None, frozen=[])
    if clear:
        clear_location()
        msgs.append("已发送 stopLocationSimulation")
    return {"messages": msgs}


def clear_location():
    try:
        subprocess.run(_pmd_cmd("developer", "dvt", "simulate-location", "clear"),
                       capture_output=True, timeout=15)
    except Exception:
        pass


def set_location(lat, lon):
    """兼容旧接口：等价 set_point（钉到指定坐标并保持）。返回 pid。"""
    return set_point(lat, lon)["pid"]


# ---------------------------------------------------------------- 自检

def selftest(shape="heart", boundary_file="boundary-nju-xianlin.geojson", size=560.0):
    """拿同一输入对比 numpy 版与 gpxart 原版落点，确保对齐。"""
    boundary_path = os.path.join(BOUND_DIR, boundary_file)
    ring, frame, poly = load_boundary(boundary_path)
    fn, _, cn = gpxart.SHAPES[shape]
    shape_pts = gpxart.normalize(fn(), float(size))

    t0 = time.time()
    m_fast, rot_f, cx_f, cy_f, pts_f = place_shape(poly, shape_pts, shape, need=60.0)
    t_fast = time.time() - t0

    t0 = time.time()
    m_ref, rot_r, cx_r, cy_r, pts_r = gpxart.best_placement(poly, shape_pts, shape, need=60.0)
    t_ref = time.time() - t0

    d_center = math.hypot(cx_f - cx_r, cy_f - cy_r)
    return {
        "shape": shape,
        "numpy": {"margin": round(m_fast, 3), "rot": rot_f,
                  "center": [round(cx_f, 3), round(cy_f, 3)], "seconds": round(t_fast, 3)},
        "ref": {"margin": round(m_ref, 3), "rot": rot_r,
                "center": [round(cx_r, 3), round(cy_r, 3)], "seconds": round(t_ref, 3)},
        "center_delta_m": round(d_center, 3),
        "margin_delta_m": round(abs(m_fast - m_ref), 3),
        "ok": d_center < 1.0 and abs(m_fast - m_ref) < 1.0,
        "speedup": round(t_ref / t_fast, 1) if t_fast > 0 else None,
    }


if __name__ == "__main__":
    print(json.dumps(selftest(*(sys.argv[1:] or ["heart"])), ensure_ascii=False, indent=2))
