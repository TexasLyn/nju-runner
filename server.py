#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
NJU Runner · server
===================
本地 WebUI 后端。只用标准库（http.server + json），零额外依赖 ——
这样换台机器只要装了 numpy（真机注入再加 pymobiledevice3）就能跑。

    python server.py            # 默认 http://127.0.0.1:8788
    python server.py 9000       # 指定端口
"""
import atexit
import json
import mimetypes
import os
import socket
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import core  # noqa: E402

STATIC = os.path.join(HERE, "static")
DEFAULT_PORT = 8788

# OSM 瓦片代理。规矩（tile.openstreetmap.org 使用条款）：
#   必须带能识别用途的 User-Agent、不得批量预取、不得高频刷。
# 这里只在用户切到「真实地图」时按当前视野取十几张，并落盘缓存 —— 之后不再回源。
TILE_URL = "https://tile.openstreetmap.org/%d/%d/%d.png"
TILE_UA = "NJU-Runner/1.0 (personal local tool; https://github.com/TexasLyn/nju-runner)"
TILE_OK_MIME = "image/png"


def _tile_proxies():
    """取瓦片时可用的代理。

    tile.openstreetmap.org 在国内**时通时断**（TLS 会被重置），所以直连失败后
    要能走本机代理。顺序：显式的 NJU_TILE_PROXY → 环境里的 HTTPS_PROXY →
    几个常见本机代理端口（Clash 7890 / V2Ray 1087 / Surge 6152）。
    """
    out = []
    for v in (os.environ.get("NJU_TILE_PROXY"), os.environ.get("HTTPS_PROXY"),
              os.environ.get("https_proxy"), os.environ.get("HTTP_PROXY"),
              os.environ.get("http_proxy"),
              "http://127.0.0.1:7890", "http://127.0.0.1:1087", "http://127.0.0.1:6152"):
        if v and v not in out and not v.startswith("127.0.0.1:6"):   # 排除沙盒自己的代理
            out.append(v)
    return out


_PROXIES = None


def _fetch(url, proxy=None, timeout=20):
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"https": proxy, "http": proxy}))
    else:
        handlers.append(urllib.request.ProxyHandler({}))          # 显式直连
    op = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, headers={"User-Agent": TILE_UA,
                                               "Accept": TILE_OK_MIME})
    with op.open(req, timeout=timeout) as resp:
        return resp.read()


_tile_locks = {}
_tile_guard = threading.Lock()


def _tile_lock(key):
    with _tile_guard:
        lk = _tile_locks.get(key)
        if lk is None:
            lk = _tile_locks[key] = threading.Lock()
        return lk


def fetch_tile(z, x, y):
    """取一张 OSM 瓦片，带磁盘缓存。返回 (bytes, from_cache)；彻底取不到返回 (None, False)。"""
    global _PROXIES
    n = 2 ** z
    if z < 0 or z > 19 or x < 0 or y < 0 or x >= n or y >= n:
        return None, False
    d = os.path.join(core.TILE_CACHE, str(z), str(x))
    f = os.path.join(d, "%d.png" % y)
    if os.path.isfile(f) and os.path.getsize(f) > 0:
        with open(f, "rb") as fh:
            return fh.read(), True
    with _tile_lock(f):
        if os.path.isfile(f) and os.path.getsize(f) > 0:      # 另一个线程刚抓好
            with open(f, "rb") as fh:
                return fh.read(), True
        if _PROXIES is None:
            _PROXIES = _tile_proxies()
        url = TILE_URL % (z, x, y)
        data = None
        for proxy in [None] + _PROXIES:                       # 先直连，再逐个代理兜底
            for _try in range(2):
                try:
                    data = _fetch(url, proxy)
                except Exception:
                    data = None
                    continue
                if data and data.startswith(b"\x89PNG"):
                    break
                data = None
            if data:
                break
        if not data:
            return None, False
        try:
            os.makedirs(d, exist_ok=True)
            tmp = f + ".part"
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.replace(tmp, f)
        except Exception:
            pass
        return data, False


def _safe_join(base, name):
    """把用户传来的文件名限制在 base 目录内，防目录穿越。"""
    name = os.path.basename(urllib.parse.unquote(name or ""))
    path = os.path.join(base, name)
    if os.path.commonpath([os.path.abspath(path), os.path.abspath(base)]) != os.path.abspath(base):
        raise ValueError("非法路径")
    return path


# ---------------------------------------------------------------- API 实现

def api_shapes(_):
    return {"shapes": core.list_shapes()}


def api_boundaries(_):
    return {"boundaries": core.list_boundaries()}


def api_tracks(_):
    return {"tracks": core.list_tracks()}


def api_preview(p):
    # 操场模式：真实跑道不做落点搜索，直接回轮廓
    if p.get("track"):
        return core.preview_track(p["track"], direction=p.get("direction", "ccw"))
    return core.preview(
        shape=p.get("shape", "heart"),
        boundary_file=p.get("boundary", "boundary-nju-xianlin.geojson"),
        size=p.get("size"),
        margin=float(p.get("margin", 60.0)),
        quality=p.get("quality", "fast"),
    )


def api_generate(p):
    if p.get("track"):
        return core.generate_track(
            track_file=p["track"],
            out_name=p.get("out_name"),
            pace=p.get("pace", "6:00"),
            laps=int(p.get("laps", 4)),
            direction=p.get("direction", "ccw"),
            pace_ramp=p.get("pace_ramp") or None,
            seed=int(p.get("seed", 42)),
        )
    return core.generate(
        shape=p.get("shape", "heart"),
        boundary_file=p.get("boundary", "boundary-nju-xianlin.geojson"),
        out_name=p.get("out_name"),
        size=p.get("size"),
        pace=p.get("pace", "6:00"),
        laps=int(p.get("laps", 1)),
        laps_mode=p.get("laps_mode", "retrace"),
        pace_ramp=p.get("pace_ramp") or None,
        seed=int(p.get("seed", 42)),
        margin=float(p.get("margin", 60.0)),
    )


def api_check(p):
    f = _safe_join(core.OUT_DIR, p.get("file", ""))
    return core.check_bounds(f, p.get("boundary", "boundary-nju-xianlin.geojson"),
                             margin=float(p.get("margin", 50.0)),
                             step=float(p.get("step", 5.0)))


def api_routes(_):
    out = []
    for fn in sorted(os.listdir(core.OUT_DIR)):
        if not fn.lower().endswith(".gpx"):
            continue
        path = os.path.join(core.OUT_DIR, fn)
        try:
            st = core.runroute.gpx_stats(path)
            out.append({
                "file": fn, "size": os.path.getsize(path),
                "mtime": os.path.getmtime(path),
                "length_m": st.get("distance"), "points": st.get("points"),
                "duration_hms": core.runroute.fmt_hms(st.get("duration") or 0),
            })
        except Exception:
            out.append({"file": fn, "size": os.path.getsize(path), "error": "无法解析"})
    out.sort(key=lambda z: -z["mtime"])
    return {"routes": out}


def api_device(_):
    return core.device_info()


def api_play(p):
    f = _safe_join(core.OUT_DIR, p.get("file", ""))
    return core.start_play(f)


def api_pause(_):
    return core.pause_play()


def api_resume(_):
    return core.resume_play()


def api_prime(p):
    """注入初始位置：钉到轨迹首点（传 file）或任意坐标（传 lat/lon）。"""
    if p.get("lat") is not None and p.get("lon") is not None:
        return core.set_point(float(p["lat"]), float(p["lon"]))
    if not p.get("file"):
        raise ValueError("要么给 file（钉到该路线起点），要么给 lat/lon")
    f = _safe_join(core.OUT_DIR, p["file"])
    return core.pin_to_start(f, hold=True)


def api_release(_):
    return core.release_all(clear=True)


def api_stop(_):
    return core.stop_play(clear=True)


def api_status(_):
    return core.play_status()


def api_clear(_):
    core.clear_location()
    return {"ok": True, "hint": "clear 无回执，退出码不代表结果；若手机仍是旧位置就拔线"}


def api_setloc(p):
    pid = core.set_location(p["lat"], p["lon"])
    return {"ok": True, "pid": pid,
            "hint": "这是常驻定点，位置会一直保持；停止请点「清理定位」或拔线"}


GET_ROUTES = {
    "/api/shapes": api_shapes,
    "/api/boundaries": api_boundaries,
    "/api/tracks": api_tracks,
    "/api/routes": api_routes,
    "/api/device": api_device,
    "/api/status": api_status,
    "/api/clear": api_clear,
}
POST_ROUTES = {
    "/api/preview": api_preview,
    "/api/generate": api_generate,
    "/api/check": api_check,
    "/api/play": api_play,
    "/api/pause": api_pause,
    "/api/resume": api_resume,
    "/api/prime": api_prime,
    "/api/release": api_release,
    "/api/stop": api_stop,
    "/api/setloc": api_setloc,
}


class Handler(BaseHTTPRequestHandler):
    server_version = "NJU Runner"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):        # 静音，只在出错时自己打印
        pass

    # -------------------------------------------------- 工具

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        hdrs = {"Content-Type": ctype,
                "Content-Length": str(len(body)),
                "Cache-Control": "no-store"}
        hdrs.update(extra or {})           # extra 可覆盖默认（比如瓦片要长缓存）
        self.send_response(code)
        for k, v in hdrs.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str))

    def _err(self, code, msg):
        self._json({"error": msg}, code)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    # -------------------------------------------------- 路由

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        path = u.path

        if path in GET_ROUTES:
            try:
                return self._json(GET_ROUTES[path](urllib.parse.parse_qs(u.query)))
            except Exception as e:
                traceback.print_exc()
                return self._err(500, str(e))

        if path == "/api/tile":
            q = urllib.parse.parse_qs(u.query)
            try:
                z, x, y = (int(q.get(k, ["-1"])[0]) for k in ("z", "x", "y"))
            except Exception:
                return self._err(400, "z/x/y 必须是整数")
            data, cached = fetch_tile(z, x, y)
            if data is None:
                # 取不到就回 404：前端 <image> 的 onerror 会记账，
                # 一张都取不到时自动切回极简底图（比无声留白友好）
                return self._err(404, "瓦片暂时取不到")
            return self._send(200, data, TILE_OK_MIME, {
                "Cache-Control": "public, max-age=604800",
                "X-Tile": "hit" if cached else "fetch",
            })

        if path == "/api/download":
            q = urllib.parse.parse_qs(u.query)
            try:
                f = _safe_join(core.OUT_DIR, q.get("file", [""])[0])
            except ValueError as e:
                return self._err(400, str(e))
            if not os.path.exists(f):
                return self._err(404, "文件不存在")
            with open(f, "rb") as fp:
                data = fp.read()
            return self._send(200, data, "application/gpx+xml",
                              {"Content-Disposition":
                               'attachment; filename="%s"' % os.path.basename(f)})

        return self._static(path)

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        fn = POST_ROUTES.get(path)
        if not fn:
            return self._err(404, "未知接口")
        try:
            return self._json(fn(self._body()))
        except (ValueError, FileNotFoundError, KeyError) as e:
            return self._err(400, str(e))
        except Exception as e:
            traceback.print_exc()
            return self._err(500, str(e))

    # -------------------------------------------------- 静态文件

    def _static(self, path):
        if path in ("/", ""):
            path = "/index.html"
        rel = path.lstrip("/")
        if rel.startswith("static/"):
            rel = rel[len("static/"):]
        f = os.path.join(STATIC, os.path.normpath(rel))
        if not f.startswith(STATIC) or not os.path.isfile(f):
            return self._err(404, "not found")
        ctype = mimetypes.guess_type(f)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        with open(f, "rb") as fp:
            data = fp.read()
        return self._send(200, data, ctype)


def pick_port(preferred):
    """挑一个可用端口。

    探测 socket 必须开 SO_REUSEADDR：否则上一个进程刚退出时，
    端口上的 TIME_WAIT 连接会让 bind 报 EADDRINUSE，
    于是服务被无意义地顺延到 8789/8790 —— 实际那个端口是能用的。
    """
    for p in [preferred] + [preferred + i for i in range(1, 12)]:
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", p))
            s.close()
            return p
        except OSError:
            s.close()
    raise SystemExit("端口都被占用了，换个端口跑：python server.py 9000")


def main():
    port = pick_port(int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT)
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True

    dev = core.device_info()
    print("┌" + "─" * 56)
    print("│  NJU Runner · 南哪跑男")
    print("│  →  http://127.0.0.1:%d" % port)
    print("│  设备：%s" % ("%s (iOS %s, %s)" % (dev.get("name"), dev.get("ios"), dev.get("conn"))
                          if dev.get("connected") else "未连接 — " + str(dev.get("error"))))
    print("│  产物：%s" % core.OUT_DIR)
    print("└" + "─" * 56 + "\n  Ctrl+C 结束服务\n")

    def _bye():
        try:
            core.release_all(clear=True)      # 停回放 + 释放定点 + clear
        except Exception:
            pass
    atexit.register(_bye)

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出。注意：设备侧若还停在模拟位置，拔一次数据线才算真正恢复。")


if __name__ == "__main__":
    main()
