# NJU Runner · 南哪跑男

> 在校园边界内**画一个图形**（爱心 / 520 / 五角星 / 校名缩写…），自动摆到最安全的位置，
> 按你要的配速生成 GPX，再一键注入到 iPhone —— 手机地图上就出现一条「跑出这个形状」的轨迹。

**不用敲命令，也不用叫 AI。** 打开网页点几下就行。

<p align="center">
  <img src="docs/screenshot-light.png" width="49%" alt="浅色界面">
  <img src="docs/screenshot-dark.png" width="49%" alt="深色界面">
</p>

---

## 快速开始

```shell
git clone <this-repo> && cd nju-runner
python3 -m pip install -r requirements.txt
```

**macOS / Linux**

```shell
./start.sh              # 默认 http://127.0.0.1:8788
./start.sh 9000         # 换端口
```

或者直接**双击 `启动.command`** —— 会起服务并自动打开浏览器。用完 `Ctrl+C` 关掉。

`start.sh` 会自己找一个装着 numpy 的 Python；要用指定的解释器就
`NJURUNNER_PY=/path/to/python ./start.sh`。

**Windows**

```bat
start.bat               :: 默认 8788，自动开浏览器
start.bat 9000          :: 换端口
```

`start.bat` 会依次试 `py -3` / `python` / `python3`，挑第一个装了 numpy 的。
装依赖：

```bat
pip install numpy
pip install pymobiledevice3   :: 只有要推真机才需要
```

装完先自检一遍（不碰手机、不碰定位）：

```powershell
powershell -ExecutionPolicy Bypass -File tools\windows-selftest.ps1
```

### 平台支持

| 能力 | macOS / Linux | Windows |
|---|---|---|
| 画图 / 预览 / 生成 GPX / 下载 | ✅ | ✅ |
| 越界校验、配速斜坡、多圈往复 | ✅ | ✅ |
| 真机注入（注入初始位置 / 开始模拟 / 暂停继续 / 结束模拟） | ✅ | ⚠️ 代码已适配，实机验证待补 |
| Windows 真机注入的额外前提 | — | Apple 移动设备驱动（iTunes / Apple 设备） |

Windows 上的差异点都已收口到 `core.py` 顶部那一小段平台层：

- **暂停 / 继续**：POSIX 用 `SIGSTOP` / `SIGCONT`；Windows 没有这两个信号
  （`signal.SIGSTOP` 属性都不存在，被 `except` 吞掉后会变成「按钮显示成功、进度照跑」的
  假成功），改走 `ntdll` 的 `NtSuspendProcess` / `NtResumeProcess`。
- **找子进程**：POSIX 用 `pgrep -P`；Windows 用 `wmic` → `PowerShell CIM` 逐级回退。
- **结束进程**：POSIX 发信号；Windows 用 `taskkill`。
- **后台起进程**：POSIX 用 `start_new_session`；Windows 用
  `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`（前者在 Windows 上会被静默忽略）。
- **执行 `vendor/` 里的脚本**：这些文件没有扩展名，Windows 的 `CreateProcess`
  直接执行会报 `WinError 193`，统一改成用当前解释器起。

装了 `psutil` 的话以上全部优先走 psutil（三平台语义一致），没装就走系统命令，都能用。

### 跑起来需要什么

| 依赖 | 说明 |
|---|---|
| Python 3.9+ | 起服务、算几何 |
| numpy | 落点搜索加速（`pip install -r requirements.txt`） |
| **psutil** | **可选但推荐**：进程树、挂起/恢复、结束进程三平台统一走它 |
| **pymobiledevice3** | **可选**：只有把轨迹推到真机才需要。没有它照样能画图、生成 GPX、下载 |
| iPhone / iPad | iOS 17+、**已开开发者模式**、数据线直连、解锁并信任本机 |

图形算法与配速工具（`gpxart` / `runroute`）**已内置在 `vendor/`**，clone 下来就能用，不必额外装。

---

## 界面怎么用

左边从上到下就是一条流水线，右边是实时预览。

**1 · 选图形** — 九个现成图形，点一下即选，尺寸自动填默认值。

| | | |
|---|---|---|
| 爱心 `heart` | 三心同心 `hearts` | 心中有心 `heartnest` |
| 三心横排 `heartsrow` | 五角星 `star` | 无限符号 `infinity` |
| 阿基米德螺旋 `spiral` | NJU 字母 `nju` | 520 数字 `520` |

**2 · 尺寸与位置** — 拖动即改，右侧实时重画。

