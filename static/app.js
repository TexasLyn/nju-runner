/* NJU Runner · 前端交互
   状态 → 防抖预览 → 按容器尺寸画 SVG；生成 / 注入 / 起播 / 暂停 / 结束 走 REST。
   主题：localStorage 记忆；没存过就跟随系统（prefers-color-scheme）。
   路线：图形（校界内自动落点） / 操场（OSM 真实跑道，沿轮廓跑 N 圈）。
   底图：极简（自绘网格，恒深色） / 真实地图（OSM 瓦片，走后端代理 + 本地缓存）。 */

const $ = (id) => document.getElementById(id);

const S = {
  mode: "shape",        // shape | track
  campus: "xianlin",    // gulou | xianlin | pukou | suzhou
  boundary: "boundary-nju-xianlin.geojson",
  shape: "heart",
  track: "",            // 操场文件名
  direction: "ccw",
  size: 560,
  margin: 60,
  laps: 1,
  lapsMode: "retrace",
  pace: 360,            // 秒/km
  rampOn: false,
  rampA: 450,
  rampB: 270,
  noise: 8,             // 配速波动 ±%（真实跑步不会匀速）
  jitter: 2.5,          // GPS 漂移 米
  pan: [0, 0],          // 手动摆位：相对自动落点的整体平移 [dlat, dlon]（度）
  rot: 0,               // 手动旋转（度，逆时针为正）
  device: false,
  theme: "light",
  basemap: "minimal",   // minimal | osm
};

let shapesMeta = {};
let boundariesMeta = [];
let tracksMeta = [];
let pollTimer = null;
let lastData = null;      // 最近一次预览结果，供 resize 重绘
let lastProj = null;      // 最近一次投影（含 .inv），拖动时要用
let baseTrack = null;     // 未经手动平移的原始落点
let baseCenter = null;
let lastCanvas = null;    // 最近一次画布的 {W,H,pad}，用来限制拖拽范围
let lastHandles = { pan: null, rot: null };   // 两个拖把当前的屏幕位置
let drag = null;          // 正在拖动中的状态
let evalTimer = null;
let playState = { running: false, paused: false, external: null };
let runningFile = null;   // 服务器上正在跑的文件名（tick 刷新）

const K = {
  route: "njurunner.route", theme: "njurunner.theme", campus: "njurunner.campus",
  mode: "njurunner.mode", basemap: "njurunner.basemap", track: "njurunner.track",
};

const fmtPace = (s) => `${Math.floor(s / 60)}'${String(s % 60).padStart(2, "0")}"`;
const fmtM = (m) => m >= 1000 ? (m / 1000).toFixed(2) + " km" : Math.round(m) + " m";
const fmtHM = (sec) => {
  sec = Math.round(sec || 0);
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return h ? `${h}时${m}分` : (m ? `${m}分${String(s).padStart(2, "0")}秒` : `${s}秒`);
};
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const store = {
  get(k, d) { try { return localStorage.getItem(k) || d; } catch (e) { return d; } },
  set(k, v) { try { v ? localStorage.setItem(k, v) : localStorage.removeItem(k); } catch (e) {} },
};

/* ---------------------------------------------------------------- 主题 */
const mqDark = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;

function applyTheme(t, persist) {
  document.documentElement.dataset.theme = t;
  S.theme = t;
  if (persist) store.set(K.theme, t);
  if (lastData) drawMap();
}

function toggleTheme() {
  applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark", true);
}

async function api(path, body) {
  const opt = body
    ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }
    : {};
  const res = await fetch(path, opt);
  const data = await res.json().catch(() => ({ error: "响应不是 JSON" }));
  if (!res.ok || data.error) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

/* ---------------------------------------------------------------- 加载元数据 */
async function loadShapes() {
  const { shapes } = await api("/api/shapes");
  shapesMeta = {};
  $("shapes").innerHTML = "";
  shapes.forEach((s) => {
    shapesMeta[s.key] = s;
    const b = document.createElement("button");
    b.dataset.k = s.key;
    b.innerHTML = `<b>${s.name}</b><em>${s.key} · ${Math.round(s.default_size)}m</em>`;
    if (s.key === S.shape) b.classList.add("on");
    b.onclick = () => {
      S.shape = s.key;
      S.size = s.default_size;
      $("size").value = s.default_size;
      $("sizeOut").textContent = Math.round(s.default_size) + " m";
      document.querySelectorAll("#shapes button").forEach(x => x.classList.toggle("on", x.dataset.k === s.key));
      queue();
    };
    $("shapes").appendChild(b);
  });
}

async function loadBoundaries() {
  const { boundaries } = await api("/api/boundaries");
  boundariesMeta = boundaries;
  const seg = $("campusSeg");
  seg.innerHTML = "";
  boundaries.forEach((b) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.dataset.c = b.campus || b.file;
    btn.textContent = b.label || b.name;
    btn.title = `${b.name} · ${b.area_km2} km² · ${b.file}`;
    btn.onclick = () => setCampus(b.campus || b.file);
    seg.appendChild(btn);
  });
  // 记住的校区不在列表里（删过文件）就退回第一个
  const want = store.get(K.campus, S.campus);
  setCampus(boundaries.some(b => b.campus === want) ? want : (boundaries[0] || {}).campus || "");
}

function campusMeta(c) {
  return boundariesMeta.find(b => b.campus === c) || null;
}

async function loadTracks() {
  try {
    const { tracks } = await api("/api/tracks");
    tracksMeta = tracks;
  } catch (e) {
    tracksMeta = [];
  }
  rebuildTrackSel();
}

/* ---------------------------------------------------------------- 模式 / 校区 */
function setMode(m) {
  if (m === S.mode) return;
  const entering = m === "track";
  S.mode = m;
  store.set(K.mode, m);
  S.pan = [0, 0]; S.rot = 0;            // 换模式，手动摆位作废
  // 圈数语义不同：图形一般 1 圈，操场一般 4 圈
  if (entering && S.laps < 2) S.laps = 4;
  if (!entering && S.laps > 6) S.laps = 1;
  $("laps").value = S.laps;
  $("lapsOut").textContent = S.laps + " 圈";
  applyMode();
  refresh();
}

function applyMode() {
  const track = S.mode === "track";
  $("shapes").classList.toggle("hidden", track);
  $("trackOnlyHint").classList.toggle("hidden", !track);
  $("trackBox").classList.toggle("hidden", !track);
  $("blkPlace").classList.toggle("hidden", track);
  $("lapsMode").classList.toggle("hidden", track);
  $("dirSeg").classList.toggle("hidden", !track);
  $("laps").max = track ? 12 : 6;
  document.querySelectorAll("#modeSeg button").forEach(b => b.classList.toggle("on", b.dataset.v === S.mode));
  document.querySelectorAll("#dirSeg button").forEach(b => b.classList.toggle("on", b.dataset.v === S.direction));
  $("outName").placeholder = track ? "留空自动命名（track-…）" : "留空自动命名";
  renderCampusHint();
  renumber();
}

function setCampus(c) {
  if (!c) return;
  S.campus = c;
  store.set(K.campus, c);
  S.pan = [0, 0]; S.rot = 0;            // 换校区，上一个校区的手动摆位没意义
  const meta = campusMeta(c);
  if (meta) S.boundary = meta.file;
  document.querySelectorAll("#campusSeg button").forEach(b => b.classList.toggle("on", b.dataset.c === c));
  rebuildTrackSel();
  renderCampusHint();
  refresh();
}

