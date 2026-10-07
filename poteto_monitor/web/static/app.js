"use strict";

const $ = (sel) => document.querySelector(sel);
const SPARK_RANGE = 86400; // 秒。カードのスパークラインに出す期間

const cards = new Map();      // key -> card element
const series = new Map();     // key -> [UNIX 秒, 値][]（直近 24 時間）
const rowEntries = new WeakMap(); // 設定行の要素 -> 読み込んだ元の watch エントリ
const BADGE_LABEL = { crypto: "crypto", forex: "forex", hyperliquid: "HL", ratio: "rate" };
const TPL = { crypto: "#tpl-crypto", forex: "#tpl-forex", hyperliquid: "#tpl-hyperliquid", ratio: "#tpl-ratio" };

// ── 認証（HttpOnly Cookie。トークンはブラウザに保存しない）──
let loginWaiters = [];
function requestLogin(message) {
  $("#login-error").textContent = message || "";
  $("#login-error").classList.toggle("hidden", !message);
  $("#login").classList.remove("hidden");
  $("#login-token").value = "";
  $("#login-token").focus();
  return new Promise((resolve) => loginWaiters.push(resolve));
}
function finishLogin(ok) {
  $("#login").classList.add("hidden");
  const waiters = loginWaiters; loginWaiters = [];
  waiters.forEach((w) => w(ok));
}
async function submitLogin() {
  const res = await fetch("/api/login", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token: $("#login-token").value }),
  }).catch(() => null);
  if (res && res.ok) { finishLogin(true); return; }
  const err = res ? await res.json().catch(() => ({})) : {};
  $("#login-error").textContent = (err.detail || "ログインできませんでした");
  $("#login-error").classList.remove("hidden");
}
async function login(token) {
  const res = await fetch("/api/login", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token }),
  });
  return res.ok;
}

async function authFetch(url, opts = {}) {
  let res = await fetch(url, opts);
  if (res.status === 401 && await requestLogin()) res = await fetch(url, opts);
  if (res.status === 403) {
    const err = await res.clone().json().catch(() => ({}));
    showBanner(err.detail || "この操作は許可されていません", true, "auth");
  }
  return res;
}

// ── ライブストリーム (SSE) ──────────────────────────────
function connect() {
  const es = new EventSource("/api/stream");
  es.onopen = () => setConn("ok", "ライブ接続中");
  es.onerror = () => setConn("err", "再接続中…");
  es.onmessage = (ev) => {
    try { render(JSON.parse(ev.data)); } catch (_) {}
  };
}
function setConn(cls, text) {
  const dot = $("#conn-dot");
  dot.className = "dot " + cls;
  $("#conn-text").textContent = text;
}

// ── レンダリング ────────────────────────────────────────
function render(snap) {
  if (snap.status === "error") {
    setConn("err", "取得エラー");
    showBanner("取得に失敗しています: " + (snap.error || "不明なエラー"), true, "live");
  } else if (snap.status === "degraded") {
    setConn("ok", "ライブ接続中（一部取得失敗）");
    showBanner("一部の銘柄を取得できていません（前回の値を表示中）: " + (snap.error || ""), false, "live");
  } else {
    setConn("ok", "ライブ接続中");
    hideBanner("live"); // 設定・認証のエラー表示はライブ更新で消さない
  }
  $("#updated").textContent = snap.updated_at
    ? "更新 " + new Date(snap.updated_at).toLocaleTimeString() + "（毎" + snap.poll_interval + "秒）"
    : "—";

  const seen = new Set();
  const assets = snap.assets || [];
  $("#empty").classList.toggle("hidden", assets.length > 0);

  for (const a of assets) {
    seen.add(a.key);
    if (a.sampled_at) pushSeries(a.key, Date.parse(a.sampled_at) / 1000, a.value);
    let card = cards.get(a.key);
    if (!card) { card = makeCard(a.key); cards.set(a.key, card); }
    $("#cards").appendChild(card); // 既存カードも末尾へ移し、監視対象の順番に揃える
    updateCard(card, a);
  }
  for (const [key, el] of cards) {
    if (!seen.has(key)) { el.remove(); cards.delete(key); series.delete(key); }
  }
}

