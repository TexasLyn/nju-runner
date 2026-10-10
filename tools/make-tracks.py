#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""生成「操场跑道」数据（data/tracks/*.geojson）。

为什么不是直接抄 OSM 的场地外框？
--------------------------------
OSM 上南大各校区的田径场，绝大多数只画了**运动场的外框**（含看台、外围步道），
周长 570~600 m —— 直接沿它跑，App 会记成 500 多米一圈，距离和配速全错。
鼓楼苏浙运动场有单独的 `leisure=track` 环，周长 431 m，也不是标准道次。

所以这里的做法是：
  **位置与朝向取自 OSM 的真实场地；跑道几何按 IAAF 标准 400 m 生成。**
即圆心 = 真实场地形心，直道方向 = 真实场地的长轴方向，
环线 = 直道 84.39 m × 2 + 半径 36.80 m 的两个半圆 = 400.00 m（第 1 道测量线）。
这样手机上记到的每一圈正好 400 m，配速才是真配速。

角度约定（**别按罗盘方位角读**）
------------------------------
SPECS 里的 `axis` 是**局部平面的旋转角**，单位「度」，
**0° = 长轴东西向；逆时针为正，折到 [-90, 90]**。
即 `axis = atan2(Δ北, Δ东)`，和 `math.atan2` 的输出同号。
（罗盘方位角是「相对北、顺时针为正」，两者差一个 90° 的旋转：
  罗盘方位角 = 90 - axis。下面 SPECS 里的值都是按 `axis` 记的。）

复核：`python3 tools/verify-tracks.py` —— 联网拉 OSM 的真实场地外框，
把「生成跑道的主轴 / 形心」和「真实场地的最长直边 / 形心」逐条比一遍。