function rebuildTrackSel() {
  const sel = $("trackSel");
  if (!sel) return;
  const list = tracksMeta.filter(t => !t.error && t.campus === S.campus);
  sel.innerHTML = "";
  if (!tracksMeta.length) {
    sel.appendChild(new Option("（没找到内置跑道）", ""));
    S.track = "";
    syncTrackLen();            // 必须清一次，否则会留着上一个校区的「400 m / 圈」
    return;
  }
  if (!list.length) {
    sel.appendChild(new Option("— 这个校区暂未收录跑道 —", ""));
    S.track = "";
    syncTrackLen();
    return;
  }
  const remembered = store.get(K.track, "");
  list.forEach(t => {
    sel.appendChild(new Option(`${t.name} · ${Math.round(t.length_m)} m`, t.file));
  });
  const hit = list.some(t => t.file === remembered) ? remembered
    : (list.some(t => t.file === S.track) ? S.track : list[0].file);
  sel.value = hit;
  S.track = hit;
  store.set(K.track, hit);
  syncTrackLen();
}

function syncTrackLen() {
  const t = tracksMeta.find(z => z.file === $("trackSel").value);
  $("trackLen").textContent = t ? `${Math.round(t.length_m)} m / 圈` : "";
  const h = $("trackHint");
  if (t) {
    h.innerHTML = `${t.label || ""} · ${t.points} 个顶点 · ${t.source || ""}`;
  } else {
    h.textContent = "";
  }
}

function renderCampusHint() {
  const el = $("campusHint");
  const m = campusMeta(S.campus);
  if (S.mode === "track") {
    const n = tracksMeta.filter(t => !t.error && t.campus === S.campus).length;
    el.innerHTML = m
      ? (n
          ? `真实跑道取自 OpenStreetMap；本校区收录 <b>${n}</b> 条。`
          : `真实跑道取自 OpenStreetMap；<span class="warn">本校区还没收录跑道，` +
            `可先用「图形轨迹」画。</span>`)
      : "（校区信息未加载）";
    return;
  }
  el.innerHTML = m
    ? `校界 <b>${m.area_km2}</b> km² · <b>${m.points}</b> 个顶点 · 中心 <code>${m.center[0].toFixed(5)}, ${m.center[1].toFixed(5)}</code>`
    : "（校区信息未加载）";
}

/* 章节序号按「当前可见的块」重排 —— 操场模式下有几块是隐藏的 */
function renumber() {
  let i = 0;
  document.querySelectorAll("#panel .block").forEach(b => {
    if (b.classList.contains("hidden")) return;
    i += 1;
    const h = b.querySelector("h2");
    if (h) h.textContent = `${i} · ${b.dataset.title || ""}`;
  });
}

/* ---------------------------------------------------------------- 投影 */
function makeProj(ring, W, H, pad) {
  const M = 111320;
  const lats = ring.map(p => p[0]), lons = ring.map(p => p[1]);
  const la0 = (Math.min(...lats) + Math.max(...lats)) / 2;
  const lo0 = (Math.min(...lons) + Math.max(...lons)) / 2;
  const k = Math.cos(la0 * Math.PI / 180);
  const toXY = ([la, lo]) => [(lo - lo0) * k * M, (la - la0) * M];
  const xy = ring.map(toXY);
  const xs = xy.map(p => p[0]), ys = xy.map(p => p[1]);
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  const y0 = Math.min(...ys), y1 = Math.max(...ys);
  const sc = Math.min((W - 2 * pad) / Math.max(1e-6, x1 - x0),
                      (H - 2 * pad) / Math.max(1e-6, y1 - y0));
  const cx = (x0 + x1) / 2, cy = (y0 + y1) / 2;
  const f = ([la, lo]) => {
    const [x, y] = toXY([la, lo]);
    return [W / 2 + (x - cx) * sc, H / 2 - (y - cy) * sc];
  };
  f.scale = sc;
  // 反投影：OSM 瓦片要按「屏幕四角对应哪块瓦片」来铺，必须有逆变换
  f.inv = ([px, py]) => {
    const x = (px - W / 2) / sc + cx;
    const y = -(py - H / 2) / sc + cy;
    return [la0 + y / M, lo0 + x / (k * M)];
  };
  return f;
}

/* ---------------------------------------------------------------- OSM 瓦片 */
const MERC_Z0 = 156543.03392;          // z=0 赤道处的 m/px
const TILE_PX = 256;

const lon2tx = (lon, z) => Math.floor((lon + 180) / 360 * Math.pow(2, z));
function lat2ty(lat, z) {
  const r = lat * Math.PI / 180;
  return Math.floor((1 - Math.log(Math.tan(r) + 1 / Math.cos(r)) / Math.PI) / 2 * Math.pow(2, z));
}
const tx2lon = (tx, z) => tx / Math.pow(2, z) * 360 - 180;
function ty2lat(ty, z) {
  const n = Math.PI - 2 * Math.PI * ty / Math.pow(2, z);
  return 180 / Math.PI * Math.atan(0.5 * (Math.exp(n) - Math.exp(-n)));
}

let tileKey = "";
let tileHTML = "";

function buildTiles(proj, W, H) {
  const [laA, loA] = proj.inv([0, 0]);
  const [laB, loB] = proj.inv([W, H]);
  const latN = Math.max(laA, laB), latS = Math.min(laA, laB);
  const lonW = Math.min(loA, loB), lonE = Math.max(loA, loB);

  const mpp = 1 / proj.scale;                       // 屏幕上一像素代表多少米
  const latC = (latN + latS) / 2;
  let z = clamp(Math.round(Math.log2(MERC_Z0 * Math.cos(latC * Math.PI / 180) / mpp)), 2, 19);
  let tx0, tx1, ty0, ty1;
  for (; z > 2; z--) {                              // 控制请求量：一张 256px 的瓦片批量别太大
    tx0 = lon2tx(lonW, z) - 1; tx1 = lon2tx(lonE, z) + 1;
    ty0 = lat2ty(latN, z) - 1; ty1 = lat2ty(latS, z) + 1;
    if ((tx1 - tx0 + 1) * (ty1 - ty0 + 1) <= 40) break;
  }
  tx0 = Math.max(0, tx0); ty0 = Math.max(0, ty0);
  const lim = Math.pow(2, z) - 1;
  tx1 = Math.min(lim, tx1); ty1 = Math.min(lim, ty1);

  const key = `${z}|${tx0}|${ty0}|${tx1}|${ty1}|${W}x${H}|${Math.round(proj.scale * 1e6)}`;
  if (key === tileKey && tileHTML) return tileHTML;   // 参数没变就复用，避免拖滑块时反复请求

  window.__tileFail = 0; window.__tileOk = 0;
  let out = "";
  for (let tx = tx0; tx <= tx1; tx++) {
    for (let ty = ty0; ty <= ty1; ty++) {
      const [x1, y1] = proj([ty2lat(ty, z), tx2lon(tx, z)]);
      const [x2, y2] = proj([ty2lat(ty + 1, z), tx2lon(tx + 1, z)]);
      const X = Math.min(x1, x2), Y = Math.min(y1, y2);
      const w = Math.abs(x2 - x1) + 0.6, h = Math.abs(y2 - y1) + 0.6;
      out += `<image href="/api/tile?z=${z}&x=${tx}&y=${ty}" x="${X.toFixed(1)}" y="${Y.toFixed(1)}"`
           + ` width="${w.toFixed(1)}" height="${h.toFixed(1)}" preserveAspectRatio="none"`
           + ` onload="window.__tileOk=(window.__tileOk||0)+1"`
           + ` onerror="window.__tileFail=(window.__tileFail||0)+1"/>`;
    }
  }
  tileKey = key; tileHTML = out;
  return out;
}

function setBasemap(v) {
  S.basemap = v;
  store.set(K.basemap, v);
  if (v !== "osm") { $("tileWarn").classList.add("hidden"); tileKey = ""; tileHTML = ""; }
  syncBasemapChrome();
  if (lastData) drawMap();
}