function makeCard(key) {
  const el = document.createElement("div");
  el.className = "card";
  el.tabIndex = 0;
  el.title = "クリックでチャートを表示";
  el.innerHTML = `
    <div class="card-top">
      <span class="card-emoji"></span>
      <span class="card-label"></span>
      <span class="badge"></span>
    </div>
    <div class="card-price"></div>
    <div class="card-change"></div>
    <canvas class="spark"></canvas>
    <div class="card-asof"></div>
    <div class="card-foot"><span class="thr"></span><span class="key"></span></div>`;
  const open = () => openChart(key, el.querySelector(".card-label").textContent);
  el.addEventListener("click", open);
  el.addEventListener("keydown", (e) => { if (e.key === "Enter") open(); });
  return el;
}

function fmtChange(pct, prefix) {
  const up = pct >= 0;
  const arrow = pct === 0 ? "➡" : up ? "▲" : "▼";
  return `${prefix}${arrow} ${up ? "+" : ""}${pct.toFixed(2)}%`;
}

function updateCard(el, a) {
  el.querySelector(".card-emoji").textContent = a.emoji || "•";
  el.querySelector(".card-label").textContent = a.label;
  const badge = el.querySelector(".badge");
  badge.textContent = BADGE_LABEL[a.type] || a.type; badge.className = "badge " + a.type;
  el.querySelector(".card-price").textContent = a.display;

  // 主表示は 24 時間変化率（履歴 DB から算出）。直前の取得からの変化は小さく添える。
  const ch = el.querySelector(".card-change");
  const has24 = a.change_24h !== null && a.change_24h !== undefined;
  const hasPrev = a.change_pct !== null && a.change_pct !== undefined;
  const main = has24 ? a.change_24h : null;
  ch.className = "card-change " + (main === null || main === 0 ? "flat" : main > 0 ? "up" : "down");
  ch.textContent = has24 ? fmtChange(a.change_24h, "24h ") : "24h — データ蓄積中";
  if (hasPrev) {
    const sub = document.createElement("span");
    sub.className = "card-sub";
    sub.textContent = "前回比 " + (a.change_pct >= 0 ? "+" : "") + a.change_pct.toFixed(2) + "%";
    ch.appendChild(sub);
  }
  // データ時刻: 為替は日次更新、取得失敗中は前回の値であることを明示する。
  const notes = [];
  if (a.stale) notes.push("⚠ 取得失敗・前回の値");
  if (a.type === "forex") notes.push("日次レート");
  if (a.as_of) notes.push("データ時刻 " + new Date(a.as_of).toLocaleString());
  el.querySelector(".card-asof").textContent = notes.join(" · ");
  el.classList.toggle("stale", !!a.stale);
  el.querySelector(".thr").textContent = "閾値 " + a.threshold + "%";
  el.querySelector(".key").textContent = a.key;

  // 変動フラッシュ
  el.classList.remove("flash"); void el.offsetWidth; el.classList.add("flash");
  drawLine(el.querySelector(".spark"), series.get(a.key) || [], { color: trendColor(main ?? a.change_pct) });
}

// 時系列は [UNIX 秒, 値] の配列。同じ取得時刻は重ねない（SSE 再接続時の重複を防ぐ）。
function pushSeries(key, ts, value) {
  const arr = series.get(key) || [];
  if (arr.length && arr[arr.length - 1][0] >= ts) return;
  arr.push([ts, value]);
  const since = ts - SPARK_RANGE;
  while (arr.length && arr[0][0] < since) arr.shift();
  series.set(key, arr);
}

async function loadHistory() {
  try {
    const res = await fetch("/api/history?range=24h");
    if (!res.ok) return;
    const body = await res.json();
    for (const [key, pts] of Object.entries(body.series || {})) {
      const live = series.get(key) || [];
      const first = live.length ? live[0][0] : Infinity;
      series.set(key, pts.filter(([t]) => t < first).concat(live));
    }
  } catch (_) { /* 履歴が無くてもライブ表示は続ける */ }
}

function trendColor(pct) {
  return pct > 0 ? "#3fb950" : pct < 0 ? "#f85149" : "#8b98ad";
}

// 高解像度ディスプレイでぼやけないよう、表示サイズ × devicePixelRatio で描く。
function fitCanvas(canvas) {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth || 260, h = canvas.clientHeight || 40;
  if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
    canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { ctx, w, h };
}

