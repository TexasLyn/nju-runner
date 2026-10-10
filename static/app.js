/* NJU Runner · 前端交互
   状态 → 防抖预览 → 按容器尺寸画 SVG；生成 / 注入 / 起播 / 暂停 / 结束 走 REST。
   主题：localStorage 记忆；没存过就跟随系统（prefers-color-scheme）。 */

const $ = (id) => document.getElementById(id);

const S = {
  shape: "heart",
  boundary: "boundary-nju-xianlin.geojson",
  size: 560,
  margin: 60,
  laps: 1,
  lapsMode: "retrace",
  pace: 360,          // 秒/km
  rampOn: false,
  rampA: 450,
  rampB: 270,
  device: false,
  theme: "light",
};

let shapesMeta = {};
let pollTimer = null;
let lastData = null;      // 最近一次预览结果，供 resize 重绘
let playState = { running: false, paused: false, external: null };
let runningFile = null;   // 服务器上正在跑的文件名（tick 刷新）
const ROUTE_KEY = "njurunner.route";

const fmtPace = (s) => `${Math.floor(s / 60)}'${String(s % 60).padStart(2, "0")}"`;
const fmtM = (m) => m >= 1000 ? (m / 1000).toFixed(2) + " km" : Math.round(m) + " m";
const fmtHM = (sec) => {
  sec = Math.round(sec || 0);
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return h ? `${h}时${m}分` : (m ? `${m}分${String(s).padStart(2, "0")}秒` : `${s}秒`);
};
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));

/* ---------------------------------------------------------------- 主题 */
const THEME_KEY = "njurunner.theme";
const mqDark = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;

function storedTheme() {
  try { return localStorage.getItem(THEME_KEY); } catch (e) { return null; }
}

