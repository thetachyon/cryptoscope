"use strict";
/* Stablecoin dashboard frontend — one shared SPA for all 8 *Scope
   stablecoin apps (usdtscope, usdcscope, daiscope, usdescope,
   fdusdsscope, pyusdsscope, tusdscope, usddscope). Per-app behaviour
   comes from /config.js (window.SCOPE_CONFIG) and the coin metadata in
   /api/<scope>/stats. API: /api/<scope>/* — one coin per app.
   Light Etherscan-like theme (styles.css is shared): .card, .tbl,
   .badge, .muted, .topbar, .wrap, .error-banner, .statgrid, .detail,
   .copybtn, .loadmore, .chart-svg, .daybtns, .notice. */
const CFG = window.SCOPE_CONFIG || {};
const SCOPE = CFG.scope || "usdtscope";
const TITLE = CFG.title || "StablecoinScope";
const API = CFG.apiPrefix || ("/api/" + SCOPE);
const SIBLINGS = Array.isArray(CFG.siblings) ? CFG.siblings : [];

/* ---------------- helpers ---------------- */
const $ = (s) => document.querySelector(s);
function esc(s) {
  return String(s ?? "").replace(/[&<>\"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
async function fetchJson(url, opts, timeoutMs) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs || 30000);
  try {
    const r = await fetch(url, { ...(opts || {}), signal: ctrl.signal });
    let j = null;
    try { j = await r.json(); } catch (e) { throw new Error("Bad response from server"); }
    if (!r.ok || (j && j.error)) throw new Error((j && j.error) || ("HTTP " + r.status));
    return j;
  } catch (e) {
    if (e.name === "AbortError") throw new Error("Request timed out");
    throw e;
  } finally { clearTimeout(timer); }
}
async function get(path) { return fetchJson(API + path); }
function num(n) {
  if (n == null || n === "") return "—";
  const x = Number(n);
  return Number.isFinite(x) ? x.toLocaleString("en-US") : String(n);
}
function usd(n) {
  if (n == null || n === "") return "—";
  const x = Number(n);
  return Number.isFinite(x) ? "$" + x.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : "—";
}
/* Large USD amounts: $177.45B / $70.71B / $4.52B / $289.78M. */
function usdBig(n) {
  if (n == null || n === "") return "—";
  const x = Number(n);
  if (!Number.isFinite(x)) return "—";
  const abs = Math.abs(x);
  if (abs >= 1e12) return "$" + (x / 1e12).toFixed(2) + "T";
  if (abs >= 1e9) return "$" + (x / 1e9).toFixed(2) + "B";
  if (abs >= 1e6) return "$" + (x / 1e6).toFixed(2) + "M";
  if (abs >= 1e3) return "$" + (x / 1e3).toFixed(2) + "K";
  return usd(x);
}
function tokenAmt(n) {
  if (n == null || n === "") return "—";
  const x = Number(n);
  if (!Number.isFinite(x)) return "—";
  const abs = Math.abs(x);
  if (abs >= 1e9) return (x / 1e9).toFixed(2) + "B";
  if (abs >= 1e6) return (x / 1e6).toFixed(2) + "M";
  if (abs >= 1e3) return (x / 1e3).toFixed(2) + "K";
  return x.toLocaleString("en-US", { maximumFractionDigits: 4 });
}
function priceFmt(n) {
  if (n == null || n === "") return "—";
  const x = Number(n);
  if (!Number.isFinite(x)) return "—";
  return "$" + x.toFixed(4);
}
function bpsFmt(bps) {
  if (bps == null || bps === "") return "—";
  const x = Number(bps);
  if (!Number.isFinite(x)) return "—";
  return (x >= 0 ? "+" : "") + x.toFixed(2) + " bps";
}
function pctFmt(v) {
  if (v == null || v === "") return "—";
  const x = Number(v);
  if (!Number.isFinite(x)) return "—";
  return (x >= 0 ? "+" : "") + x.toFixed(2) + "%";
}
function dateStr(ts) {
  if (ts == null || ts === "") return "—";
  try {
    const d = new Date(Number(ts) * 1000);
    return d.toISOString().slice(0, 10);
  } catch (e) { return String(ts); }
}
function dateTimeStr(ts) {
  if (ts == null || ts === "") return "—";
  try {
    const d = new Date(Number(ts) * (Number(ts) > 1e12 ? 1 : 1000));
    return d.toISOString().replace("T", " ").slice(0, 19) + " UTC";
  } catch (e) { return String(ts); }
}
function shortH(s) {
  s = String(s || "");
  return s.length > 18 ? s.slice(0, 10) + "…" + s.slice(-8) : s;
}
function copyBtn(text) {
  return `<button class="copybtn" data-copy="${esc(text)}" title="Copy to clipboard">⧉</button>`;
}
function healthBadge(health, bps) {
  if (health === "pegged") return '<span class="badge ok">Pegged</span>';
  if (health === "watch") return '<span class="badge warn">Off-peg watch</span>';
  if (health === "depegged") return '<span class="badge fail">Depegged</span>';
  return '<span class="badge grey">Unknown</span>';
}
function banner(msg) {
  const b = $("#error-banner");
  if (!b) return;
  if (msg) { b.textContent = msg; b.classList.remove("hidden"); }
  else { b.textContent = ""; b.classList.add("hidden"); }
}
function render(html) { $("#view").innerHTML = html; banner(null); }

/* ---------------- SVG line chart (hand-rolled, no dependency) ---------------- */
function svgChart(points, opts = {}) {
  const vals = (points || []).map((p) => p.y).filter((v) => Number.isFinite(v));
  if (!points || !points.length || !vals.length) return '<div class="empty">No chart data</div>';
  const W = 760, H = 260, padL = 78, padR = 14, padT = 14, padB = 30;
  let min = Math.min(...vals), max = Math.max(...vals);
  if (min === max) { min -= 1; max += 1; }
  const pad = (max - min) * 0.08;
  min -= pad; max += pad;
  const iw = W - padL - padR, ih = H - padT - padB;
  const X = (i) => padL + (points.length === 1 ? iw / 2 : (i / (points.length - 1)) * iw);
  const Y = (v) => padT + (1 - (v - min) / (max - min)) * ih;
  const fmt = opts.fmtY || ((v) => Number(v).toLocaleString("en-US", { maximumFractionDigits: 2 }));
  const line = points.map((p, i) => `${X(i).toFixed(1)},${Y(p.y).toFixed(1)}`).join(" ");
  const mid = (min + max) / 2;
  // Depeg markers: red dots where opts.isDepeg(point) is true.
  const marks = opts.isDepeg ? points.map((p, i) => opts.isDepeg(p)
    ? `<circle cx="${X(i).toFixed(1)}" cy="${Y(p.y).toFixed(1)}" r="4" fill="#d64545" stroke="#fff" stroke-width="1"><title>Depeg ${esc(p.x)}: ${esc(fmt(p.y))}</title></circle>` : "").join("") : "";
  // Optional reference line (e.g. $1.00 on the peg chart).
  const ref = (opts.refY != null) ? `<line x1="${padL}" y1="${Y(opts.refY).toFixed(1)}" x2="${W - padR}" y2="${Y(opts.refY).toFixed(1)}" stroke="#d64545" stroke-dasharray="5 4" stroke-width="1.2"/><text x="${W - padR - 4}" y="${Y(opts.refY).toFixed(1) - 5}" text-anchor="end" class="chart-tick" fill="#d64545">${esc(opts.refLabel || "")}</text>` : "";
  return `<svg viewBox="0 0 ${W} ${H}" class="chart-svg" role="img" aria-label="${esc(opts.label || "chart")}">
    <line x1="${padL}" y1="${padT}" x2="${padL}" y2="${H - padB}" stroke="#e3e9f0"/>
    <line x1="${padL}" y1="${H - padB}" x2="${W - padR}" y2="${H - padB}" stroke="#e3e9f0"/>
    <line x1="${padL}" y1="${Y(mid).toFixed(1)}" x2="${W - padR}" y2="${Y(mid).toFixed(1)}" stroke="#eef1f5"/>
    <text x="${padL - 8}" y="${padT + 4}" text-anchor="end" class="chart-tick">${esc(fmt(max))}</text>
    <text x="${padL - 8}" y="${Y(mid).toFixed(1) + 4}" text-anchor="end" class="chart-tick">${esc(fmt(mid))}</text>
    <text x="${padL - 8}" y="${H - padB + 4}" text-anchor="end" class="chart-tick">${esc(fmt(min))}</text>
    ${ref}
    <polyline points="${line}" fill="none" stroke="#0784c3" stroke-width="2"/>
    ${points.map((p, i) => `<circle cx="${X(i).toFixed(1)}" cy="${Y(p.y).toFixed(1)}" r="2.5" fill="#0784c3"><title>${esc(p.x)}: ${esc(fmt(p.y))}</title></circle>`).join("")}
    ${marks}
    <text x="${padL}" y="${H - 8}" class="chart-tick">${esc(points[0].x)}</text>
    ${points.length > 2 ? `<text x="${W / 2}" y="${H - 8}" text-anchor="middle" class="chart-tick">${esc(points[Math.floor(points.length / 2)].x)}</text>` : ""}
    <text x="${W - padR}" y="${H - 8}" text-anchor="end" class="chart-tick">${esc(points[points.length - 1].x)}</text>
  </svg>`;
}

/* ---------------- shared state ---------------- */
let STATS = null;      // /stats payload (coin metadata + live figures)
let PEG_DAYS = 30;     // home peg-chart window
let SUPPLY_DAYS = 90;  // home supply-chart window
let pollTimer = null;
function clearPoll() { if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } }

async function loadStats() {
  if (STATS) return STATS;
  STATS = await get("/stats");
  return STATS;
}
function primarySupplyDenominator() {
  if (!STATS) return null;
  if (STATS.primary_chain === "tron") {
    const row = (STATS.chains || []).find((c) => c.chain === "Tron");
    return row ? row.supply_usd : null;
  }
  const t = STATS.token_on_primary;
  if (t && t.total_supply != null) {
    const v = Number(String(t.total_supply).replace(/,/g, ""));
    return Number.isFinite(v) ? v : null;
  }
  return null;
}
function extAddrLink(addr, chain) {
  if (!addr) return '<span class="muted">—</span>';
  if (chain === "tron") return `<a class="mono" href="https://tronscan.org/#/address/${esc(addr)}" target="_blank" rel="noopener">${esc(shortH(addr))}</a>`;
  return `<a class="mono" href="https://ethscope.vercel.app/#/address/${esc(addr)}" target="_blank" rel="noopener">${esc(shortH(addr))}</a>`;
}
function extTxLink(hash, chain) {
  if (!hash) return '<span class="muted">—</span>';
  if (chain === "tron") return `<a class="mono" href="https://tronscan.org/#/transaction/${esc(hash)}" target="_blank" rel="noopener">${esc(shortH(hash))}</a>`;
  return `<a class="mono" href="https://ethscope.vercel.app/#/tx/${esc(hash)}" target="_blank" rel="noopener">${esc(shortH(hash))}</a>`;
}