/* 底图相关的三处 UI 只在这里改，避免 setBasemap / refresh / 空状态各写一遍写岔。
   「真实地图」只有在真有图可画时才算生效 —— 空状态下按钮该显示的是「点它能去哪」。 */
function syncBasemapChrome() {
  const on = S.basemap === "osm" && !!lastData;
  $("btnMap").classList.toggle("on", on);
  $("mapLabel").textContent = on ? "极简底图" : "真实地图";
  $("osmAttrib").classList.toggle("hidden", !on);
  $("canvasWrap").classList.toggle("osm", on);
}

function toggleBasemap() { setBasemap(S.basemap === "osm" ? "minimal" : "osm"); }

/* ---------------------------------------------------------------- 手动摆位
   预览图上有两个拖把，都是「形状不变」的刚体变换：
     · 起点那个绿点      → 整条轨迹平移
     · 图形外侧那个旋转钮 → 绕图形中心旋转
   拖动过程纯本地重画、不发请求，所以是帧率级的顺滑；松手后才回后端
   算一次精确余量（点到边界最短距离是 numpy 算的，前端算不了）。

   四个坑：
   1. 鼠标坐标 → SVG 坐标必须走 getScreenCTM().inverse()。
      因为 preserveAspectRatio="xMidYMid meet" 会有 letterbox，
      直接按元素宽高折算一定偏。
   2. lastData.track 始终是「已经变换过」的，所以基准点另存 baseTrack；
      否则第二次拖动会把位移/角度累加两遍。
   3. 旋转必须换到「局部平面（米）」里做：经度一度和纬度一度的实际长度
      差一个 cos(纬度)，直接在经纬度上转会把图形拉斜。
   4. 旋转角逐帧累积差值，而不是每帧算绝对角 —— 跨过 ±180° 不会突然跳 360°。 */

const HIT_R = 20;          // 抓手命中半径（SVG 用户单位）
const ROT_SNAP = 15;       // 按住 Shift 时的角度吸附步长（度）
const M_PER_DEG = 111320;

function evtToLL(e) {
  const svg = $("map");
  const ctm = svg.getScreenCTM();
  if (!ctm || !lastProj) return null;
  const p = svg.createSVGPoint();
  p.x = e.clientX; p.y = e.clientY;
  const l = p.matrixTransform(ctm.inverse());
  return { ll: lastProj.inv([l.x, l.y]), xy: [l.x, l.y] };
}

function panMeters() {
  const [dlat, dlon] = S.pan;
  const la = (baseCenter ? baseCenter[0] : 32.0) * Math.PI / 180;
  return [dlon * M_PER_DEG * Math.cos(la), dlat * M_PER_DEG];    // [东, 北]
}

function hasPan() { return !!(S.pan[0] || S.pan[1]); }
function hasManip() { return hasPan() || !!S.rot; }

/* 绕 baseCenter 旋转 deg 度（逆时针为正）。在局部平面里转，保证形状不变形。 */
function rotLL(pts, deg) {
  if (!baseCenter || !deg) return pts;
  const k = Math.cos(baseCenter[0] * Math.PI / 180);
  const th = deg * Math.PI / 180, cs = Math.cos(th), sn = Math.sin(th);
  return pts.map(([la, lo]) => {
    const x = (lo - baseCenter[1]) * k * M_PER_DEG;     // 东
    const y = (la - baseCenter[0]) * M_PER_DEG;         // 北
    return [baseCenter[0] + (x * sn + y * cs) / M_PER_DEG,
            baseCenter[1] + (x * cs - y * sn) / (k * M_PER_DEG)];
  });
}

/* 先旋转、再平移。显示和生成共用这一份变换，保证所见即所得。 */
function xformTrack(pts) {
  if (!baseCenter || !pts) return pts;
  let out = S.rot ? rotLL(pts, S.rot) : pts;
  if (hasPan()) out = out.map(([la, lo]) => [la + S.pan[0], lo + S.pan[1]]);
  return out;
}

/* 图形中心当前在哪（旋转不移动中心，只有平移会） */
function centerNow() {
  return baseCenter ? [baseCenter[0] + S.pan[0], baseCenter[1] + S.pan[1]] : null;
}

/* 某经纬度相对图形中心的方位角：0° = 正东，逆时针为正 */
function angAt(ll) {
  const c = centerNow();
  if (!c || !ll) return 0;
  const k = Math.cos(c[0] * Math.PI / 180);
  return Math.atan2((ll[0] - c[0]) * M_PER_DEG,
                    (ll[1] - c[1]) * k * M_PER_DEG) * 180 / Math.PI;
}

/* 限制拖拽范围：越界是允许的，但不许把轨迹挪到「连抓手都看不见」的地方 ——
   那会让人以为功能坏了。抓手在起点上，所以**起点必须留在画布可视区内**。
   用画布像素坐标来卡，比按校界包围盒卡更准（画布还有留白和 letterbox）。 */
function clampPan(pan) {
  if (!lastProj || !lastCanvas || !baseTrack) return pan;
  const a = rotLL(baseTrack.slice(0, 1), S.rot)[0];     // 旋转之后的起点
  const [x, y] = lastProj([a[0] + pan[0], a[1] + pan[1]]);
  const { W, H, pad } = lastCanvas;
  const cx = clamp(x, pad, W - pad);
  const cy = clamp(y, pad, H - pad);
  if (cx === x && cy === y) return pan;
  const back = lastProj.inv([cx, cy]);
  return [back[0] - a[0], back[1] - a[1]];
}

function applyManip(d) {
  if (!d || !d.track || d.kind === "track" || !hasManip() || !baseTrack) return d;
  return Object.assign({}, d, {
    track: xformTrack(baseTrack),
    center: centerNow(),
    manual: true,
  });
}

function syncPan() {
  const on = hasManip();
  const btn = $("btnReset");
  if (btn) btn.classList.toggle("hidden", !on);
  const el = $("panHint");
  if (!el) return;
  if (!on) { el.textContent = "拖绿点挪位置，拖外侧圆钮转角度"; return; }
  const parts = [];
  if (hasPan()) {
    const [east, north] = panMeters();
    parts.push(`<b>${east >= 0 ? "东" : "西"} ${Math.abs(east).toFixed(0)} m</b>` +
               ` · <b>${north >= 0 ? "北" : "南"} ${Math.abs(north).toFixed(0)} m</b>`);
  }
  if (S.rot) parts.push(`<b>旋转 ${S.rot > 0 ? "+" : ""}${S.rot.toFixed(1)}°</b>`);
  el.innerHTML = "已手动摆位：" + parts.join(" · ");
}

function resetManip() {
  if (!hasManip()) return;
  S.pan = [0, 0];
  S.rot = 0;
  syncPan();
  refresh();
}

function scheduleEval() {
  clearTimeout(evalTimer);
  evalTimer = setTimeout(runEval, 150);
}

async function runEval() {
  if (!lastData || lastData.kind === "track") return;
  if (!hasManip()) return;              // 没手动动过，后端 preview 的值就是准的
  try {
    const ev = await api("/api/eval", {
      points: lastData.track, boundary: S.boundary, margin: S.margin,
    });
    // 只取「余量」这几个字段。center 是后端按顶点算的形心，跟图形中心不是一回事，
    // 拿它覆盖会让旋转钮的位置微微漂移。
    const { center, points, boundary, ...rest } = ev;
    Object.assign(lastData, rest);
    drawMap();
    renderStats(lastData);
  } catch (e) { /* 服务没起来就静默，下次拖动再试 */ }
}

