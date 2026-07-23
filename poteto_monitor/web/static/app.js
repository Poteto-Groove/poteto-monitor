"use strict";

const $ = (sel) => document.querySelector(sel);
const TOKEN_KEY = "poteto_token";
const SPARK_MAX = 60;

const cards = new Map();      // key -> card element
const series = new Map();     // key -> number[] (直近の値)

// ── 認証つき fetch ──────────────────────────────────────
function authHeaders() {
  const t = localStorage.getItem(TOKEN_KEY);
  return t ? { "X-Auth-Token": t } : {};
}
async function authFetch(url, opts = {}) {
  opts.headers = Object.assign({}, opts.headers, authHeaders());
  let res = await fetch(url, opts);
  if (res.status === 401) {
    const t = prompt("Web 認証トークンを入力してください:");
    if (t) {
      localStorage.setItem(TOKEN_KEY, t);
      opts.headers = Object.assign({}, opts.headers, authHeaders());
      res = await fetch(url, opts);
    }
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
    showBanner("取得に失敗しています: " + (snap.error || "不明なエラー"), true);
  } else {
    setConn("ok", "ライブ接続中");
    hideBanner();
  }
  $("#updated").textContent = snap.updated_at
    ? "更新 " + new Date(snap.updated_at).toLocaleTimeString() + "（毎" + snap.poll_interval + "秒）"
    : "—";

  const seen = new Set();
  const assets = snap.assets || [];
  $("#empty").classList.toggle("hidden", assets.length > 0);

  for (const a of assets) {
    seen.add(a.key);
    pushSeries(a.key, a.value);
    let card = cards.get(a.key);
    if (!card) { card = makeCard(a.key); cards.set(a.key, card); $("#cards").appendChild(card); }
    updateCard(card, a);
  }
  for (const [key, el] of cards) {
    if (!seen.has(key)) { el.remove(); cards.delete(key); series.delete(key); }
  }
}

function makeCard(key) {
  const el = document.createElement("div");
  el.className = "card";
  el.innerHTML = `
    <div class="card-top">
      <span class="card-emoji"></span>
      <span class="card-label"></span>
      <span class="badge"></span>
    </div>
    <div class="card-price"></div>
    <div class="card-change"></div>
    <canvas class="spark" width="260" height="40"></canvas>
    <div class="card-foot"><span class="thr"></span><span class="key"></span></div>`;
  return el;
}

function updateCard(el, a) {
  el.querySelector(".card-emoji").textContent = a.emoji || "•";
  el.querySelector(".card-label").textContent = a.label;
  const badge = el.querySelector(".badge");
  badge.textContent = a.type; badge.className = "badge " + a.type;
  el.querySelector(".card-price").textContent = a.display;

  const ch = el.querySelector(".card-change");
  if (a.change_pct === null || a.change_pct === undefined) {
    ch.className = "card-change flat"; ch.textContent = "— 初回取得";
  } else {
    const up = a.change_pct >= 0;
    ch.className = "card-change " + (a.change_pct === 0 ? "flat" : up ? "up" : "down");
    const arrow = a.change_pct === 0 ? "➡" : up ? "▲" : "▼";
    ch.textContent = `${arrow} ${up ? "+" : ""}${a.change_pct.toFixed(2)}%`;
  }
  el.querySelector(".thr").textContent = "閾値 " + a.threshold + "%";
  el.querySelector(".key").textContent = a.key;

  // 変動フラッシュ
  el.classList.remove("flash"); void el.offsetWidth; el.classList.add("flash");
  drawSpark(el.querySelector(".spark"), series.get(a.key) || [], a.change_pct);
}

function pushSeries(key, value) {
  const arr = series.get(key) || [];
  arr.push(value);
  if (arr.length > SPARK_MAX) arr.shift();
  series.set(key, arr);
}