/* ---------------- views ---------------- */
async function homePage() {
  render(`<div class="card" style="margin-top:22px"><div class="empty"><span class="spinner"></span> Loading ${esc(TITLE)}…</div></div>`);
  try {
    const [s, peg, sup, dep] = await Promise.all([
      loadStats(), get("/peg?days=" + PEG_DAYS), get("/supply?days=" + SUPPLY_DAYS), get("/depegs"),
    ]);
    const denom = primarySupplyDenominator();
    const pegPts = (peg.points || []).map((p) => ({ x: dateStr(p.date), y: p.price, bps: p.deviation_bps }));
    const supPts = (sup.points || []).map((p) => ({ x: dateStr(p.date), y: p.supply_usd }));
    const topChains = (s.chains || []).slice(0, 5);
    document.title = `${s.symbol} — ${TITLE}`;
    render(`
    <h1 class="pagehead" style="margin-top:22px">${esc(s.name)} (${esc(s.symbol)}) <span class="muted" style="font-size:15px">· ${esc(s.issuer || "")}</span></h1>
    <p class="muted">${esc(s.description || "")}</p>
    <div class="statgrid">
      <div class="stat"><div class="stat-label">Price</div><div class="stat-value">${priceFmt(s.price_usd)}</div><div class="muted">vs $1.00 · ${bpsFmt(s.deviation_bps)} ${healthBadge(s.peg_health, s.deviation_bps)}</div><div class="muted">Source: ${esc(s.price_source || "—")}</div></div>
      <div class="stat"><div class="stat-label">Total supply</div><div class="stat-value">${usdBig(s.supply_usd)}</div><div class="muted">7d ${pctFmt(s.supply_change_7d_pct)} · 30d ${pctFmt(s.supply_change_30d_pct)}</div><div class="muted">DeFiLlama circulating, all chains</div></div>
      <div class="stat"><div class="stat-label">Holders (${esc(s.primary_chain === "tron" ? "Tron" : "Ethereum")})</div><div class="stat-value">${s.token_on_primary ? num(s.token_on_primary.holders_count) : (s.tron_token ? "see Holders" : "—")}</div><div class="muted">${s.primary_chain === "tron" ? "TronScan list capped at 10,000" : "Blockscout holders_count"}</div><div class="muted">Contract: <span class="mono">${esc(shortH(s.contract))}</span> ${copyBtn(s.contract)}</div></div>
      <div class="stat"><div class="stat-label">Market cap</div><div class="stat-value">${usdBig(s.market_cap_usd)}</div><div class="muted">CoinGecko · total supply ${tokenAmt(s.gecko_total_supply)} ${esc(s.symbol)}</div><div class="muted">Peg mechanism: ${esc(s.peg_mechanism || "—")}</div></div>
    </div>

    <div class="card"><div class="card-head">Peg — price vs $1.00 <span class="muted">(${esc(String(PEG_DAYS))} days · DeFiLlama daily closes · <span style="color:#d64545">●</span> depeg day, |dev| &gt; 50 bps)</span>
      <span class="daybtns" style="float:right">${[7, 30, 90].map((d) => `<button class="btn ${d === PEG_DAYS ? "active" : ""}" data-pegdays="${d}">${d}d</button>`).join(" ")}</span></div>
      <div class="chart-wrap">${svgChart(pegPts, { label: `${s.symbol} price, ${PEG_DAYS} days`, fmtY: (v) => "$" + Number(v).toFixed(4), refY: 1.0, refLabel: "$1.00", isDepeg: (p) => p.bps != null && Math.abs(p.bps) > 50 })}</div>
      <div class="muted">Depeg days in window: <b>${num(peg.depeg_days_in_window)}</b>${peg.worst_depeg_in_window ? ` · worst: ${priceFmt(peg.worst_depeg_in_window.price)} (${bpsFmt(peg.worst_depeg_in_window.deviation_bps)}) on ${dateStr(peg.worst_depeg_in_window.date)}` : ""} · <a href="#/depegs">Full depeg history →</a></div>
    </div>

    <div class="card"><div class="card-head">Supply — circulating, all chains <span class="muted">(${esc(String(SUPPLY_DAYS))} days · DeFiLlama)</span>
      <span class="daybtns" style="float:right">${[30, 90, 365].map((d) => `<button class="btn ${d === SUPPLY_DAYS ? "active" : ""}" data-supdays="${d}">${d}d</button>`).join(" ")}</span></div>
      <div class="chart-wrap">${svgChart(supPts, { label: `${s.symbol} supply, ${SUPPLY_DAYS} days`, fmtY: (v) => usdBig(v) })}</div>
      <div class="muted">History: ${num(sup.points_count)} daily points (${dateStr(sup.history_first)} → ${dateStr(sup.history_last)})</div>
    </div>

    <div class="cols2">
      <div class="card"><div class="card-head">Supply by chain</div>
        <table class="tbl"><thead><tr><th>Chain</th><th style="text-align:right">Supply</th><th style="text-align:right">Share</th></tr></thead><tbody>
        ${topChains.map((c) => `<tr><td>${esc(c.chain)}</td><td style="text-align:right">${usdBig(c.supply_usd)}</td><td style="text-align:right">${c.share_pct != null ? c.share_pct.toFixed(1) + "%" : "—"}</td></tr>`).join("") || '<tr><td colspan="3" class="empty">No chain data</td></tr>'}
        </tbody></table>
        <div class="muted">${num(s.chain_count)} chains with supply · primary chain for holders/transfers: ${esc(s.primary_chain === "tron" ? "Tron" : "Ethereum")}</div>
      </div>
      <div class="card"><div class="card-head">Depeg history — summary</div>
        <div class="detail">
          <div><span class="muted">Depeg days (all history):</span> <b>${num(dep.depeg_days_total)}</b> of ${num(dep.history_days)} days (${dateStr(dep.history_first)} → ${dateStr(dep.history_last)})</div>
          <div><span class="muted">Worst depeg:</span> ${dep.worst_depeg ? `${priceFmt(dep.worst_depeg.price)} (${bpsFmt(dep.worst_depeg.deviation_bps)}) on ${dateStr(dep.worst_depeg.date)}` : "none"}</div>
          <div><span class="muted">Most recent:</span> ${dep.most_recent_depeg ? `${priceFmt(dep.most_recent_depeg.price)} (${bpsFmt(dep.most_recent_depeg.deviation_bps)}) on ${dateStr(dep.most_recent_depeg.date)}` : "none"}</div>
        </div>
        <div style="margin-top:10px"><a href="#/depegs">Full depeg history →</a> · <a href="#/holders">Top holders →</a> · <a href="#/transfers">Recent transfers →</a> · <a href="#/reserves">Reserves &amp; attestations →</a></div>
      </div>
    </div>
    <p class="muted">Sources: DeFiLlama stablecoins API (peg + supply), CoinGecko (market cap, price fallback), ${s.primary_chain === "tron" ? "TronScan / TronGrid (Tron holders + transfers)" : "Blockscout v2 (Ethereum holders + transfers)"}. No API keys. ${denom ? "" : ""}</p>
    `);
    // Day-range buttons re-render the home page with the new window.
    document.querySelectorAll("[data-pegdays]").forEach((b) => b.addEventListener("click", () => { PEG_DAYS = Number(b.dataset.pegdays); homePage(); }));
    document.querySelectorAll("[data-supdays]").forEach((b) => b.addEventListener("click", () => { SUPPLY_DAYS = Number(b.dataset.supdays); homePage(); }));
    clearPoll();
    pollTimer = setInterval(() => { STATS = null; homePage(); }, 60000);
  } catch (e) {
    render(`<div class="card" style="margin-top:22px"><div class="empty">Could not load ${esc(TITLE)}: ${esc(e.message)}<br><a href="#/">Retry</a></div></div>`);
  }
}