- *图形大小*：图形的**最长边**（米）。太大放不下会提示，调小即可。
- *离边界最小余量*：图形离校园边界至少留多少米。余量不够时程序会先试「**原地微调角度**」，
  只有实在放不下才允许倾斜（爱心歪着放很难看，所以正放永远优先）。

**3 · 跑法**

- *往复*：跑到终点原路返回，图形重合、不糊。**默认推荐**。
- *同向绕圈*：每圈回到起点重新开始，会在图形外多出一条回程直线。
- *圈数*：图形太小时靠多跑几圈凑距离，比硬放大更安全（放大容易出界）。
- *配速*：整体配速。运动 App 里轨迹会按速度着色。
- *配速渐变*：起点慢、终点快（或反之）。**手机里的轨迹会变成一条彩色带**，比单色好看得多。

**4 · 生成** — 补时间戳 + 逐点越界校验，生成到 `out/`。文件名留空自动命名。

底部第一行是**路线库**：选一条已生成的路线 → 刷新 / 下载 GPX。

底部第二行是**模拟控制台**，四个按钮从左到右就是一次完整模拟的生命周期：

| 按钮 | 做什么 | 备注 |
|---|---|---|
| **① 注入初始位置** | 把手机定位钉到所选路线的起点，并一直保持 | 位置会停在起点不动，直到你点 ④ |
| **② 开始模拟** | 按轨迹把点依次推给手机 | 会自动先做一次 ①，衔接无跳变 |
| **③ 暂停 / 继续** | 冻结 / 解冻注入进程（`SIGSTOP`↔`SIGCONT`，Windows 走 `NtSuspendProcess`） | 连计时一起冻住，**不丢进度**；再点变「继续」 |
| **④ 结束模拟（恢复定位）** | 停回放 + 释放定点 + 发 `clear` | 若手机仍停在旧位置，**拔一次数据线** |

右侧进度条实时显示已推送到第几个点、已跑时长 / 预计时长。

两个细节：

- **起播会自动先把定位钉到轨迹起点**，再交接给回放 —— 否则设备当前位置与起点之间的瞬移
  会被运动 App 记成一条杂线。起播前请确认 App 没在记录。
- 如果你用命令行另起了回放，页面会显示「检测到命令行回放：xxx.gpx」，
  但它**不接管**那个会话（停止按钮对它无效，回命令行处理）。

### 深色模式

右上角的按钮切换浅色 / 深色，选择记在 `localStorage`；从没切过就跟随系统
`prefers-color-scheme`。中间那块画布**始终是深色** —— 它是「出图区」，不跟着主题翻。

---

## 它比「写死的路线」强在哪

| | 固定路线 JSON | NJU Runner |
|---|---|---|
| 路线来源 | 事先手工画好 | 参数化图形，随时改 |
| 换尺寸 | 得重画 | 拖滑块 |
| 摆位 | 靠眼估 | 自动搜索边界内最安全的落点 |
| 越界 | 跑出去才知道 | 生成前逐点校验并报最小余量 |
| 变速 | 不支持 | 配速斜坡 → 彩色轨迹 |

落点搜的是「到边界距离最大的一批候选中心」再逐个试摆放。原版是纯 Python，
用 numpy 重写并做两级搜索（抽稀点粗筛候选 → 前几名再用全点精算）后，
九个图形全部与原算法**逐位一致**（中心误差 0.0000 m），速度快 8~25 倍。
`core.selftest()` 可随时复验。

---

## ⚠️ 三件必须知道的事

1. **回放跑到一半别掐，也别拔线。** 强行中断会让模拟位置残留在设备侧，
   之后 `clear` 也清不掉（这条指令没有回执，退出码恒为 0，不能当判据）。
   要停就用界面上的「暂停」或「结束模拟」。
2. **收尾以「拔一次数据线」为准。** 跑完或停止后程序会自动发 clear，
   但如果手机地图还停在原地，拔线是唯一 100% 可靠的恢复方式
   （或用 `pymobiledevice3 diagnostics restart` 重启设备）。
3. **注入期间手机得连着线、保持解锁。** iOS 只认 USB 连接的开发者会话。

### 能力边界

注入的**只有经纬度**，没有海拔、速度、步数、加速度、陀螺仪。
如果目标 App 用步频做防作弊，这条路会被识别 —— 这是工具的能力边界，不是 bug。

---

## 文件结构