function onDragDown(e) {
  if (!lastData || lastData.kind === "track" || !lastProj) return;
  if (e.button) return;                 // 只认左键 / 触摸
  const hit = evtToLL(e);
  if (!hit) return;
  const near = (q) => q && Math.hypot(hit.xy[0] - q[0], hit.xy[1] - q[1]) <= HIT_R;
  let mode = null;
  if (near(lastHandles.rot)) mode = "rot";
  else if (near(lastHandles.pan)) mode = "pan";
  if (!mode) return;

  drag = { mode, moved: false };
  if (mode === "pan") {
    drag.start = hit.ll;
    drag.basePan = S.pan.slice();
  } else {
    drag.acc = 0;                       // 角度累计（度）
    drag.lastAng = angAt(hit.ll);
    drag.baseRot = S.rot;
  }
  const svg = $("map");
  try { svg.setPointerCapture(e.pointerId); } catch (_) {}
  svg.classList.add("dragging");
  e.preventDefault();
}

function onDragMove(e) {
  if (!drag) return;
  const hit = evtToLL(e);
  if (!hit) return;

  if (drag.mode === "pan") {
    const dlat = hit.ll[0] - drag.start[0], dlon = hit.ll[1] - drag.start[1];
    if (!drag.moved && Math.hypot(dlat, dlon) * M_PER_DEG < 2.5) return;   // 2.5 m 内算手抖
    drag.moved = true;
    S.pan = clampPan([drag.basePan[0] + dlat, drag.basePan[1] + dlon]);
  } else {
    let d = angAt(hit.ll) - drag.lastAng;
    if (d > 180) d -= 360;
    if (d < -180) d += 360;
    drag.lastAng += d;
    drag.acc += d;
    if (!drag.moved && Math.abs(drag.acc) < 1.0) return;                   // 1° 内算手抖
    drag.moved = true;
    S.rot = drag.baseRot + drag.acc;
    if (e.shiftKey) S.rot = Math.round(S.rot / ROT_SNAP) * ROT_SNAP;       // Shift 吸附
  }

  lastData.track = xformTrack(baseTrack);
  lastData.center = centerNow();
  lastData.manual = true;
  drawMap();
  syncPan();
  e.preventDefault();
}

function onDragUp(e) {
  if (!drag) return;
  const moved = drag.moved;
  drag = null;
  const svg = $("map");
  svg.classList.remove("dragging");
  try { svg.releasePointerCapture(e.pointerId); } catch (_) {}
  if (moved) { syncPan(); scheduleEval(); }
}
function drawMap() {
  if (!lastData) return;
  const d = lastData;
  const wrap = $("canvasWrap");
  const rect = wrap.getBoundingClientRect();
  const W = Math.max(360, Math.round(wrap.clientWidth || rect.width || 900));
  const H = Math.max(260, Math.round(wrap.clientHeight || rect.height || 620));
  const pad = clamp(Math.min(W, H) * 0.09, 34, 76);
  lastCanvas = { W, H, pad };        // 拖拽范围限制要用
  const osm = S.basemap === "osm";
  const dark = S.theme === "dark";

  const svg = $("map");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  if (!svg.dataset.ready) {          // 三层：底 → 瓦片 → 覆盖物（分开更新，避免瓦片被反复重建）
    svg.innerHTML = '<g class="back"></g><g class="tiles"></g><g class="overlay"></g>';
    svg.dataset.ready = "1";
  }
  const gBack = svg.querySelector("g.back");
  const gTiles = svg.querySelector("g.tiles");
  const gOver = svg.querySelector("g.overlay");

  const ring = d.view || d.boundary || d.track;
  const proj = makeProj(ring, W, H, pad);
  lastProj = proj;                   // 拖动时要用它的反投影
  const path = (pts) => pts.map(p => {
    const [x, y] = proj(p);
    return x.toFixed(1) + "," + y.toFixed(1);
  }).join(" ");

  const step = Math.round(clamp(Math.min(W, H) / 8, 46, 96));
  const canvasBg = osm ? (dark ? "#171b21" : "#e9e5de") : "#0f1218";

  /* ---- 底层：背景 + 网格（极简模式才画网格）---- */
  let grid = "";
  if (!osm) {
    for (let x = 0; x <= W; x += step) grid += `<line x1="${x}" y1="0" x2="${x}" y2="${H}"/>`;
    for (let y = 0; y <= H; y += step) grid += `<line x1="0" y1="${y}" x2="${W}" y2="${y}"/>`;
  }
  gBack.innerHTML = `
    <defs>
      <pattern id="g" width="${step}" height="${step}" patternUnits="userSpaceOnUse">
        <path d="M ${step} 0 L 0 0 0 ${step}" fill="none" stroke="#1a2029" stroke-width="1"/>
      </pattern>
      <linearGradient id="tg" x1="0" y1="0" x2="1" y2="0">
        <stop offset="0" stop-color="#ffb457"/><stop offset="1" stop-color="#ff7a45"/>
      </linearGradient>
    </defs>
    <rect width="${W}" height="${H}" fill="${canvasBg}"/>
    ${grid ? `<rect width="${W}" height="${H}" fill="url(#g)"/>` : ""}`;

  /* ---- 瓦片层 ---- */
  gTiles.innerHTML = osm ? buildTiles(proj, W, H) : "";

  /* ---- 覆盖层：校界 / 跑道 / 起终点 / 文字 ---- */
  const inkCol = osm ? (dark ? "#e8eef7" : "#25313f") : "#8ea4bf";
  const halo = canvasBg;
  const casing = osm ? (dark ? "#0b0e13" : "#ffffff") : "#0f1218";
  const sw = clamp(Math.min(W, H) / 190, 2.2, 4.4);
  const bw = clamp(Math.min(W, H) / 400, 1.4, 2.4);
  const fs = clamp(Math.min(W, H) / 52, 11, 16);

  let layers = "";

  // 校界：极简模式填充；真实地图模式只描边 + 淡填充（别把地图糊住）
  if (d.boundary) {
    const bp = path(d.boundary);
    layers += osm
      ? `<polygon points="${bp}" fill="${dark ? "rgba(79,143,232,.13)" : "rgba(47,111,208,.10)"}"
                  stroke="${dark ? "#7cb0f4" : "#2f6fd0"}" stroke-width="${bw}"
                  stroke-linejoin="round" stroke-dasharray="7 5" opacity="0.95"/>`
      : `<polygon points="${bp}" fill="#16283c" stroke="#5f9ce0" stroke-width="${bw}"
                  stroke-linejoin="round" opacity="0.95"/>`;
  }
  // 操场模式：校园轮廓给一条很淡的虚线做参照
  if (osm && d.campus_boundary) {
    layers += `<polygon points="${path(d.campus_boundary)}" fill="none"
                stroke="${dark ? "rgba(160,190,230,.45)" : "rgba(47,111,208,.42)"}"
                stroke-width="${bw}" stroke-dasharray="4 6"/>`;
  }

  const tp = path(d.track);
  const [sx, sy] = proj(d.track[0]);
  const [ex, ey] = proj(d.track[d.track.length - 1]);
  const [clx, cly] = proj(d.center);
  const sameEnds = Math.abs(sx - ex) < 1.5 && Math.abs(sy - ey) < 1.5;
  // 越界超过 1 km（danger）才把轨迹染红；1 km 内的黄警告由画布上的警示条表达，
  // 免得「正常橙」和「警告黄」在轨迹上糊成一片。
  const lv = d.kind === "track" ? "ok" : (d.level || (d.margin_m > 0 ? "ok" : "warn"));
  const trackColor = lv === "danger" ? "#e0464b" : "url(#tg)";

  // 描边（casing）让轨迹在真实地图上也清楚；极简模式本来就有对比，不加也罢
  if (osm) {
    layers += `<polyline points="${tp}" fill="none" stroke="${casing}" stroke-width="${sw + 3.4}"
                stroke-linejoin="round" stroke-linecap="round" opacity="0.9"/>`;
  }
  layers += `<polyline points="${tp}" fill="none" stroke="${trackColor}" stroke-width="${sw}"
              stroke-linejoin="round" stroke-linecap="round"/>`;
  layers += `<circle cx="${clx.toFixed(1)}" cy="${cly.toFixed(1)}" r="3" fill="#ffffff" opacity="0.28"/>`;
  if (!sameEnds) {
    layers += `<circle cx="${ex.toFixed(1)}" cy="${ey.toFixed(1)}" r="${sw * 2}"
                fill="#e0464b" stroke="${casing}" stroke-width="2.5"/>`;
  }
  layers += `<circle cx="${sx.toFixed(1)}" cy="${sy.toFixed(1)}" r="${sw * 2}" fill="#25c06b"
              stroke="${casing}" stroke-width="2.5"/>`;

  // 两个拖把：起点绿点 = 整体平移；图形外侧的圆钮 = 绕中心旋转。
  // 只在图形模式给 —— 操场是真实跑道，位置和朝向都不能挪。
  if (d.kind !== "track") {
    lastHandles.pan = [sx, sy];
    // 旋转钮放在图形中心正上方，离「最远的那个点」再外扩一点，免得压在轨迹上
    let reach = 0;
    for (const p of d.track) {
      const [qx, qy] = proj(p);
      reach = Math.max(reach, Math.hypot(qx - clx, qy - cly));
    }
    const knob = [clx, Math.max(22, cly - reach - 30)];
    lastHandles.rot = knob;
    layers += `<g class="rot">
        <line class="rot-arm" x1="${clx.toFixed(1)}" y1="${cly.toFixed(1)}"
              x2="${knob[0].toFixed(1)}" y2="${knob[1].toFixed(1)}"
              stroke="${inkCol}" stroke-width="1.1" stroke-dasharray="4 4" opacity="0.7"/>
        <circle class="rot-hit" cx="${knob[0].toFixed(1)}" cy="${knob[1].toFixed(1)}" r="${HIT_R}"/>
        <circle class="rot-knob" cx="${knob[0].toFixed(1)}" cy="${knob[1].toFixed(1)}" r="8"
                stroke="${casing}" stroke-width="2"/>
        <path class="rot-glyph" d="${rotGlyph(knob[0], knob[1])}"
              stroke="${casing}" stroke-width="1.8" fill="none" stroke-linecap="round"/>
      </g>`;
    layers += `<g class="grab">
        <circle class="grab-hit" cx="${sx.toFixed(1)}" cy="${sy.toFixed(1)}" r="${HIT_R}"/>
        <circle class="grab-ring" cx="${sx.toFixed(1)}" cy="${sy.toFixed(1)}"
                r="${(sw * 3.4).toFixed(1)}" stroke="${casing}" stroke-width="0.9"/>
      </g>`;
    if (S.rot) {
      layers += `<text x="${(knob[0] + 15).toFixed(1)}" y="${(knob[1] + 4).toFixed(1)}"
          fill="${inkCol}" font-size="${fs - 1}" font-family="monospace"
          stroke="${halo}" stroke-width="3" paint-order="stroke"
          stroke-linejoin="round">${S.rot > 0 ? "+" : ""}${S.rot.toFixed(1)}°</text>`;
    }
  } else {
    lastHandles.pan = lastHandles.rot = null;
  }

  // 比例尺 + 罗盘 + 图形名（文字都带同色描边，压在瓦片上也读得清）
  const nice = [50, 100, 200, 250, 500, 1000, 2000].find(v => v >= 150 / proj.scale) || 2000;
  const barPx = nice * proj.scale;
  const bx = W - 34 - barPx, by = H - 30;
  layers += `<g stroke="${inkCol}" stroke-width="1.8">
      <line x1="${bx}" y1="${by}" x2="${bx + barPx}" y2="${by}"/>
      <line x1="${bx}" y1="${by - 5}" x2="${bx}" y2="${by + 5}"/>
      <line x1="${bx + barPx}" y1="${by - 5}" x2="${bx + barPx}" y2="${by + 5}"/>
    </g>
    <text x="${bx + barPx / 2}" y="${by - 10}" fill="${inkCol}" font-size="${fs - 1}"
          text-anchor="middle" font-family="monospace" stroke="${halo}" stroke-width="3"
          paint-order="stroke" stroke-linejoin="round">${nice} m</text>
    <text x="${W - 22}" y="${fs + 42}" fill="${inkCol}" font-size="${fs + 10}" text-anchor="end"
          font-family="-apple-system" stroke="${halo}" stroke-width="3.5"
          paint-order="stroke" stroke-linejoin="round" opacity="${osm ? 0.85 : 1}">N ↑</text>
    <text x="${pad * 0.5}" y="${H - 14}" fill="${inkCol}" font-size="${fs - 2}"
          font-family="-apple-system" stroke="${halo}" stroke-width="3"
          paint-order="stroke" stroke-linejoin="round" opacity="${osm ? 0.9 : 0.75}">${
      d.kind === "track"
        ? escapeXml(d.shape_name) + (d.direction === "cw" ? " · 顺时针" : " · 逆时针")
        : escapeXml(d.shape_name) + (d.rotation ? ` · 旋转 ${d.rotation}°` : "")
    }</text>`;
  gOver.innerHTML = layers;

  $("scale").textContent = `画布 1px ≈ ${(1 / proj.scale).toFixed(1)} m · ${W}×${H}`;

  if (osm) {
    clearTimeout(window.__tileTimer);
    window.__tileTimer = setTimeout(checkTiles, 1800);
  }
}