function holdersRows(items, denom, chain, startIdx) {
  return (items || []).map((h, i) => {
    const rank = (startIdx || 0) + i + 1;
    const share = (denom && h.balance != null) ? (h.balance / denom * 100) : null;
    const label = h.label ? ` <span class="badge grey">${esc(h.label)}</span>` : "";
    const contract = h.is_contract ? ' <span class="badge grey" title="Contract">C</span>' : "";
    return `<tr><td>${num(rank)}</td><td>${extAddrLink(h.address, chain)}${label}${contract} ${copyBtn(h.address || "")}</td><td style="text-align:right">${tokenAmt(h.balance)}</td><td style="text-align:right">${usd(h.balance_usd)}</td><td style="text-align:right">${share != null ? share.toFixed(2) + "%" : "—"}</td></tr>`;
  }).join("");
}

async function holdersPage() {
  render(`<div class="card" style="margin-top:22px"><div class="empty"><span class="spinner"></span> Loading holders…</div></div>`);
  try {
    const s = await loadStats();
    const chain = s.primary_chain;
    const denom = primarySupplyDenominator();
    const d = await get("/holders");
    let nextParams = d.next_page_params || null;
    let nextStart = d.next_start != null ? d.next_start : null;
    let offset = 0;
    const note = d.holders_total_note ? `<div class="notice">${esc(d.holders_total_note)}</div>` : "";
    render(`
    <h1 class="pagehead" style="margin-top:22px">Top holders — ${esc(s.symbol)} <span class="muted" style="font-size:15px">(${esc(chain === "tron" ? "Tron" : "Ethereum")} · ${d.holders_total != null ? num(d.holders_total) + " holders" : "holder count unavailable"})</span></h1>
    ${note}
    <div class="card"><div class="card-head">Largest holders by balance <span class="muted">· share of ${esc(chain === "tron" ? "Tron" : "Ethereum")} supply${denom ? ` (${tokenAmt(denom)} ${esc(s.symbol)})` : ""}</span></div>
    <table class="tbl"><thead><tr><th>#</th><th>Holder</th><th style="text-align:right">Balance (${esc(s.symbol)})</th><th style="text-align:right">USD value</th><th style="text-align:right">Share</th></tr></thead>
    <tbody id="holders-body">${holdersRows(d.items, denom, chain)}</tbody></table>
    <div style="margin-top:12px"><button class="btn loadmore" id="holders-more">Load more</button></div>
    </div>
    <p class="muted">Source: ${chain === "tron" ? "TronScan keyless API" : "Blockscout v2 (Ethereum)"}. Labels come from Blockscout address tags where available; chain data gives addresses, not identities.</p>
    `);
    const moreBtn = $("#holders-more");
    const updateBtn = () => {
      if (!moreBtn) return;
      const hasMore = chain === "tron" ? nextStart != null : !!nextParams;
      moreBtn.style.display = hasMore ? "" : "none";
    };
    updateBtn();
    if (moreBtn) moreBtn.addEventListener("click", async () => {
      moreBtn.disabled = true;
      try {
        let nd;
        if (chain === "tron") nd = await get("/holders?start=" + nextStart);
        else {
          const qs = new URLSearchParams(nextParams).toString();
          nd = await get("/holders?" + qs);
        }
        offset += (d.items || []).length;
        const body = $("#holders-body");
        if (body) body.insertAdjacentHTML("beforeend", holdersRows(nd.items, denom, chain, offset));
        nextParams = nd.next_page_params || null;
        nextStart = nd.next_start != null ? nd.next_start : null;
        updateBtn();
      } catch (e) { banner("Load more failed: " + e.message); }
      moreBtn.disabled = false;
    });
  } catch (e) {
    render(`<div class="card" style="margin-top:22px"><div class="empty">Could not load holders: ${esc(e.message)}<br><a href="#/holders">Retry</a></div></div>`);
  }
}