function applyTheme(t, persist) {
  document.documentElement.dataset.theme = t;
  S.theme = t;
  if (persist) { try { localStorage.setItem(THEME_KEY, t); } catch (e) { /* 隐私模式 */ } }
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

/* ---------------------------------------------------------------- 图形按钮 */
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

/* ---------------------------------------------------------------- 投影与绘制 */
function makeProj(boundary, W, H, pad) {
  const M = 111320;
  const lats = boundary.map(p => p[0]), lons = boundary.map(p => p[1]);
  const la0 = (Math.min(...lats) + Math.max(...lats)) / 2;
  const lo0 = (Math.min(...lons) + Math.max(...lons)) / 2;
  const k = Math.cos(la0 * Math.PI / 180);
  const toXY = ([la, lo]) => [(lo - lo0) * k * M, (la - la0) * M];
  const xy = boundary.map(toXY);
  const xs = xy.map(p => p[0]), ys = xy.map(p => p[1]);
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  const y0 = Math.min(...ys), y1 = Math.max(...ys);
  const sc = Math.min((W - 2 * pad) / (x1 - x0), (H - 2 * pad) / (y1 - y0));
  const cx = (x0 + x1) / 2, cy = (y0 + y1) / 2;
  const f = ([la, lo]) => {
    const [x, y] = toXY([la, lo]);
    return [W / 2 + (x - cx) * sc, H / 2 - (y - cy) * sc];
  };
  f.scale = sc;
  return f;
}

function drawMap() {
  if (!lastData) return;
  const d = lastData;
  const wrap = $("canvasWrap");
  const rect = wrap.getBoundingClientRect();
  // 布局未完成时 clientWidth/Height 可能是 0，退回 rect，再退回一个合理默认值；
  // 真正的尺寸一旦确定，ResizeObserver 会再触发重绘。
  const W = Math.max(360, Math.round(wrap.clientWidth || rect.width || 900));
  const H = Math.max(260, Math.round(wrap.clientHeight || rect.height || 620));
  const pad = clamp(Math.min(W, H) * 0.09, 34, 76);

  const svg = $("map");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);

  const proj = makeProj(d.boundary, W, H, pad);
  const path = (pts) => pts.map(p => {
    const [x, y] = proj(p);
    return x.toFixed(1) + "," + y.toFixed(1);
  }).join(" ");

  const bp = path(d.boundary);
  const tp = path(d.track);
  const [sx, sy] = proj(d.track[0]);
  const [ex, ey] = proj(d.track[d.track.length - 1]);
  const [clx, cly] = proj(d.center);
  const sameEnds = Math.abs(sx - ex) < 1.5 && Math.abs(sy - ey) < 1.5;

  // 网格
  const step = Math.round(clamp(Math.min(W, H) / 8, 46, 96));
  let grid = "";
  for (let x = 0; x <= W; x += step) grid += `<line x1="${x}" y1="0" x2="${x}" y2="${H}"/>`;
  for (let y = 0; y <= H; y += step) grid += `<line x1="0" y1="${y}" x2="${W}" y2="${y}"/>`;

  // 比例尺
  const nice = [50, 100, 200, 250, 500, 1000, 2000].find(v => v >= 150 / proj.scale) || 2000;
  const barPx = nice * proj.scale;
  const bx = W - 34 - barPx, by = H - 30;
  const fs = clamp(Math.min(W, H) / 52, 11, 16);
  const sw = clamp(Math.min(W, H) / 190, 2.2, 4.4);          // 轨迹线宽
  const bw = clamp(Math.min(W, H) / 400, 1.4, 2.4);          // 边界线宽

  const trackColor = d.margin_m > 0 ? "url(#tg)" : "#e0464b";

  svg.innerHTML = `
    <defs>
      <pattern id="g" width="${step}" height="${step}" patternUnits="userSpaceOnUse">
        <path d="M ${step} 0 L 0 0 0 ${step}" fill="none" stroke="#1a2029" stroke-width="1"/>
      </pattern>
      <linearGradient id="tg" x1="0" y1="0" x2="1" y2="0">
        <stop offset="0" stop-color="#ffb457"/><stop offset="1" stop-color="#ff7a45"/>
      </linearGradient>
    </defs>
    <rect width="${W}" height="${H}" fill="#0f1218"/>
    <rect width="${W}" height="${H}" fill="url(#g)"/>
    <polygon points="${bp}" fill="#16283c" stroke="#5f9ce0" stroke-width="${bw}"
             stroke-linejoin="round" opacity="0.95"/>
    <polyline points="${tp}" fill="none" stroke="${trackColor}" stroke-width="${sw}"
              stroke-linejoin="round" stroke-linecap="round"/>
    <circle cx="${clx.toFixed(1)}" cy="${cly.toFixed(1)}" r="3" fill="#ffffff" opacity="0.28"/>
    ${sameEnds ? "" : `<circle cx="${ex.toFixed(1)}" cy="${ey.toFixed(1)}" r="${sw * 2}"
              fill="#e0464b" stroke="#0f1218" stroke-width="2.5"/>`}
    <circle cx="${sx.toFixed(1)}" cy="${sy.toFixed(1)}" r="${sw * 2}" fill="#25c06b"
            stroke="#0f1218" stroke-width="2.5"/>
    <g stroke="#8ea4bf" stroke-width="1.8">
      <line x1="${bx}" y1="${by}" x2="${bx + barPx}" y2="${by}"/>
      <line x1="${bx}" y1="${by - 5}" x2="${bx}" y2="${by + 5}"/>
      <line x1="${bx + barPx}" y1="${by - 5}" x2="${bx + barPx}" y2="${by + 5}"/>
    </g>
    <text x="${bx + barPx / 2}" y="${by - 10}" fill="#8ea4bf" font-size="${fs - 1}"
          text-anchor="middle" font-family="monospace">${nice} m</text>
    <text x="${W - 22}" y="${fs + 26}" fill="#7c8ea6" font-size="${fs + 10}" text-anchor="end"
          font-family="-apple-system">N ↑</text>
    <text x="${pad * 0.5}" y="${H - 14}" fill="#435063" font-size="${fs - 2}"
          font-family="-apple-system">${d.shape_name}${d.rotation ? ` · 旋转 ${d.rotation}°` : ""}</text>
  `;
  $("scale").textContent = `画布 1px ≈ ${(1 / proj.scale).toFixed(1)} m · ${W}×${H}`;
}

let ro = null;
function watchResize() {
  if (ro) return;
  const wrap = $("canvasWrap");
  let t = null;
  ro = new ResizeObserver(() => {
    clearTimeout(t);
    t = setTimeout(drawMap, 100);
  });
  ro.observe(wrap);
}

function renderStats(d) {
  const total = d.length_m * S.laps;
  const est = total / (1000 / S.pace);
  const mOk = d.margin_m >= Math.max(10, S.margin * 0.9);
  $("stats").innerHTML = `
    <span><span class="k">轨迹长度</span><b>${(total / 1000).toFixed(2)}</b> km</span>
    <span><span class="k">预估用时</span><b>${fmtHM(est)}</b></span>
    <span><span class="k">离边界</span><b class="${mOk ? "good" : "warn"}">${d.margin_m.toFixed(0)}</b> m</span>
    <span><span class="k">顶点</span><b>${d.track.length}</b></span>
    <span><span class="k">校园面积</span><b>${d.area_km2.toFixed(2)}</b> km²</span>
  `;
  $("placeHint").innerHTML = d.ok
    ? `已放在 <code>${d.center[0].toFixed(5)}, ${d.center[1].toFixed(5)}</code>，离边界最小 <b>${d.margin_m.toFixed(0)} m</b>`
    : `<span style="color:#e0464b">放不下，把「图形大小」调小</span>`;
}