function escapeXml(s) {
  return String(s == null ? "" : s).replace(/[<>&"]/g, c => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;", '"': "&quot;" }[c]));
}

/* 旋转钮里的那个「转一圈」小圆弧（纯装饰，提示这里能转） */
function rotGlyph(cx, cy) {
  const r = 3.8, a0 = -0.45 * Math.PI, a1 = 1.15 * Math.PI;
  const x0 = cx + r * Math.cos(a0), y0 = cy + r * Math.sin(a0);
  const x1 = cx + r * Math.cos(a1), y1 = cy + r * Math.sin(a1);
  return `M ${x0.toFixed(1)} ${y0.toFixed(1)} A ${r} ${r} 0 1 1 ${x1.toFixed(1)} ${y1.toFixed(1)}`;
}

function checkTiles() {
  if (S.basemap !== "osm") return;
  const bad = window.__tileFail || 0, good = window.__tileOk || 0;
  const el = $("tileWarn");
  if (!bad) { el.classList.add("hidden"); return; }
  if (!good) {
    el.textContent = "真实地图取不到瓦片（网络不通或代理拦截），已切回极简底图";
    el.classList.remove("hidden");
    setBasemap("minimal");
    return;
  }
  el.textContent = `真实地图有 ${bad} 张瓦片没取到（网络不稳），其余正常`;
  el.classList.remove("hidden");
}

let ro = null;
function watchResize() {
  if (ro) return;
  const wrap = $("canvasWrap");
  let t = null;
  ro = new ResizeObserver(() => { clearTimeout(t); t = setTimeout(drawMap, 120); });
  ro.observe(wrap);
}