function transfersRows(items, chain, sym) {
  return (items || []).map((t) => {
    const from = t.from && typeof t.from === "object" ? t.from.hash : t.from;
    const to = t.to && typeof t.to === "object" ? t.to.hash : t.to;
    const ts = t.timestamp ? (typeof t.timestamp === "number" && t.timestamp > 1e12 ? t.timestamp : (typeof t.timestamp === "string" && t.timestamp.includes("T") ? Date.parse(t.timestamp) : t.timestamp * 1000)) : null;
    return `<tr><td>${ts ? esc(new Date(ts).toISOString().replace("T", " ").slice(0, 19)) : "—"}</td><td>${extAddrLink(from, chain)}</td><td>${extAddrLink(to, chain)}</td><td style="text-align:right">${tokenAmt(t.amount)} ${esc(sym)}</td><td style="text-align:right">${usd(t.amount_usd)}</td><td>${extTxLink(t.tx_hash, chain)}</td></tr>`;
  }).join("");
}

async function transfersPage() {
  render(`<div class="card" style="margin-top:22px"><div class="empty"><span class="spinner"></span> Loading transfers…</div></div>`);
  try {
    const s = await loadStats();
    const chain = s.primary_chain;
    const d = await get("/transfers");
    let nextParams = d.next_page_params || null;
    let nextStart = d.next_start != null ? d.next_start : null;
    render(`
    <h1 class="pagehead" style="margin-top:22px">Recent transfers — ${esc(s.symbol)} <span class="muted" style="font-size:15px">(${esc(chain === "tron" ? "Tron" : "Ethereum")} · newest first)</span></h1>
    <div class="card"><div class="card-head">Latest ${esc(s.symbol)} transfers on ${esc(chain === "tron" ? "Tron" : "Ethereum")}</div>
    <table class="tbl"><thead><tr><th>Time (UTC)</th><th>From</th><th>To</th><th style="text-align:right">Amount</th><th style="text-align:right">USD value</th><th>Tx</th></tr></thead>
    <tbody id="transfers-body">${transfersRows(d.items, chain, s.symbol)}</tbody></table>
    <div style="margin-top:12px"><button class="btn loadmore" id="transfers-more">Load more</button></div>
    </div>
    <p class="muted">Source: ${chain === "tron" ? "TronScan keyless API (transfers sorted newest first)" : "Blockscout v2 (Ethereum)"}. Amounts in ${esc(s.symbol)}; USD at the live ${esc(s.symbol)} price (${priceFmt(d.price_usd)}).</p>
    `);
    const moreBtn = $("#transfers-more");
    const updateBtn = () => {
      if (!moreBtn) return;
      const hasMore = chain === "tron" ? nextStart != null : !!nextParams;
      moreBtn.style.display = hasMore ? "" : "none";
    };
    updateBtn();
    if (moreBtn) moreBtn.addEventListener("click", async () => {
      moreBtn.disabled = true;
      try {
        let nd;
        if (chain === "tron") nd = await get("/transfers?start=" + nextStart);
        else {
          const qs = new URLSearchParams(nextParams).toString();
          nd = await get("/transfers?" + qs);
        }
        const body = $("#transfers-body");
        if (body) body.insertAdjacentHTML("beforeend", transfersRows(nd.items, chain, s.symbol));
        nextParams = nd.next_page_params || null;
        nextStart = nd.next_start != null ? nd.next_start : null;
        updateBtn();
      } catch (e) { banner("Load more failed: " + e.message); }
      moreBtn.disabled = false;
    });
  } catch (e) {
    render(`<div class="card" style="margin-top:22px"><div class="empty">Could not load transfers: ${esc(e.message)}<br><a href="#/transfers">Retry</a></div></div>`);
  }
}