function drawLine(canvas, data, { color, axes = false } = {}) {
  const { ctx, w, h } = fitCanvas(canvas);
  ctx.clearRect(0, 0, w, h);
  if (data.length < 2) return;
  const padX = axes ? 8 : 3, padTop = axes ? 18 : 3, padBottom = axes ? 20 : 3;
  const vals = data.map((p) => p[1]);
  const min = Math.min(...vals), max = Math.max(...vals), span = max - min || 1;
  const t0 = data[0][0], tspan = data[data.length - 1][0] - t0 || 1;
  const x = (t) => padX + ((t - t0) / tspan) * (w - padX * 2);
  const y = (v) => h - padBottom - ((v - min) / span) * (h - padTop - padBottom);

  ctx.beginPath();
  data.forEach(([t, v], i) => (i ? ctx.lineTo(x(t), y(v)) : ctx.moveTo(x(t), y(v))));
  ctx.strokeStyle = color; ctx.lineWidth = axes ? 2 : 1.8; ctx.lineJoin = "round"; ctx.stroke();

  ctx.lineTo(x(data[data.length - 1][0]), h - padBottom); ctx.lineTo(x(t0), h - padBottom); ctx.closePath();
  const g = ctx.createLinearGradient(0, 0, 0, h);
  g.addColorStop(0, color + "44"); g.addColorStop(1, color + "00");
  ctx.fillStyle = g; ctx.fill();

  if (axes) {
    const muted = getComputedStyle(document.body).getPropertyValue("--muted") || "#8b98ad";
    ctx.fillStyle = muted; ctx.font = "12px system-ui, sans-serif";
    ctx.textAlign = "left";
    ctx.fillText("高値 " + fmtNum(max), padX, 12);
    ctx.fillText(new Date(t0 * 1000).toLocaleString(), padX, h - 5);
    ctx.textAlign = "right";
    ctx.fillText("安値 " + fmtNum(min), w - padX, 12);
    ctx.fillText(new Date(data[data.length - 1][0] * 1000).toLocaleString(), w - padX, h - 5);
  }
}

function fmtNum(v) {
  return Math.abs(v) >= 1 ? v.toLocaleString(undefined, { maximumFractionDigits: 2 })
                          : v.toPrecision(4);
}

// ── チャート（24 時間 / 7 日）────────────────────────────
let chartKey = null;
async function openChart(key, label) {
  chartKey = key;
  $("#chart-title").textContent = label;
  $("#chart").classList.remove("hidden");
  await drawChart($("#chart .seg.active").dataset.range);
}
function closeChart() { chartKey = null; $("#chart").classList.add("hidden"); }

async function drawChart(range) {
  const key = chartKey;
  const canvas = $("#chart-canvas");
  $("#chart-note").textContent = "読み込み中…";
  const res = await fetch(`/api/history?range=${range}&key=${encodeURIComponent(key)}`).catch(() => null);
  if (key !== chartKey) return; // 待っている間に閉じた・切り替えた
  const pts = res && res.ok ? ((await res.json()).series || {})[key] || [] : [];
  if (pts.length < 2) {
    drawLine(canvas, [], {});
    $("#chart-note").textContent = "履歴がまだありません（取得のたびに蓄積されます）";
    return;
  }
  const pct = (pts[pts.length - 1][1] - pts[0][1]) / pts[0][1] * 100;
  $("#chart-note").textContent = `期間の変化 ${pct >= 0 ? "+" : ""}${pct.toFixed(2)}% · ${pts.length} 点`;
  drawLine(canvas, pts, { color: trendColor(pct), axes: true });
}

// source: "live"（取得状況）/ "config" / "auth"。ライブ更新は自分が出したものだけ消す。
let bannerSource = null;
function showBanner(msg, isErr, source = "config") {
  if (source === "live" && bannerSource && bannerSource !== "live") return;
  const b = $("#banner");
  b.textContent = msg; b.className = "banner" + (isErr ? " err" : "");
  bannerSource = source;
}
function hideBanner(source) {
  if (source && bannerSource !== source) return;
  $("#banner").classList.add("hidden");
  bannerSource = null;
}

// ── 設定ドロワー ────────────────────────────────────────
async function openSettings() {
  const res = await authFetch("/api/config");
  if (!res.ok) {
    if (res.status !== 403) {
      const err = await res.json().catch(() => ({}));
      showBanner("設定を読み込めませんでした: " + (err.detail || res.status), true, "config");
    }
    return;
  }
  hideBanner("config"); hideBanner("auth");
  const cfg = await res.json();
  fillSettings(cfg);
  $("#drawer").classList.remove("hidden");
}
function closeSettings() { $("#drawer").classList.add("hidden"); $("#cfg-error").classList.add("hidden"); }