/* ---------------------------------------------------------------- 统计行 */
function renderStats(d) {
  if (d.kind === "track") {
    const total = d.length_m * S.laps;
    const est = total / (1000 / S.pace);
    $("stats").innerHTML = `
      <span><span class="k">单圈</span><b>${Math.round(d.length_m)}</b> m</span>
      <span><span class="k">圈数</span><b>${S.laps}</b></span>
      <span><span class="k">总里程</span><b>${(total / 1000).toFixed(2)}</b> km</span>
      <span><span class="k">预估用时</span><b>${fmtHM(est)}</b></span>
      <span><span class="k">方向</span><b>${d.direction === "cw" ? "顺时针" : "逆时针"}</b></span>
      <span class="muted">${escapeXml(d.shape_name)}</span>`;
    return;
  }
  const total = d.length_m * S.laps;
  const est = total / (1000 / S.pace);
  const lv = d.level || (d.margin_m > 0 ? "ok" : "warn");
  const over = d.over_m || 0;
  const mItem = lv === "ok"
    ? `<span><span class="k">离边界</span><b class="${d.margin_m >= Math.max(10, S.margin * 0.9) ? "good" : "warn"}">${d.margin_m.toFixed(0)}</b> m</span>`
    : `<span><span class="k">越界</span><b class="${lv}">${over >= 1000
        ? (over / 1000).toFixed(2) + " km" : Math.round(over) + " m"}</b></span>`;
  $("stats").innerHTML = `
    <span><span class="k">轨迹长度</span><b>${(total / 1000).toFixed(2)}</b> km</span>
    <span><span class="k">预估用时</span><b>${fmtHM(est)}</b></span>
    ${mItem}
    <span><span class="k">顶点</span><b>${d.track.length}</b></span>
    <span><span class="k">校园面积</span><b>${d.area_km2.toFixed(2)}</b> km²</span>`;

  // 画布上的越界警示条：≤1 km 黄、>1 km 红（跟 stats 同一套 level）
  const bw = $("boundWarn");
  if (bw) {
    if (lv === "ok") {
      bw.classList.add("hidden");
    } else {
      bw.textContent = d.note || (lv === "danger" ? "图形越过校界超过 1 km" : "图形越过校界");
      bw.className = "boundwarn " + lv;
    }
  }

  $("placeHint").innerHTML = lv !== "ok"
    ? `<span class="lv-${lv}">${escapeXml(d.note || "已越过校界")}</span>`
    : (d.manual
        ? `已手动摆位，离边界最小 <b>${d.margin_m.toFixed(0)} m</b> <span class="muted">（点「重置位置与角度」回到自动最优）</span>`
        : `已放在 <code>${d.center[0].toFixed(5)}, ${d.center[1].toFixed(5)}</code>，离边界最小 <b>${d.margin_m.toFixed(0)} m</b>`);
}

/* ---------------------------------------------------------------- 预览 */
let pending = null;
function queue() { clearTimeout(pending); pending = setTimeout(refresh, 220); }

function previewParams() {
  if (S.mode === "track") {
    return S.track ? { track: S.track, direction: S.direction } : null;
  }
  return { shape: S.shape, boundary: S.boundary, size: S.size, margin: S.margin, quality: "fast" };
}

async function refresh() {
  const p = previewParams();
  if (!p) {
    // 这个校区没有内置跑道：清干净画布，别留一块空的 OSM 底和上一轮的标注
    lastData = null;
    baseTrack = null;
    baseCenter = null;
    $("map").dataset.ready = "";
    $("map").innerHTML = "";
    $("tileWarn").classList.add("hidden");
    $("boundWarn").classList.add("hidden");
    $("emptyState").classList.remove("hidden");
    syncBasemapChrome();
    $("stats").innerHTML = '<span class="muted">这个校区还没有收录跑道 —— 切到「图形轨迹」画一个吧</span>';
    return;
  }
  $("emptyState").classList.add("hidden");
  $("busy").classList.remove("hidden");
  try {
    const d = await api("/api/preview", p);
    // 原始落点单独留一份：拖动时以它为基准，避免平移量被累加两遍
    baseTrack = d.track;
    baseCenter = d.center;
    lastData = applyManip(d);
    lastProj = null;              // 重新投影前先作废，免得 resize 间隙用旧投影
    syncBasemapChrome();
    syncPan();
    drawMap();
    renderStats(lastData);
  } catch (e) {
    $("stats").innerHTML = `<span class="warn">预览失败：${e.message}</span>`;
  } finally {
    $("busy").classList.add("hidden");
  }
}

/* ---------------------------------------------------------------- 生成 */
async function doGenerate() {
  if (S.mode === "track" && !S.track) return alert("这个校区还没有收录跑道。\n\n可以先切到「图形轨迹」，或换一个校区。");
  const btn = $("btnGen");
  btn.disabled = true;
  $("genHint").textContent = S.mode === "track"
    ? "正在生成（沿跑道连圈 + 补时间戳）…"
    : "正在生成（补时间戳 + 越界校验）…";
  try {
    const base = {
      laps: S.laps, pace: fmtPace(S.pace),
      pace_ramp: S.rampOn ? `${fmtPace(S.rampA)},${fmtPace(S.rampB)}` : null,
      out_name: $("outName").value.trim() || null,
      jitter: S.jitter,              // GPS 漂移（米）
      pace_noise: S.noise / 100,     // 配速波动（0~0.3）
    };
    const p = S.mode === "track"
      ? Object.assign({ track: S.track, direction: S.direction }, base)
      : Object.assign({ shape: S.shape, boundary: S.boundary, size: S.size,
                        margin: S.margin, laps_mode: S.lapsMode,
                        // 手动摆位过就按这点列出 GPX（所见即所得）；没摆过交给后端搜最优落点
                        points: (hasManip() && lastData) ? lastData.track : null }, base);
    const r = await api("/api/generate", p);
    let line = `已生成 <b>${r.file}</b> · ${fmtM(r.length_m)} · ${r.duration_hms}`;
    if (r.kind === "track") {
      line += ` · ${r.laps} 圈 × ${Math.round(r.loop_m)} m · ${r.direction === "cw" ? "顺时针" : "逆时针"}`;
    } else {
      const c = r.check;
      line += ` · 校验 ${c.out_of_bounds === 0
        ? `<span style="color:#25c06b">全部在界内</span>`
        : `<span style="color:#e0464b">有 ${c.out_of_bounds} 点在界外</span>`}`;
    }
    if (r.warning) {
      const col = r.level === "danger" ? "#e0464b" : "#e8a13a";
      const mark = r.level === "danger" ? "⛔" : "⚠";
      line += `<br><span style="color:${col}">${mark} ${escapeXml(r.warning)}</span>`;
    }
    $("genHint").innerHTML = line;
    await loadRoutes(r.file);
  } catch (e) {
    $("genHint").innerHTML = `<span style="color:#e0464b">生成失败：${e.message}</span>`;
  } finally {
    btn.disabled = false;
  }
}

/* ---------------------------------------------------------------- 路线列表 */
function selectedRoute() { return $("routeSel").value || ""; }

async function loadRoutes(select) {
  const { routes } = await api("/api/routes");
  const sel = $("routeSel");
  const want = select || sel.value || store.get(K.route, "") || "";
  sel.innerHTML = "";

  // 占位项：绝不再「静默默认第一条」——那正是「以为选了 A、实际跑了 B」的温床
  const ph = document.createElement("option");
  ph.value = "";
  ph.textContent = "— 请选一条路线 —";
  sel.appendChild(ph);

  routes.forEach(r => {
    const o = document.createElement("option");
    o.value = r.file;
    o.textContent = `${r.file}${r.length_m ? ` · ${(r.length_m / 1000).toFixed(2)}km` : ""}${r.duration_hms ? ` · ${r.duration_hms}` : ""}`;
    sel.appendChild(o);
  });

  const hit = routes.some(r => r.file === want) ? want : "";
  sel.value = hit;
  store.set(K.route, hit);
  syncDown();
  renderTarget();
}

/* 「将要跑的路线」：让"选了哪条"和"跑了哪条"不可能对不上 */
function renderTarget() {
  const f = selectedRoute();
  const tn = $("targetName");
  if (tn) {
    tn.textContent = f || "未选择（先在上方挑一条）";
    tn.classList.toggle("muted", !f);
  }
  const note = $("targetNote");
  if (!note) return;
  const busyFile = runningFile || (playState.external && playState.external.file) || null;
  if (busyFile && f && f !== busyFile) {
    note.innerHTML = `⚠ 现在跑的是 <b>${busyFile}</b> —— 先点 <b>④ 结束模拟</b>，才能换成这条`;
    note.className = "tw warn";
  } else if (busyFile) {
    note.innerHTML = `正在跑 <b>${busyFile}</b>`;
    note.className = "tw";
  } else if (!f) {
    note.textContent = "选一条，再点 ②";
    note.className = "tw";
  } else {
    note.textContent = "";
    note.className = "tw";
  }
}