async function depegsPage() {
  render(`<div class="card" style="margin-top:22px"><div class="empty"><span class="spinner"></span> Loading depeg history…</div></div>`);
  try {
    const s = await loadStats();
    const d = await get("/depegs");
    render(`
    <h1 class="pagehead" style="margin-top:22px">Depeg history — ${esc(s.symbol)}</h1>
    <p class="muted">A depeg day is a day where the DeFiLlama daily close deviated from $1.00 by more than ${num(d.depeg_threshold_bps)} bps (0.5%). Prices are daily closes, not intraday extremes — intraday depegs larger than the daily close are not shown.</p>
    <div class="statgrid">
      <div class="stat"><div class="stat-label">Depeg days</div><div class="stat-value">${num(d.depeg_days_total)}</div><div class="muted">of ${num(d.history_days)} days in history</div></div>
      <div class="stat"><div class="stat-label">Worst depeg</div><div class="stat-value">${d.worst_depeg ? priceFmt(d.worst_depeg.price) : "—"}</div><div class="muted">${d.worst_depeg ? bpsFmt(d.worst_depeg.deviation_bps) + " on " + dateStr(d.worst_depeg.date) : "none in history"}</div></div>
      <div class="stat"><div class="stat-label">Most recent</div><div class="stat-value">${d.most_recent_depeg ? priceFmt(d.most_recent_depeg.price) : "—"}</div><div class="muted">${d.most_recent_depeg ? bpsFmt(d.most_recent_depeg.deviation_bps) + " on " + dateStr(d.most_recent_depeg.date) : "none in history"}</div></div>
    </div>
    <div class="card"><div class="card-head">All depeg days — newest first</div>
    ${(d.events || []).length ? `<table class="tbl"><thead><tr><th>Date</th><th style="text-align:right">Price</th><th style="text-align:right">Deviation</th></tr></thead><tbody>
      ${(d.events || []).map((e) => `<tr><td>${dateStr(e.date)}</td><td style="text-align:right">${priceFmt(e.price)}</td><td style="text-align:right">${bpsFmt(e.deviation_bps)}</td></tr>`).join("")}
    </tbody></table>` : '<div class="empty">No depeg days in the full price history.</div>'}
    </div>
    <p class="muted">Source: DeFiLlama /stablecoinprices daily closes (${dateStr(d.history_first)} → ${dateStr(d.history_last)}).</p>
    `);
  } catch (e) {
    render(`<div class="card" style="margin-top:22px"><div class="empty">Could not load depeg history: ${esc(e.message)}<br><a href="#/depegs">Retry</a></div></div>`);
  }
}