function drawSpark(canvas, data, changePct) {
  const ctx = canvas.getContext("2d");
  const w = canvas.width, h = canvas.height, pad = 3;
  ctx.clearRect(0, 0, w, h);
  if (data.length < 2) return;
  const min = Math.min(...data), max = Math.max(...data), span = max - min || 1;
  const x = (i) => pad + (i / (data.length - 1)) * (w - pad * 2);
  const y = (v) => h - pad - ((v - min) / span) * (h - pad * 2);
  const color = changePct > 0 ? "#3fb950" : changePct < 0 ? "#f85149" : "#8b98ad";

  ctx.beginPath();
  data.forEach((v, i) => (i ? ctx.lineTo(x(i), y(v)) : ctx.moveTo(x(i), y(v))));
  ctx.strokeStyle = color; ctx.lineWidth = 1.8; ctx.lineJoin = "round"; ctx.stroke();

  ctx.lineTo(x(data.length - 1), h); ctx.lineTo(x(0), h); ctx.closePath();
  const g = ctx.createLinearGradient(0, 0, 0, h);
  g.addColorStop(0, color + "44"); g.addColorStop(1, color + "00");
  ctx.fillStyle = g; ctx.fill();
}

function showBanner(msg, isErr) {
  const b = $("#banner");
  b.textContent = msg; b.className = "banner" + (isErr ? " err" : "");
}
function hideBanner() { $("#banner").classList.add("hidden"); }

// ── 設定ドロワー ────────────────────────────────────────
async function openSettings() {
  const res = await authFetch("/api/config");
  if (!res.ok) { showBanner("設定を読み込めませんでした (" + res.status + ")", true); return; }
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
  const list = $("#watch-list"); list.innerHTML = "";
  (cfg.watch || []).forEach(addWatchRow);
  updateWatchCount();
}

function addWatchRow(entry) {
  const type = entry.type === "forex" ? "forex" : "crypto";
  const tpl = $(type === "forex" ? "#tpl-forex" : "#tpl-crypto");
  const node = tpl.content.firstElementChild.cloneNode(true);
  node.querySelector(".w-emoji").value = entry.emoji || "";
  node.querySelector(".w-label").value = entry.label || "";
  const thr = entry.threshold;
  node.querySelector(".w-threshold").value = (thr === undefined || thr === null) ? "" : thr;
  if (type === "crypto") {
    node.querySelector(".w-id").value = entry.id || "";
    node.querySelector(".w-vs").value = (entry.vs || ["usd", "jpy"]).join(",");
  } else {
    node.querySelector(".w-base").value = entry.base || "";
    node.querySelector(".w-quote").value = entry.quote || "";
  }
  node.querySelector(".w-del").addEventListener("click", () => { node.remove(); updateWatchCount(); });
  $("#watch-list").appendChild(node);
  updateWatchCount();
}
function updateWatchCount() { $("#watch-count").textContent = "(" + $("#watch-list").children.length + ")"; }

function collectWatch() {
  const out = [];
  for (const item of $("#watch-list").children) {
    const type = item.dataset.type;
    const emoji = item.querySelector(".w-emoji").value.trim();
    const label = item.querySelector(".w-label").value.trim();
    const thrRaw = item.querySelector(".w-threshold").value.trim();
    const e = { type };
    if (emoji) e.emoji = emoji;
    if (label) e.label = label;
    if (thrRaw !== "") e.threshold = Number(thrRaw);
    if (type === "crypto") {
      e.id = item.querySelector(".w-id").value.trim();
      const vs = item.querySelector(".w-vs").value.split(",").map((s) => s.trim()).filter(Boolean);
      if (vs.length) e.vs = vs;
      if (!e.id) continue;
    } else {
      e.base = item.querySelector(".w-base").value.trim();
      e.quote = item.querySelector(".w-quote").value.trim();
      if (!e.base || !e.quote) continue;
    }
    out.push(e);
  }
  return out;
}

async function saveSettings() {
  const payload = {
    base_currency: $("#cfg-base").value.trim() || "usd",
    alert_threshold: Number($("#cfg-threshold").value || 10),
    poll_interval: Number($("#cfg-poll").value || 60),
    report_interval: Number($("#cfg-report").value || 0),
    watch: collectWatch(),
  };
  const webhook = $("#cfg-webhook").value.trim();
  if (webhook) payload.webhook_url = webhook;
  const token = $("#cfg-token").value.trim();
  if (token) payload.web = { auth_token: token };

  $("#save-status").textContent = "保存中…";
  const res = await authFetch("/api/config", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (res.ok) {
    if (token) localStorage.setItem(TOKEN_KEY, token); // 認証を変更したら以後に使う
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
$("#btn-refresh").addEventListener("click", async () => {
  $("#btn-refresh").disabled = true;
  await authFetch("/api/refresh", { method: "POST" }).catch(() => {});
  setTimeout(() => ($("#btn-refresh").disabled = false), 800);
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeSettings(); });

connect();