来源与推导都写在每个文件的 properties 里，可复核。
"""
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(os.path.dirname(HERE), "data", "tracks")

M_PER_DEG = 111320.0
STRAIGHT = 84.39        # IAAF 400 m 跑道直道长度
RADIUS = 36.80          # 第 1 道测量线半径（内沿 36.50 + 0.30）
STEP = 2.5              # 采样间距（米）

# 每一条：圆心与长轴都是从 OSM 真实场地量出来的（复核用 tools/verify-tracks.py）
SPECS = [
    dict(
        file="track-nju-gulou-suzhe.geojson",
        name="苏浙运动场",
        campus="gulou",
        campus_name="南京大学(鼓楼校区)",
        center=(32.058940, 118.773689),
        axis=-83.53,
        source="OpenStreetMap way/187816516（leisure=track, sport=running）",
        derived="圆心取该跑道环本身的形心；长轴方向取环上最长直边（111.6 m）。"
                "该环外框 91.8×167.0 m、周长 430.8 m，即 6 道附近；本文件按第 1 道 400 m 生成",
    ),
    dict(
        file="track-nju-xianlin-weihua.geojson",
        name="炜华体育场",
        campus="xianlin",
        campus_name="南京大学(仙林校区)",
        center=(32.114065, 118.952553),
        axis=-81.58,
        source="OpenStreetMap way/99661837（sports_centre 炜华体育场）+ way/118862373（内场足球场）",
        derived="圆心取「场地外框」与「内场足球场」两形心的均值；长轴方向取外框最长直边（155.8 m）",
    ),
    dict(
        file="track-nju-xianlin-di2.geojson",
        name="第二运动场",
        campus="xianlin",
        campus_name="南京大学(仙林校区)",
        center=(32.122727, 118.947797),
        axis=-85.16,
        source="OpenStreetMap way/609155597（第二运动场）+ way/609155596（内场足球场）",
        derived="同上：两形心均值定圆心，外框最长直边（181.6 m）定朝向",
    ),
    dict(
        file="track-nju-pukou.geojson",
        name="浦口校区运动场",
        campus="pukou",
        campus_name="南京大学浦口校区",
        center=(32.182193, 118.700716),
        axis=89.58,
        source="OpenStreetMap way/89782131（leisure=pitch）",
        derived="该校区 OSM 上唯一的大场地（外框 120.3×217.2 m）；圆心取形心，"
                "朝向取最长直边（138.5 m）。OSM 未单独描出跑道，按标准 400 m 生成",
    ),
]


def track_loop(center, axis, n=None):
    """在局部平面里造一圈标准 400 m 跑道，再旋转/平移到指定经纬度。

    局部坐标：X = 直道方向，Y = 垂直方向；在局部平面上逆时针走一圈（标准跑向）。
    `axis` 的单位是度：0° = 长轴东西向，逆时针为正（见模块头部的角度约定）。
    """
    s, r = STRAIGHT, RADIUS
    pts = []

    def arc(cx, cy, a0, a1):
        steps = max(6, int(abs(a1 - a0) / (2 * math.pi) * 72))
        for i in range(steps + 1):
            a = a0 + (a1 - a0) * i / steps
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))

    pts.append((-s / 2, -r))
    pts.append((+s / 2, -r))
    arc(+s / 2, 0.0, -math.pi / 2, +math.pi / 2)     # 右弯
    pts.append((-s / 2, +r))
    arc(-s / 2, 0.0, +math.pi / 2, +math.pi * 1.5)   # 左弯
    pts.append((pts[0][0], pts[0][1]))

    # 等距重采样
    cum = [0.0]
    for i in range(1, len(pts)):
        cum.append(cum[-1] + math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]))
    total = cum[-1]
    n = n or int(round(total / STEP))
    out = []
    j = 0
    for i in range(n):
        d = total * i / n
        while j < len(cum) - 1 and cum[j + 1] < d:
            j += 1
        seg = cum[j + 1] - cum[j]
        t = 0.0 if seg <= 0 else (d - cum[j]) / seg
        out.append((pts[j][0] + (pts[j + 1][0] - pts[j][0]) * t,
                    pts[j][1] + (pts[j + 1][1] - pts[j][1]) * t))
    out.append(out[0])

    # 旋转 + 平移到经纬度
    th = math.radians(axis)
    ca, sa = math.cos(th), math.sin(th)
    lat0, lon0 = center
    k = M_PER_DEG * math.cos(math.radians(lat0))
    coords = []
    for x, y in out:
        xe = x * ca - y * sa
        yn = x * sa + y * ca
        coords.append([round(lon0 + xe / k, 7), round(lat0 + yn / M_PER_DEG, 7)])
    return coords, total


def main():
    os.makedirs(OUT, exist_ok=True)
    for sp in SPECS:
        coords, total = track_loop(sp["center"], sp["axis"])
        obj = {
            "type": "FeatureCollection",
            "features": [{
                "type": "Feature",
                "properties": {
                    "name": sp["name"],
                    "campus": sp["campus"],
                    "campus_name": sp["campus_name"],
                    "lane": "第 1 道",
                    "loop_m": round(total, 1),
                    "axis_deg": sp["axis"],
                    "axis_note": "长轴角度：0°=东西向，逆时针为正（折到 ±90），"
                                 "即 atan2(Δ北, Δ东)；罗盘方位角 = 90 - axis_deg",
                    "source": sp["source"],
                    "derived": sp["derived"],
                    "note": "圆心与朝向取自 OSM 真实场地；跑道几何按 IAAF 标准 400 m 生成"
                            "（直道 %.2f m、半径 %.2f m），每圈 400.0 m" % (STRAIGHT, RADIUS),
                },
                "geometry": {"type": "LineString", "coordinates": coords},
            }],
        }
        path = os.path.join(OUT, sp["file"])
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=1)
            fh.write("\n")
        print("%-40s %3d 点  周长 %.1f m  %s" %
              (sp["file"], len(coords), total, sp["name"]))
    print("\n输出目录：", OUT)


if __name__ == "__main__":
    sys.exit(main())