async function reservesPage() {
  render(`<div class="card" style="margin-top:22px"><div class="empty"><span class="spinner"></span> Loading reserves…</div></div>`);
  try {
    const s = await loadStats();
    const r = await get("/reserves");
    render(`
    <h1 class="pagehead" style="margin-top:22px">Reserves &amp; attestations — ${esc(s.symbol)}</h1>
    <div class="card"><div class="card-head">Issuer &amp; backing</div>
      <div class="detail">
        <div><span class="muted">Issuer:</span> ${esc(r.issuer || "—")}</div>
        <div><span class="muted">Peg mechanism:</span> ${esc(r.peg_mechanism || "—")}</div>
        <div><span class="muted">About:</span> ${esc(r.description || "—")}</div>
        ${r.mint_redeem ? `<div><span class="muted">Mint / redeem:</span> ${esc(r.mint_redeem)}</div>` : ""}
      </div>
    </div>
    <div class="card"><div class="card-head">Attestation &amp; transparency links</div>
      <div class="detail">
        ${(r.attestations || []).map((a) => `<div>· <a href="${esc(a.url)}" target="_blank" rel="noopener">${esc(a.label)}</a></div>`).join("")}
        <div>· <a href="${esc(r.website)}" target="_blank" rel="noopener">Issuer website</a></div>
        ${r.whitepaper ? `<div>· <a href="${esc(r.whitepaper)}" target="_blank" rel="noopener">Whitepaper</a></div>` : ""}
      </div>
      <div class="notice" style="margin-top:12px">${esc(r.note || "")}</div>
    </div>
    <div class="card"><div class="card-head">Market figures <span class="muted">(NOT reserves — labelled market data from CoinGecko)</span></div>
      <div class="detail">
        <div><span class="muted">Market cap:</span> ${usdBig(r.market_figures.market_cap_usd)}</div>
        <div><span class="muted">Total supply:</span> ${tokenAmt(r.market_figures.total_supply)} ${esc(s.symbol)}</div>
        <div><span class="muted">Circulating supply:</span> ${tokenAmt(r.market_figures.circulating_supply)} ${esc(s.symbol)}</div>
        <div><span class="muted">Source:</span> ${esc(r.market_figures.source)}</div>
      </div>
      <div class="notice" style="margin-top:12px">${esc(r.reserve_figures_note || "")}</div>
    </div>
    `);
  } catch (e) {
    render(`<div class="card" style="margin-top:22px"><div class="empty">Could not load reserves: ${esc(e.message)}<br><a href="#/reserves">Retry</a></div></div>`);
  }
}

async function docsPage() {
  render(`<div class="card" style="margin-top:22px"><div class="empty"><span class="spinner"></span> Loading API docs…</div></div>`);
  try {
    const d = await get("/docs");
    render(`
    <h1 class="pagehead" style="margin-top:22px">API Docs — ${esc(d.name || TITLE)} <span class="muted" style="font-size:15px">(${esc(d.symbol || "")} · primary chain ${esc(d.primary_chain || "")})</span></h1>
    <div class="card"><div class="card-head">Endpoints — ${esc(API)}</div>
    ${(d.endpoints || []).map((e) => `<div class="docitem"><span class="method">${esc(e.method)}</span> <span class="mono">${esc(e.path)}</span><div class="muted">${esc(e.description)}</div>${e.params && e.params.length ? `<div class="muted">Params: ${esc(e.params.join(", "))}</div>` : ""}<div><a class="mono" href="${esc(e.example)}" target="_blank" rel="noopener">Try: ${esc(e.example)}</a></div></div>`).join("")}
    </div>
    <p class="muted">All endpoints are GET, keyless, and return JSON. DeFiLlama price history is cached 10 minutes server-side; coin detail 2 minutes.</p>
    `);
  } catch (e) {
    render(`<div class="card" style="margin-top:22px"><div class="empty">Could not load API docs: ${esc(e.message)}<br><a href="#/api-docs">Retry</a></div></div>`);
  }
}