function fillSettings(cfg) {
  $("#cfg-base").value = cfg.base_currency ?? "usd";
  $("#cfg-threshold").value = cfg.alert_threshold ?? 10;
  $("#cfg-poll").value = cfg.poll_interval ?? 60;
  $("#cfg-report").value = cfg.report_interval ?? 3600;
  $("#cfg-webhook").placeholder = cfg.webhook_configured ? "設定済み（変更する場合のみ入力）" : "https://discord.com/api/webhooks/...";
  $("#cfg-webhook").value = "";
  $("#cfg-token").placeholder = (cfg.web && cfg.web.auth_configured) ? "設定済み（変更する場合のみ入力）" : "（任意）";
  $("#cfg-token").value = "";
  $("#cfg-protect-read").checked = !cfg.web || cfg.web.protect_read !== false;
  const list = $("#watch-list"); list.innerHTML = "";
  (cfg.watch || []).forEach(addWatchRow);
  updateWatchCount();
}

// "pair" 形式を base/quote（ratio は num/den）に展開する。規則は config.py と同じ。
function expandPair(entry) {
  if (!entry.pair) return entry;
  const { pair, ...rest } = entry;
  if (entry.type === "forex" && !entry.base && !entry.quote) {
    const parts = String(pair).replace(/-/g, "/").split("/");
    if (parts.length === 2) return { ...rest, base: parts[0].trim(), quote: parts[1].trim() };
  } else if (entry.type === "ratio" && !entry.num && !entry.den) {
    const parts = String(pair).split("/");
    if (parts.length === 2) return { ...rest, num: parts[0].trim(), den: parts[1].trim() };
  }
  return entry;
}

function addWatchRow(entry) {
  entry = expandPair(entry);
  const type = TPL[entry.type] ? entry.type : "crypto";
  const node = $(TPL[type]).content.firstElementChild.cloneNode(true);
  rowEntries.set(node, entry);
  node.querySelector(".w-emoji").value = entry.emoji || "";
  node.querySelector(".w-label").value = entry.label || "";
  const thr = entry.threshold;
  node.querySelector(".w-threshold").value = (thr === undefined || thr === null) ? "" : thr;
  if (type === "crypto") {
    node.querySelector(".w-id").value = entry.id || "";
    const vs = entry.vs ?? ["usd", "jpy"];
    node.querySelector(".w-vs").value = Array.isArray(vs) ? vs.join(",") : String(vs);
  } else if (type === "forex") {
    node.querySelector(".w-base").value = entry.base || "";
    node.querySelector(".w-quote").value = entry.quote || "";
  } else if (type === "hyperliquid") {
    node.querySelector(".w-coin").value = entry.coin || "";
  } else if (type === "ratio") {
    node.querySelector(".w-num").value = entry.num || "";
    node.querySelector(".w-den").value = entry.den || "";
  }
  node.querySelector(".w-del").addEventListener("click", () => { node.remove(); updateWatchCount(); });
  node.addEventListener("input", (ev) => ev.target.classList.remove("invalid"));
  $("#watch-list").appendChild(node);
  updateWatchCount();
}
function updateWatchCount() { $("#watch-count").textContent = "(" + $("#watch-list").children.length + ")"; }

// 種別ごとの必須入力欄（クラス名 → エントリのキー）。
const REQUIRED = {
  crypto: { ".w-id": "id" },
  forex: { ".w-base": "base", ".w-quote": "quote" },
  hyperliquid: { ".w-coin": "coin" },
  ratio: { ".w-num": "num", ".w-den": "den" },
};

