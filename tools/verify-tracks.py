#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""复核 data/tracks/*.geojson 是不是真的踩在 OSM 的真实操场上。

怎么核：
  1. 用 Overpass 拉每块跑道对应的 OSM 真实场地外框；
  2. 在局部平面（等距方位投影近似）里算两边的
       · 形心  → 看位置对不对
       · 主轴  → 看朝向对不对（PCA 会被顶点疏密带偏，所以另算「最长直边」）
  3. 打印偏差表；偏差在几米 / 几度以内就算贴合。

角度约定：本脚本一律用「相对东向、逆时针为正」（= atan2(Δ北, Δ东)），
和 tools/make-tracks.py 里的 `axis` 同一套。罗盘方位角 = 90 - axis。

用法：
    python3 tools/verify-tracks.py                # 直连，失败自动走本机代理
    python3 tools/verify-tracks.py --offline DIR  # 用本地已存的 Overpass 响应
"""
import argparse
import json
import math
import os
import ssl
import sys
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TRACK_DIR = os.path.join(ROOT, "data", "tracks")

M_PER_DEG = 111320.0

# 跑道文件 → (场地外框 way id, 内场 way id 或 None)
WATCH = {
    "track-nju-gulou-suzhe.geojson": (187816516, None),
    "track-nju-xianlin-weihua.geojson": (99661837, 118862373),
    "track-nju-xianlin-di2.geojson": (609155597, 609155596),
    "track-nju-pukou.geojson": (89782131, None),
}

EPS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
PROXIES = [None, "http://127.0.0.1:7890", "http://127.0.0.1:1087"]


def overpass(way_ids, proxies=PROXIES):
    q = ("[out:json][timeout:120];(" +
         "".join("way(%d);" % w for w in way_ids) + ");out geom;")
    last = None
    for ep in EPS:
        for px in proxies:
            handler = urllib.request.ProxyHandler({"http": px, "https": px} if px else {})
            ctx = ssl.create_default_context()
            ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
            op = urllib.request.build_opener(handler, urllib.request.HTTPSHandler(context=ctx))
            try:
                req = urllib.request.Request(
                    ep + "?" + urllib.parse.urlencode({"data": q}),
                    headers={"User-Agent": "nju-runner/verify-tracks (+track provenance check)"})
                raw = op.open(req, timeout=150).read().decode()
                print("  · Overpass %s（%s）" % (ep.split("/")[2], px or "直连"))
                return json.loads(raw)
            except Exception as e:            # noqa: BLE001
                last = e
    raise SystemExit("Overpass 全部镜像都失败：%s" % last)


def flatten(coords):
    """经纬度点列 → 以首点为原点的米制局部平面。"""
    lat0 = sum(p[1] for p in coords) / len(coords)
    k = M_PER_DEG * math.cos(math.radians(lat0))
    lon0, la0 = coords[0]
    return [((p[0] - lon0) * k, (p[1] - la0) * M_PER_DEG) for p in coords]


def perimeter(P):
    return sum(math.hypot(P[(i + 1) % len(P)][0] - P[i][0],
                          P[(i + 1) % len(P)][1] - P[i][1]) for i in range(len(P)))


def pca_angle(P):
    """主轴角度（相对东向、逆时针为正）；**先去掉形心**再求二阶矩。

    采样均匀时才准（生成跑道是等距 2.5 m 采样，正好满足）；
    OSM 外框顶点疏密不均，PCA 会被带偏，所以那边改用最长直边。
    """
    cx = sum(p[0] for p in P) / len(P)
    cy = sum(p[1] for p in P) / len(P)
    sxx = sum((p[0] - cx) ** 2 for p in P)
    syy = sum((p[1] - cy) ** 2 for p in P)
    sxy = sum((p[0] - cx) * (p[1] - cy) for p in P)
    return math.degrees(0.5 * math.atan2(2.0 * sxy, sxx - syy))


def longest_edge(P):
    """返回（边长, 角度）。角度 = 相对东向、逆时针为正。"""
    best = (0.0, 0.0)
    for i in range(len(P)):
        x0, y0 = P[i]
        x1, y1 = P[(i + 1) % len(P)]
        L = math.hypot(x1 - x0, y1 - y0)
        if L > best[0]:
            best = (L, math.degrees(math.atan2(y1 - y0, x1 - x0)))
    return best


def centroid_local(P, lat0):
    cx = sum(p[0] for p in P) / len(P)
    cy = sum(p[1] for p in P) / len(P)
    return cx, cy


def fold90(a):
    return ((a + 90.0) % 180.0) - 90.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", metavar="JSON", help="用本地已存的 Overpass 响应，不联网")
    args = ap.parse_args()

    want = sorted({w for pair in WATCH.values() for w in pair if w})
    if args.offline:
        data = json.load(open(args.offline, encoding="utf-8"))
        print("  · 读本地 Overpass 响应：%s" % args.offline)
    else:
        data = overpass(want)

    osm = {}
    for el in data["elements"]:
        g = el.get("geometry") or []
        if len(g) >= 3:
            osm[el["id"]] = [(p["lon"], p["lat"]) for p in g]

    print()
    print("%-34s %-14s %8s %8s %8s %8s %7s %8s" %
          ("跑道文件", "OSM 场地", "场地周长", "跑道周长", "场地轴向", "跑道轴向", "轴向差", "形心间距"))
    print("-" * 112)
    bad = 0
    for fn, (wid, inner) in sorted(WATCH.items()):
        path = os.path.join(TRACK_DIR, fn)
        if not os.path.exists(path):
            print("%-34s 缺失" % fn); bad += 1; continue
        feat = json.load(open(path, encoding="utf-8"))["features"][0]
        tc = feat["geometry"]["coordinates"]
        if feat["geometry"]["type"] == "Polygon":
            tc = tc[0]
        if wid not in osm:
            print("%-34s OSM way/%d 无几何" % (fn, wid)); bad += 1; continue

        RP = flatten(tc)
        OP = flatten(osm[wid])
        # 两边各自相对自己的首点，摆到同一原点（用形心差来比位置）
        orc = centroid_local(OP, 0)
        rrc = centroid_local(RP, 0)
        # 形心差：需要把跑道形心换算回经纬度再比距离
        lat0 = sum(p[1] for p in tc) / len(tc)
        k = M_PER_DEG * math.cos(math.radians(lat0))
        t_cx = tc[0][0] + rrc[0] / k
        t_cy = tc[0][1] + rrc[1] / M_PER_DEG
        o_cx = osm[wid][0][0] + orc[0] / k
        o_cy = osm[wid][0][1] + orc[1] / M_PER_DEG
        if inner and inner in osm:
            gi = flatten(osm[inner])
            icx, icy = centroid_local(gi, 0)
            o_cx = osm[inner][0][0] + icx / k
            o_cy = osm[inner][0][1] + icy / M_PER_DEG
            # 外框形心与内场形心的均值 → 更接近跑道环中心
            o_cx = (o_cx + osm[wid][0][0] + orc[0] / k) / 2
            o_cy = (o_cy + osm[wid][0][1] + orc[1] / M_PER_DEG) / 2
        d_center = math.hypot((t_cx - o_cx) * k, (t_cy - o_cy) * M_PER_DEG)

        _, o_ang = longest_edge(OP)
        t_ang = pca_angle(RP)
        d_ang = fold90(t_ang - o_ang)

        name = json.load(open(path, encoding="utf-8"))["features"][0]["properties"].get("campus_name", "")
        ok = abs(d_ang) <= 5.0 and d_center <= 15.0 and abs(perimeter(RP) - 400.0) <= 1.0
        if not ok:
            bad += 1
        print("%-34s %-14s %7.1f m %7.1f m %7.1f° %7.1f° %6.2f° %7.1f m %s" %
              (fn, str(name)[-8:], perimeter(OP), perimeter(RP),
               fold90(o_ang), fold90(t_ang),
               d_ang, d_center, "OK" if ok else "**超出容差**"))

    print()
    if bad:
        print("有 %d 条超出容差（|轴向差|>5° 或 形心间距>15 m 或 周长≠400±1 m）" % bad)
        return 1
    print("全部贴合：周长都是 400 m，轴向差与形心间距都在容差内。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