async function searchPage(q) {
  render(`<div class="card" style="margin-top:22px"><div class="empty"><span class="spinner"></span> Searching…</div></div>`);
  try {
    const s = await loadStats();
    const d = await get("/search?q=" + encodeURIComponent(q));
    if (d.type === "holder") {
      render(`<div class="card" style="margin-top:22px"><div class="card-head">Holder — ${esc(s.symbol)} on ${esc(d.chain)}</div>
        <div class="detail"><div><span class="muted">Address:</span> <span class="mono">${esc(d.address)}</span> ${copyBtn(d.address)}</div>
        <div><span class="muted">See it in the holders list:</span> <a href="#/holders">Top holders →</a></div>
        <div><span class="muted">External explorer:</span> ${extAddrLink(d.address, d.chain)}</div></div></div>`);
    } else {
      render(`<div class="card" style="margin-top:22px"><div class="card-head">Transaction — ${esc(s.symbol)} on ${esc(d.chain)}</div>
        <div class="detail"><div><span class="muted">Hash:</span> <span class="mono">${esc(d.hash || d.txid)}</span> ${copyBtn(d.hash || d.txid)}</div>
        <div><span class="muted">External explorer:</span> ${extTxLink(d.hash || d.txid, d.chain)}</div>
        <div><span class="muted">See recent transfers:</span> <a href="#/transfers">Transfers →</a></div></div></div>`);
    }
  } catch (e) {
    render(`<div class="card" style="margin-top:22px"><div class="empty">No match for <span class="mono">${esc(q)}</span>: ${esc(e.message)}<br><a href="#/">Back to home</a></div></div>`);
  }
}

/* ---------------- router ---------------- */
const routes = [
  [/^#\/$/, () => homePage()],
  [/^#\/holders$/, () => holdersPage()],
  [/^#\/transfers$/, () => transfersPage()],
  [/^#\/depegs$/, () => depegsPage()],
  [/^#\/reserves$/, () => reservesPage()],
  [/^#\/api-docs$/, () => docsPage()],
  [/^#\/search\?q=(.*)$/, (m) => searchPage(decodeURIComponent(m[1] || ""))],
];
function route() {
  banner(null);
  clearPoll();
  const h = location.hash || "#/";
  for (const [re, fn] of routes) {
    const m = h.match(re);
    if (m) { fn(m); return; }
  }
  document.title = "Not found — " + TITLE;
  render(`<div class="card" style="margin-top:22px"><div class="empty">Page not found: <span class="mono">${esc(h)}</span><br><a href="#/">Back to home</a></div></div>`);
}
function wireSearch(form, input) {
  if (!form || !input) return;
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const q = input.value.trim();
    if (!q) return;
    location.hash = "#/search?q=" + encodeURIComponent(q);
  });
}

document.addEventListener("DOMContentLoaded", async () => {
  document.title = TITLE + " — Stablecoin Dashboard";
  const badge = document.getElementById("brand-badge");
  if (badge) badge.textContent = CFG.brandLetter || "S";
  const bname = document.getElementById("brand-name");
  if (bname) bname.innerHTML = esc(TITLE.replace(/Scope$/, "")) + '<span class="brand-accent">Scope</span>';
  const fbrand = document.getElementById("footer-brand");
  if (fbrand) fbrand.textContent = TITLE;
  const fav = document.querySelector('link[rel="icon"]');
  if (fav && CFG.brandLetter) {
    fav.href = "data:image/svg+xml," + encodeURIComponent("<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect width='32' height='32' rx='7' fill='%230784c3'/><text x='16' y='22' font-size='17' text-anchor='middle' fill='white' font-family='Arial' font-weight='bold'>" + CFG.brandLetter + "</text></svg>");
  }
  const scopeSel = document.getElementById("scope-select");
  if (scopeSel) {
    scopeSel.innerHTML = `<option value="">${esc(TITLE)}</option>` + SIBLINGS.map((s) => `<option value="${esc(s.url)}">${esc(s.name)}</option>`).join("");
    scopeSel.onchange = () => { if (scopeSel.value) window.location.href = scopeSel.value; };
  }
  const np = document.getElementById("netpill-text");
  if (np) np.textContent = CFG.netpill || "Stablecoin";
  const pill = document.getElementById("netpill");
  if (pill) pill.title = "Data: DeFiLlama · CoinGecko · Blockscout / TronScan";
  wireSearch($("#search-form"), $("#search-input"));
  document.getElementById("view").addEventListener("click", (e) => {
    const b = e.target.closest("[data-copy]");
    if (b) {
      e.preventDefault();
      navigator.clipboard.writeText(b.dataset.copy).then(() => {
        const old = b.textContent; b.textContent = "✓";
        setTimeout(() => { b.textContent = old; }, 1200);
      });
    }
  });
  window.addEventListener("hashchange", route);
  try {
    const s = await loadStats();
    if (np) np.textContent = `${s.symbol} · ${s.primary_chain === "tron" ? "Tron" : "Ethereum"}`;
  } catch (e) { /* net pill keeps its default; the view shows the error */ }
  route();
});