// 元のエントリを土台に UI の入力だけを上書きする（key など UI に無い項目は残す）。
// 必須欄が空の行は捨てずに errors に積む。
function collectWatch() {
  const watch = [], errors = [];
  [...$("#watch-list").children].forEach((item, i) => {
    const type = item.dataset.type;
    const e = { ...(rowEntries.get(item) || {}), type };
    const setOpt = (k, v) => { if (v === "") delete e[k]; else e[k] = v; };
    setOpt("emoji", item.querySelector(".w-emoji").value.trim());
    setOpt("label", item.querySelector(".w-label").value.trim());
    const thrRaw = item.querySelector(".w-threshold").value.trim();
    setOpt("threshold", thrRaw === "" ? "" : Number(thrRaw));
    if (type === "crypto") {
      const vs = item.querySelector(".w-vs").value.split(",").map((s) => s.trim()).filter(Boolean);
      if (vs.length) e.vs = vs; else delete e.vs;
    }
    let missing = false;
    for (const [sel, k] of Object.entries(REQUIRED[type] || {})) {
      const input = item.querySelector(sel);
      e[k] = input.value.trim();
      input.classList.toggle("invalid", !e[k]);
      if (!e[k]) missing = true;
    }
    if (missing) errors.push(`${i + 1} 行目 (${type}) の必須項目が空です`);
    watch.push(e);
  });
  return { watch, errors };
}

async function saveSettings() {
  const { watch, errors } = collectWatch();
  if (errors.length) {
    $("#cfg-error").textContent = "保存できませんでした: " + errors.join(" / ");
    $("#cfg-error").classList.remove("hidden");
    return;
  }
  const payload = {
    base_currency: $("#cfg-base").value.trim() || "usd",
    alert_threshold: Number($("#cfg-threshold").value || 10),
    poll_interval: Number($("#cfg-poll").value || 60),
    report_interval: Number($("#cfg-report").value || 0),
    watch,
  };
  const webhook = $("#cfg-webhook").value.trim();
  if (webhook) payload.webhook_url = webhook;
  const token = $("#cfg-token").value.trim();
  payload.web = { protect_read: $("#cfg-protect-read").checked };
  if (token) payload.web.auth_token = token;

  $("#save-status").textContent = "保存中…";
  const res = await authFetch("/api/config", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (res.ok) {
    if (token) await login(token); // トークンを変えると既存のセッションは無効になるため、入れ直す
    $("#save-status").textContent = "✓ 反映しました";
    $("#cfg-error").classList.add("hidden");
    setTimeout(() => { $("#save-status").textContent = ""; closeSettings(); }, 700);
  } else {
    const err = await res.json().catch(() => ({}));
    $("#cfg-error").textContent = "保存できませんでした: " + (err.detail || res.status);
    $("#cfg-error").classList.remove("hidden");
    $("#save-status").textContent = "";
  }
}

// ── イベント配線 ────────────────────────────────────────
$("#btn-settings").addEventListener("click", openSettings);
$("#btn-close").addEventListener("click", closeSettings);
$("#drawer").querySelector("[data-close]").addEventListener("click", closeSettings);
$("#btn-save").addEventListener("click", saveSettings);
$("#add-crypto").addEventListener("click", () => addWatchRow({ type: "crypto", vs: ["usd", "jpy"] }));
$("#add-forex").addEventListener("click", () => addWatchRow({ type: "forex" }));
$("#add-hyperliquid").addEventListener("click", () => addWatchRow({ type: "hyperliquid" }));
$("#add-ratio").addEventListener("click", () => addWatchRow({ type: "ratio" }));
$("#btn-refresh").addEventListener("click", async () => {
  $("#btn-refresh").disabled = true;
  await authFetch("/api/refresh", { method: "POST" }).catch(() => {});
  setTimeout(() => ($("#btn-refresh").disabled = false), 800);
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape") { closeSettings(); closeChart(); } });
$("#chart-close").addEventListener("click", closeChart);
$("#chart").querySelector("[data-close]").addEventListener("click", closeChart);
for (const seg of document.querySelectorAll("#chart .seg")) {
  seg.addEventListener("click", () => {
    document.querySelectorAll("#chart .seg").forEach((s) => s.classList.toggle("active", s === seg));
    drawChart(seg.dataset.range);
  });
}

$("#login-submit").addEventListener("click", submitLogin);
$("#login-token").addEventListener("keydown", (e) => { if (e.key === "Enter") submitLogin(); });
$("#login-cancel").addEventListener("click", () => finishLogin(false));

// 閲覧に認証が必要な設定なら、先にログインしてから履歴とライブ接続を始める。
async function start() {
  const st = await fetch("/api/auth").then((r) => r.json()).catch(() => ({}));
  if (st.protect_read && !st.authenticated) {
    while (!(await requestLogin())) { /* 閲覧にはログインが必須 */ }
  }
  await loadHistory();
  connect();
}
start();