```
nju-runner/
├── 启动.command        双击启动（macOS）
├── start.sh            命令行启动（macOS / Linux，自动找带 numpy 的 Python）
├── start.bat           双击启动（Windows）
├── server.py           本地 HTTP 服务 + REST API（纯标准库）
├── core.py             核心：加载 vendor 工具、numpy 落点搜索、平台层、设备与回放控制
├── static/             前端单页（无 CDN 依赖，离线可用）
│   ├── index.html · app.js · style.css
├── vendor/             内置的 gpxart / runroute（上游在 ~/.local/bin）
├── tools/
│   ├── sync-vendor.py      从上游同步 vendor/（跨平台，替换失配会报错）
│   ├── sync-vendor.sh      上面那个的 zsh 壳
│   ├── selftest.py         跨平台自检（环境 / 几何 / 平台原语 / 服务端到端）
│   └── windows-selftest.ps1  Windows 一键跑自检
├── data/boundaries/    边界 GeoJSON（可自行添加）
├── docs/               截图
└── out/                生成的 GPX 与回放日志（不入库）
```

### 关于 `vendor/`

`gpxart`（图形与落点）和 `runroute`（配速与时间戳）是两条独立命令行工具，
为了让仓库自包含，这里内置了一份。加载顺序是
**环境变量 `GPXART_PATH` / `RUNROUTE_PATH` → `vendor/` → `~/.local/bin/`**。

内置副本相对上游改了三处：

1. shebang → `#!/usr/bin/env python3`；
2. `gpxart` 里改成优先找同目录的 `runroute`；
3. `runroute` 里的 `pymobiledevice3` 路径不再是写死的绝对路径，而是
   **环境变量 `PMD` / `PYMOBILEDEVICE3` → `PATH` → 常见安装位置**。

第 3 条是必需的：写死路径意味着别人 clone 下来点「开始模拟」会直接报
「找不到 pymobiledevice3」，而 `clear` 会静默变成空操作。

上游更新后同步：

```shell
./tools/sync-vendor.sh                            # macOS / Linux
python tools\sync-vendor.py                       # Windows
GPXART_SRC=/path/to/bin python tools/sync-vendor.py   # 换上游目录
```

同步脚本用的是**带断言的字面替换**：上游改了措辞、锚点找不到时会直接报错退出，
而不是静默产出一份坏的副本。跑完还会横扫一遍，确认没有本机路径泄漏进仓库。

---

## API（想自己接别的界面也行）

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/api/shapes` | 图形列表 |
| GET | `/api/boundaries` | 可用边界 |
| POST | `/api/preview` | `{shape,size,margin,boundary}` → 几何 + 余量 + 点列 |
| POST | `/api/generate` | 同上 + `{pace,laps,laps_mode,pace_ramp,out_name}` → 生成 GPX |
| POST | `/api/check` | 对已有 GPX 做逐点越界校验 |
| GET | `/api/routes` | `out/` 里的路线列表 |
| GET | `/api/device` | 设备连接状态 |
| POST | `/api/prime` | ① 注入初始位置：`{file}` 钉到该路线起点，或 `{lat,lon}` 钉到任意坐标 |
| POST | `/api/play` `{file}` | ② 开始模拟 |
| POST | `/api/pause` / `/api/resume` | ③ 暂停 / 继续 |
| POST | `/api/release` | ④ 结束模拟（停回放 + 释放定点 + clear） |
| POST | `/api/stop` | 仅停回放（不管定点） |
| GET | `/api/status` | 回放进度（含 `paused` / `holder` / `external`） |
| GET | `/api/download?file=` | 下载 GPX |

### 暂停是怎么做到的

回放链路是 `runroute play` → `pymobiledevice3 developer dvt simulate-location play`，
后者是个「按相邻点时间差 sleep，然后 set」的循环。

暂停 = 把**注入进程**冻住 —— 连 sleep 计时一起冻。
继续 = 解冻。所以进度、计时都不丢，也不会在设备侧留下残留
（对比之下，直接 kill 就会残留，只能拔线）。

冻住的办法按平台分：POSIX 是 `SIGSTOP` / `SIGCONT`；Windows 没有这两个信号，
走 `ntdll.NtSuspendProcess` / `NtResumeProcess`（需要 `PROCESS_SUSPEND_RESUME` 权限的句柄）。
装了 psutil 则两边都优先用 `psutil.Process.suspend()/resume()`。

服务退出时会先解冻再杀，避免留下一个冻结的僵尸进程。

---

## 换一个校园

把目标区域的边界 GeoJSON（WGS-84）丢进 `data/boundaries/`，刷新页面即可选用。
边界越精确，「离边界最小余量」越有意义。

> 注意坐标系：本工具全程用 **WGS-84**（GPS / iOS 注入口径）。
> 高德、腾讯、百度用的是 **GCJ-02**，同一地点两者能差四五百米 ——
> 拿高德上的点去对轨迹，一定会觉得"偏了"。

## 许可

MIT，见 [LICENSE](LICENSE)。