function syncDown() {
  const f = $("routeSel").value;
  const a = $("btnDown");
  a.href = `/api/download?file=${encodeURIComponent(f)}`;
  a.classList.toggle("disabled", !f);
}

/* ---------------------------------------------------------------- 设备 */
async function loadDevice() {
  const el = $("dev");
  try {
    // 这里不能用 api()：没插设备时 /api/device **正常返回 200**，
    // 只是带了个 error 字段说明原因，而 api() 见到 error 就当失败抛异常
    // —— 那样会永远走到「服务没起来」那支，把「未发现设备」的提示吞掉。
    const res = await fetch("/api/device");
    const d = await res.json().catch(() => ({ error: "响应不是 JSON" }));
    if (!res.ok) throw new Error(d.error || `HTTP ${res.status}`);
    S.device = !!d.connected;
    if (d.connected) {
      el.className = "dev ok";
      $("devtext").textContent = `${d.name} · iOS ${d.ios} · ${d.conn}`;
    } else {
      el.className = "dev bad";
      $("devtext").textContent = d.error || "设备未连接";
    }
  } catch (e) {
    S.device = false;
    el.className = "dev bad";
    $("devtext").textContent = "设备查询失败（服务没起来？）";
  }
  syncButtons();
}

/* ---------------------------------------------------------------- 模拟控制 */
/* 「暂停」的坑：网页暂停只冻住轨迹注入，手机端运动 App 照旧在计时。
   静止那段时间会被算进总时长 → 平均配速被拖下来 → 配速不达标。
   所以暂停键必须自带这条提醒，且暂停期间高亮。 */
const PAUSE_HINT_IDLE =
  "<b>③ 暂停</b> 只冻结轨迹注入 —— 手机端运动 App 不会跟着停，" +
  "请手动也点暂停；否则静止这段时间会计进总时长，平均配速直接掉下来（配速不达标）。继续时也一起恢复。";
const PAUSE_HINT_ON =
  "<b>已暂停 ·</b> 确认一下手机端运动 App 也已暂停，不然配速不达标；点「继续」后记得在手机端一起恢复。";

function setProg(txt, cls) {
  const el = $("progText");
  el.textContent = txt;
  el.className = cls || "";
}

function renderPauseHint() {
  const el = $("pauseHint");
  if (!el) return;
  const on = !!playState.running && !!playState.paused;
  el.innerHTML = on ? PAUSE_HINT_ON : PAUSE_HINT_IDLE;
  el.classList.toggle("on", on);
}

/* 锁住按钮但**不用原生 disabled**：原生 disabled 会把点击彻底吞掉（连 alert 都没有），
   于是「选了新路线 → 点② → 毫无反应 → 手机上还在跑旧的」看起来就像「路线选择失效」。
   改成 .locked：视觉上变淡，但点下去一定会告诉你为什么不行。 */
function lockWhy(el, reason, title) {
  el.disabled = false;              // 兜底：HTML 里若残留 disabled，这里一并抹掉
  el.classList.toggle("locked", !!reason);
  el.dataset.lock = reason || "";
  if (title) el.title = title;
}

function busyFileNow() {
  return runningFile || (playState.external && playState.external.file) || "";
}

function syncButtons() {
  const running = playState.running;
  const paused = playState.paused;
  const ext = playState.external;
  const busy = running || !!ext;                    // 有会话在跑（本页或外部）
  const dev = S.device;
  const cur = busyFileNow() || "（未知）";

  lockWhy($("btnPrime"), !dev ? "dev" : (busy ? "busy" : ""),
    !dev ? "没检测到设备：插好数据线、解锁并信任本机"
         : busy ? `现在正跑着《${cur}》，先点 ④ 结束模拟`
                : "把手机定位钉到所选路线的起点，并一直保持");
  lockWhy($("btnPlay"), !dev ? "dev" : (busy ? "busy" : ""),
    !dev ? "没检测到设备：插好数据线、解锁并信任本机"
         : busy ? `现在正跑着《${cur}》，先点 ④ 结束模拟`
                : "按所选路线把轨迹回放到手机");
  lockWhy($("btnPause"), !dev ? "dev" : (!running ? "idle" : ""),
    !dev ? "没检测到设备"
         : !running ? "当前没有在跑的回放"
                    : paused ? "解冻注入，从断点接着跑。回到手机端把运动 App 也一起恢复"
                             : "冻结轨迹注入，不丢进度。注意：手机端运动 App 不会跟着停，请手动也暂停记录");
  lockWhy($("btnRelease"), !dev ? "dev" : "",
    !dev ? "没检测到设备" : "停回放 + 释放定点；若手机仍停在旧位置，需拔一次数据线");

  const pb = $("btnPause");
  if (paused) {
    pb.innerHTML = "<b>③</b>继续";
    pb.classList.add("on");
  } else {
    pb.innerHTML = "<b>③</b>暂停";
    pb.classList.remove("on");
  }
  renderPauseHint();
  renderTarget();
}

/* 起播/注入前的统一检查：任何一条不满足都要**说清楚原因**，绝不静默失败 */
function preflight(action) {
  const f = selectedRoute();
  if (!S.device) return { msg: "没检测到设备。\n\n插好数据线、解锁手机并在弹窗里点「信任」，再试。" };
  if (playState.running || playState.external) {
    const cur = busyFileNow() || "（未知）";
    return { msg: `现在正跑着《${cur}》，不能同时再起一条。\n\n` +
                  `· 想继续跑它 → 点 ③ 继续\n· 想换路线 → 先点 ④ 结束模拟，再选新路线点 ②\n\n` +
                  (f && f !== cur ? `（你刚选的《${f}》要等结束后才能跑）` : "") };
  }
  if (!f) return { msg: `还没选路线。\n\n先在下方「已生成的路线」里挑一条，再点 ${action}。` };
  return { f };
}

async function doPrime() {
  const chk = preflight("① 注入初始位置");
  if (chk.msg) return alert(chk.msg);
  const f = chk.f;
  const btn = $("btnPrime");
  btn.classList.add("locked");
  try {
    const r = await api("/api/prime", { file: f });
    setProg(`已注入初始位置 ${r.lat.toFixed(6)}, ${r.lon.toFixed(6)}（点「开始模拟」继续）`);
    alert(`已把手机定位钉到《${f}》的起点：\n${r.lat.toFixed(6)}, ${r.lon.toFixed(6)}\n\n位置会一直保持，直到你点「结束模拟」。`);
  } catch (e) {
    alert("注入失败：" + e.message);
  } finally {
    syncButtons();
  }
}

async function doGoto() {
  const la = parseFloat($("cusLat").value), lo = parseFloat($("cusLon").value);
  if (!isFinite(la) || !isFinite(lo)) return alert("请填合法的纬度、经度（十进制度）");
  if (la < -90 || la > 90 || lo < -180 || lo > 180) return alert("经纬度超出范围");
  try {
    const r = await api("/api/prime", { lat: la, lon: lo });
    setProg(`已定点到 ${r.lat.toFixed(6)}, ${r.lon.toFixed(6)}`);
  } catch (e) {
    alert("定点失败：" + e.message);
  }
}