let pending = null;
function queue() {
  clearTimeout(pending);
  pending = setTimeout(refresh, 220);
}

async function refresh() {
  $("busy").classList.remove("hidden");
  try {
    const d = await api("/api/preview", {
      shape: S.shape, boundary: S.boundary,
      size: S.size, margin: S.margin,
    });
    lastData = d;
    drawMap();
    renderStats(d);
  } catch (e) {
    $("stats").innerHTML = `<span class="warn">预览失败：${e.message}</span>`;
  } finally {
    $("busy").classList.add("hidden");
  }
}

/* ---------------------------------------------------------------- 生成 */
async function doGenerate() {
  const btn = $("btnGen");
  btn.disabled = true;
  $("genHint").textContent = "正在生成（补时间戳 + 越界校验）…";
  try {
    const r = await api("/api/generate", {
      shape: S.shape, boundary: S.boundary, size: S.size, margin: S.margin,
      pace: fmtPace(S.pace), laps: S.laps, laps_mode: S.lapsMode,
      pace_ramp: S.rampOn ? `${fmtPace(S.rampA)},${fmtPace(S.rampB)}` : null,
      out_name: $("outName").value.trim() || null,
    });
    const c = r.check;
    $("genHint").innerHTML =
      `已生成 <b>${r.file}</b> · ${fmtM(r.length_m)} · ${r.duration_hms} · ` +
      `校验 ${c.out_of_bounds === 0 ? `<span style="color:#25c06b">全部在界内</span>` : `<span style="color:#e0464b">有 ${c.out_of_bounds} 点越界</span>`}`;
    await loadRoutes(r.file);
  } catch (e) {
    $("genHint").innerHTML = `<span style="color:#e0464b">生成失败：${e.message}</span>`;
  } finally {
    btn.disabled = false;
  }
}

/* ---------------------------------------------------------------- 路线列表 */
function selectedRoute() { return $("routeSel").value || ""; }

function storedRoute() {
  try { return localStorage.getItem(ROUTE_KEY) || ""; } catch (e) { return ""; }
}

function rememberRoute(f) {
  try { f ? localStorage.setItem(ROUTE_KEY, f) : localStorage.removeItem(ROUTE_KEY); } catch (e) {}
}

async function loadRoutes(select) {
  const { routes } = await api("/api/routes");
  const sel = $("routeSel");
  // 想在重建列表后继续选中原来那条：显式传入 > 当前选中 > 上次记住的
  const want = select || sel.value || storedRoute() || "";
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
  rememberRoute(hit);
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
    const d = await api("/api/device");
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
  if (!f) return { msg: `还没选路线。\n\n先在上方的「已生成的路线」里挑一条，再点 ${action}。` };
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

function stopPoll() { if (pollTimer) clearInterval(pollTimer); pollTimer = null; }

/* ---------------------------------------------------------------- 绑定 */
function bind() {
  $("size").oninput = e => { S.size = +e.target.value; $("sizeOut").textContent = S.size + " m"; queue(); };
  $("margin").oninput = e => { S.margin = +e.target.value; $("marginOut").textContent = S.margin + " m"; queue(); };
  $("laps").oninput = e => {
    S.laps = +e.target.value;
    $("lapsOut").textContent = S.laps + " 圈";
    queue();
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
  $("routeSel").onchange = () => {
    syncDown();
    rememberRoute(selectedRoute());
    renderTarget();
  };
  $("btnTheme").onclick = toggleTheme;
}

(async function init() {
  // 首帧的主题已由 <head> 里的内联脚本定好，这里只是把状态接过来
  S.theme = document.documentElement.dataset.theme || "light";
  if (mqDark && !storedTheme()) {
    mqDark.addEventListener("change", e => applyTheme(e.matches ? "dark" : "light", false));
  }
  bind();
  watchResize();
  await Promise.all([loadShapes(), loadDevice(), loadRoutes()]);
  await refresh();
  setInterval(loadDevice, 12000);
  startPoll();
})();