async function doPlay() {
  const chk = preflight("② 开始模拟");
  if (chk.msg) return alert(chk.msg);
  const f = chk.f;
  if (!confirm(`开始模拟《${f}》？\n\n回放期间别拔线。中途要停就点「暂停」，或点「结束模拟」。`)) return;
  const btn = $("btnPlay");
  btn.classList.add("locked");
  try {
    const r = await api("/api/play", { file: f });
    runningFile = f;
    playState = { running: true, paused: false, external: null };
    setProg(`回放中：${f} · 预计 ${r.duration_hms}`, "live");
    startPoll();
  } catch (e) {
    alert("起播失败：" + e.message);
  } finally {
    syncButtons();
  }
}

async function doPause() {
  if (!playState.running) return alert("当前没有在跑的回放，没什么可暂停的。");
  const btn = $("btnPause");
  btn.classList.add("locked");
  try {
    if (playState.paused) {
      await api("/api/resume", {});
      setProg("已继续 · 记得把手机端运动记录也恢复", "live");
    } else {
      const r = await api("/api/pause", {});
      setProg(`已暂停（冻结 ${r.frozen} 个注入进程）· 手机端也要暂停记录，否则配速不达标`, "pause");
    }
  } catch (e) {
    alert("操作失败：" + e.message);
  } finally {
    tick();
  }
}

async function doRelease() {
  if (!S.device) return alert("没检测到设备。插好数据线、解锁手机后再试。");
  if (!confirm("结束模拟并恢复定位？\n\n会停掉回放、释放定点，并向设备发送「停止模拟定位」。\n注意：这条指令没有回执，若手机地图还停在旧位置，需要拔一次数据线。")) return;
  const btn = $("btnRelease");
  btn.classList.add("locked");
  setProg("正在结束模拟…");
  try {
    const r = await api("/api/release", {});
    runningFile = null;
    playState = { running: false, paused: false, external: null };
    setProg(r.messages.join(" · "));
    $("bar").style.width = "0%";
  } catch (e) {
    alert("结束失败：" + e.message);
  } finally {
    await loadDevice();
    tick();
  }
}

/* 常驻轮询：也顺带发现命令行另起的会话 */
function startPoll() {
  if (pollTimer) return;
  pollTimer = setInterval(tick, 2500);
}

async function tick() {
  try {
    const s = await api("/api/status");
    playState = { running: !!s.running, paused: !!s.paused, external: s.external || null };
    runningFile = s.running ? s.gpx : null;
    if (s.running) {
      $("bar").style.width = (s.progress * 100).toFixed(1) + "%";
      const pct = (s.progress * 100).toFixed(0);
      setProg(
        `${s.gpx} · ${pct}%（${s.pushed}/${s.total} 点）· 已跑 ${s.elapsed_hms} / ${s.expected_hms}` +
        (s.paused ? " · 已暂停（手机端记得也暂停记录）" : ""),
        s.paused ? "pause" : "live");
    } else if (s.external) {
      $("bar").style.width = "0%";
      setProg(`检测到命令行回放：${s.external.file}（预计 ${s.external.duration_hms}）· 本页不接管它`);
    } else {
      $("bar").style.width = "0%";
      if (!$("progText").textContent.startsWith("已")) setProg("未在回放");
    }
    syncButtons();
  } catch (e) { /* 服务没起来就静默 */ }
}

/* ---------------------------------------------------------------- 绑定 */
function bind() {
  document.querySelectorAll("#modeSeg button").forEach(b => { b.onclick = () => setMode(b.dataset.v); });
  document.querySelectorAll("#dirSeg button").forEach(b => {
    b.onclick = () => {
      S.direction = b.dataset.v;
      document.querySelectorAll("#dirSeg button").forEach(x => x.classList.toggle("on", x === b));
      queue();
    };
  });

  $("size").oninput = e => { S.size = +e.target.value; $("sizeOut").textContent = S.size + " m"; queue(); };
  $("margin").oninput = e => { S.margin = +e.target.value; $("marginOut").textContent = S.margin + " m"; queue(); };
  $("laps").oninput = e => {
    S.laps = +e.target.value;
    $("lapsOut").textContent = S.laps + " 圈";
    if (lastData) renderStats(lastData);
  };
  $("pace").oninput = e => {
    S.pace = +e.target.value;
    $("paceOut").textContent = fmtPace(S.pace) + " /km";
    if (lastData) renderStats(lastData);
  };
  $("rampOn").onchange = e => {
    S.rampOn = e.target.checked;
    $("rampBox").classList.toggle("hidden", !S.rampOn);
  };
  $("rampA").oninput = e => { S.rampA = +e.target.value; $("rampAOut").textContent = fmtPace(S.rampA); };
  $("rampB").oninput = e => { S.rampB = +e.target.value; $("rampBOut").textContent = fmtPace(S.rampB); };
  $("noise").oninput = e => {
    S.noise = +e.target.value;
    $("noiseOut").textContent = "±" + S.noise + "%";
  };
  $("jitter").oninput = e => {
    S.jitter = +e.target.value;
    $("jitterOut").textContent = S.jitter.toFixed(1) + " m";
  };
  $("btnReset").onclick = resetManip;
  $("trackSel").onchange = () => { S.track = $("trackSel").value; store.set(K.track, S.track); syncTrackLen(); refresh(); };

  // 拖动摆位：事件绑在 SVG 根上（overlay 每次重画都会重建，绑在子元素上会丢）
  const svg = $("map");
  svg.addEventListener("pointerdown", onDragDown);
  svg.addEventListener("pointermove", onDragMove);
  svg.addEventListener("pointerup", onDragUp);
  svg.addEventListener("pointercancel", onDragUp);
  svg.addEventListener("lostpointercapture", onDragUp);

  document.querySelectorAll("#lapsMode button").forEach(b => {
    b.onclick = () => {
      S.lapsMode = b.dataset.v;
      document.querySelectorAll("#lapsMode button").forEach(x => x.classList.toggle("on", x === b));
    };
  });

  $("btnGen").onclick = doGenerate;
  $("btnPrime").onclick = doPrime;
  $("btnGoto").onclick = doGoto;
  $("btnPlay").onclick = doPlay;
  $("btnPause").onclick = doPause;
  $("btnRelease").onclick = doRelease;
  $("btnRefresh").onclick = () => loadRoutes();
  $("btnMap").onclick = toggleBasemap;
  $("routeSel").onchange = () => {
    syncDown();
    store.set(K.route, selectedRoute());
    renderTarget();
  };
  $("btnTheme").onclick = toggleTheme;
}

(async function init() {
  // 首帧的主题已由 <head> 里的内联脚本定好，这里只是把状态接过来
  S.theme = document.documentElement.dataset.theme || "light";
  if (mqDark && !store.get(K.theme, "")) {
    mqDark.addEventListener("change", e => applyTheme(e.matches ? "dark" : "light", false));
  }
  S.mode = store.get(K.mode, "shape") === "track" ? "track" : "shape";
  S.basemap = store.get(K.basemap, "minimal") === "osm" ? "osm" : "minimal";
  S.track = store.get(K.track, "");

  // 滑块显示跟 S 对齐（HTML 里写死的 value 只作首帧兜底）
  $("size").value = S.size; $("sizeOut").textContent = S.size + " m";
  $("margin").value = S.margin; $("marginOut").textContent = S.margin + " m";
  $("pace").value = S.pace; $("paceOut").textContent = fmtPace(S.pace) + " /km";
  $("noise").value = S.noise; $("noiseOut").textContent = "±" + S.noise + "%";
  $("jitter").value = S.jitter; $("jitterOut").textContent = S.jitter.toFixed(1) + " m";

  bind();
  watchResize();
  setBasemap(S.basemap);
  await Promise.all([loadShapes(), loadBoundaries(), loadTracks(), loadDevice(), loadRoutes()]);
  applyMode();
  await refresh();
  setInterval(loadDevice, 12000);
  startPoll();
})();
