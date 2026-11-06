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
/* Feature 3 — attestation tracker status badge. */
function trackerBadge(status) {
  if (status === "current") return '<span class="badge ok">Current</span>';
  if (status === "due_soon") return '<span class="badge warn">Due soon</span>';
  if (status === "overdue") return '<span class="badge fail">Overdue</span>';
  if (status === "live") return '<span class="badge ok">Live</span>';
  if (status === "on_chain") return '<span class="badge grey">On-chain</span>';
  if (status === "no_attestation") return '<span class="badge fail">No attestation</span>';
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

function bandBadge(band) {
  if (band === "low") return '<span class="badge ok">Low</span>';
  if (band === "moderate") return '<span class="badge grey">Moderate</span>';
  if (band === "elevated") return '<span class="badge warn">Elevated</span>';
  if (band === "high") return '<span class="badge fail">High</span>';
  return '<span class="badge grey">Unknown</span>';
}
function scoreCell(v) {
  if (v == null) return '<span class="muted">unknown</span>';
  return `<b>${Number(v).toFixed(1)}</b> / 100`;
}
/* Depeg early-warning score card (Par v0, provisional weights).
   The four input sub-scores and their values are always shown with
   the headline score — never a bare number. d may be null if the
   endpoint failed; the home page still renders without it. */
function depegScoreCard(d) {
  if (!d) return `<div class="card"><div class="card-head">Depeg risk</div><div class="empty">Depeg risk score unavailable right now (endpoint failed).</div></div>`;
  const inp = d.inputs || {};
  const rows = [
    ["Redemption quality", inp.redemption_quality, inp.redemption_quality ? `Current deviation ${bpsFmt(inp.redemption_quality.evidence.current_deviation_bps)} · ${num(inp.redemption_quality.evidence.depeg_days_365d)} depeg day(s) in 365d · worst ${bpsFmt(inp.redemption_quality.evidence.worst_depeg_365d_bps)}` : ""],
    ["Reserve liquidity", inp.reserve_liquidity, inp.reserve_liquidity ? `Attestation: ${esc(inp.reserve_liquidity.evidence.cadence || "—")} · ${esc(inp.reserve_liquidity.evidence.auditor || "no auditor")} · last ${esc(inp.reserve_liquidity.evidence.last_attestation || "unverified")}${inp.reserve_liquidity.evidence.days_since_attestation != null ? ` (${num(inp.reserve_liquidity.evidence.days_since_attestation)}d ago)` : ""} — PROXY, not a reserve measurement` : ""],
    ["Secondary-market depth", inp.secondary_market_depth, inp.secondary_market_depth ? `Near-par tracked liquidity ${usdBig(inp.secondary_market_depth.evidence.near_par_liquidity_usd)} (${inp.secondary_market_depth.evidence.liquidity_to_supply_pct != null ? inp.secondary_market_depth.evidence.liquidity_to_supply_pct + "%" : "—"} of supply) · top pair ${inp.secondary_market_depth.evidence.top_pair ? inp.secondary_market_depth.evidence.top_pair.share_of_near_par_pct + "%" : "—"} of near-par liquidity` : ""],
    ["Issuer / operational", inp.issuer_operational, inp.issuer_operational ? `Disclosure: ${esc(inp.issuer_operational.evidence.cadence || "—")} · ${esc(inp.issuer_operational.evidence.auditor || "no auditor")} · ${num(inp.issuer_operational.evidence.depeg_days_total)} depeg day(s) in full history` : ""],
  ];
  return `<div class="card"><div class="card-head">Depeg risk — early-warning score <span class="muted">(Par v0, provisional weights — UNCONFIRMED)</span></div>
    <div style="padding:2px 0 8px">
      <div style="font-size:20px">Score: ${d.score != null ? `<b>${Number(d.score).toFixed(1)}</b> / 100` : "—"} ${bandBadge(d.band)}</div>
      <div class="muted">Price ${priceFmt(d.price_usd)} (${bpsFmt(d.deviation_bps)}) · supply ${usdBig(d.supply_usd)}${d.inputs_unknown && d.inputs_unknown.length ? ` · unknown inputs (weight redistributed): ${esc(d.inputs_unknown.join(", "))}` : ""}</div>
    </div>
    <table class="tbl" style="margin-top:10px"><thead><tr><th>Input</th><th style="text-align:right">Weight</th><th style="text-align:right">Sub-score</th><th>Evidence</th></tr></thead><tbody>
    ${rows.map(([label, v, ev]) => `<tr><td>${esc(label)}</td><td style="text-align:right">${v ? Math.round(v.weight * 100) + "%" : "—"}</td><td style="text-align:right">${v ? scoreCell(v.score) : "—"}</td><td class="muted">${ev || '<span class="muted">unknown — could not be measured; weight redistributed to the other inputs</span>'}</td></tr>`).join("")}
    </tbody></table>
    <div class="notice" style="margin-top:12px">${esc(d.methodology || "")}</div>
  </div>`;
}

/* Peg guard at quote time (phase 2, P3) — the same guard the
   walletscope quote screen runs (GET /api/walletscope/quote/guard),
   here for this coin as "paying an invoice in this coin". Verdict
   ok / warn / block / unknown with thresholds stated, never hidden.
   Block: |deviation| > 50 bps or depeg-risk score >= 75. Warn:
   |deviation| > 10 bps or score >= 50. On warn/block the lowest-
   depeg-score coin among the other 7 scopes is suggested, with its
   own peg state. d may be null (endpoint failed). */
function quoteGuardCard(g) {
  if (!g) return `<div class="card"><div class="card-head">Peg guard — paying in ${esc(TITLE)}</div><div class="empty">Guard unavailable right now (endpoint failed).</div></div>`;
  const v = String(g.verdict || "unknown").toLowerCase();
  const badgeCls = { ok: "ok", warn: "warn", block: "fail", unknown: "grey" }[v] || "grey";
  const vLabel = {
    ok: "OK — safe to pay in this coin",
    warn: "WARN — review before paying in this coin",
    block: "BLOCK — do not pay in this coin",
    unknown: "UNKNOWN — peg could not be verified",
  }[v] || v;
  const ds = g.depeg_score || {};
  const peg = g.peg || {};
  const alt = g.alternative;
  let altHtml = "";
  if (alt) {
    const sib = SIBLINGS.find((s) => s.name === alt.symbol + " Scope");
    altHtml = `<div style="margin-top:8px">Nearest stable alternative: <b>${esc(alt.symbol)}</b> — depeg-risk score ${alt.score != null ? Number(alt.score).toFixed(1) : "—"} (${esc(alt.band || "—")}), price ${priceFmt(alt.price_usd)} (${bpsFmt(alt.deviation_bps)})${sib ? ` · <a href="${esc(sib.url)}" target="_blank" rel="noopener">${esc(alt.symbol)} Scope →</a>` : ""}<br><span class="muted" style="font-size:12px">${esc(alt.note || "")}</span></div>`;
  } else if (g.alternative_note) {
    altHtml = `<div class="muted" style="margin-top:8px;font-size:12px">${esc(g.alternative_note)}</div>`;
  }
  const th = g.thresholds || {};
  return `<div class="card" style="border-left:4px solid ${v === "ok" ? "var(--green)" : v === "warn" ? "var(--amber)" : v === "block" ? "var(--red)" : "var(--muted)"}">
    <div class="card-head">🛡️ Peg guard — paying in ${esc(g.symbol || "")} <span class="spacer"></span><span class="badge ${badgeCls}">${esc(vLabel)}</span></div>
    <div style="padding:12px 16px">
      <div style="margin:3px 0">Price <b>${priceFmt(peg.price_usd)}</b> (${bpsFmt(peg.deviation_bps)} from $1.00 · ${esc(peg.peg_health || "—")})${peg.price_source ? ` · source: ${esc(peg.price_source)}` : ""}${ds.score != null ? ` · depeg-risk score <b>${Number(ds.score).toFixed(1)}</b> / 100 (${esc(ds.band || "—")})` : " · depeg-risk score unavailable"}</div>
      ${(g.verdict_reasons || []).map((r) => `<div style="margin:3px 0">${esc(r)}</div>`).join("")}
      ${altHtml}
      <div class="muted" style="margin-top:8px;font-size:12px">Thresholds — block: ${esc(th.block || "—")} · warn: ${esc(th.warn || "—")} · ok: ${esc(th.ok || "—")}</div>
      <div class="muted" style="margin-top:4px;font-size:12px">Guard is a screening signal at quote time, not investment advice. Peg score: Par v0 provisional weights (UNCONFIRMED) — full score and evidence in the Depeg risk card above.</div>
    </div>
  </div>`;
}

/* Issuer risk card (feature 11) — one composite 0-100 score per coin
   (higher = riskier), built ONLY by reusing features 1, 3, 4, 6, 8
   and 10, with the six input sub-scores and their values always
   shown with the headline score — never a bare number. d may be
   null (still loading — the home page fetches /issuer-risk
   separately because it reuses the slow freeze/activity scans) or
   {error:true} if the fetch failed. */
function issuerRiskCard(d) {
  if (!d) return `<div class="card"><div class="card-head">Issuer risk</div><div class="empty"><span class="spinner"></span> Loading issuer risk data…</div></div>`;
  if (d.error) return `<div class="card"><div class="card-head">Issuer risk</div><div class="empty">Issuer risk data unavailable right now (endpoint failed).</div></div>`;
  const inp = d.inputs || {};
  const fmtFee = (v) => (v == null ? "—" : "$" + Number(v).toFixed(4));
  const fmtFinality = (s) => {
    if (s == null) return "—";
    const x = Number(s);
    if (x < 60) return "~" + x + "s";
    if (x < 3600) return "~" + Math.round(x / 60) + " min";
    return "~" + (x / 3600).toFixed(1) + "h";
  };
  const rows = [
    ["Depeg risk", inp.depeg_risk, inp.depeg_risk ? `Feature-1 score ${inp.depeg_risk.evidence.depeg_risk_score != null ? Number(inp.depeg_risk.evidence.depeg_risk_score).toFixed(1) : "—"} / 100 · price ${priceFmt(inp.depeg_risk.evidence.price_usd)} (${bpsFmt(inp.depeg_risk.evidence.current_deviation_bps)}) · ${num(inp.depeg_risk.evidence.depeg_days_365d)} depeg day(s) in 365d · worst ${bpsFmt(inp.depeg_risk.evidence.worst_depeg_365d_bps)}` : ""],
    ["Reserve transparency", inp.reserve_transparency, inp.reserve_transparency ? `${esc(inp.reserve_transparency.evidence.status_label || inp.reserve_transparency.evidence.status || "—")} · ${esc(inp.reserve_transparency.evidence.auditor || "no auditor")}${inp.reserve_transparency.evidence.auditor_tier ? ` (${esc(inp.reserve_transparency.evidence.auditor_tier)})` : ""} · last attestation ${esc(inp.reserve_transparency.evidence.last_attestation || "unverified")}${inp.reserve_transparency.evidence.days_since_attestation != null ? ` (${num(inp.reserve_transparency.evidence.days_since_attestation)}d ago)` : ""}${inp.reserve_transparency.evidence.reserve_to_outstanding_ratio != null ? ` · dated reserves/outstanding ${Number(inp.reserve_transparency.evidence.reserve_to_outstanding_ratio).toFixed(4)}x (${esc(inp.reserve_transparency.evidence.reserve_figures_as_of || "")})` : " · no dated reserve figures verified"} — TRANSPARENCY, not a live reserve measurement` : ""],
    ["Issuance direction", inp.issuance_direction, inp.issuance_direction ? `${inp.issuance_direction.evidence.flat_supply ? "<b>Supply frozen</b> — no net mint/burn in 90d" : `Net 30d ${inp.issuance_direction.evidence.net_30d_usd != null ? (Number(inp.issuance_direction.evidence.net_30d_usd) >= 0 ? "+" : "−") + usdBig(Math.abs(inp.issuance_direction.evidence.net_30d_usd)) : "—"} (${pctFmt(inp.issuance_direction.evidence.net_30d_pct_of_supply)}) · net 90d ${inp.issuance_direction.evidence.net_90d_usd != null ? (Number(inp.issuance_direction.evidence.net_90d_usd) >= 0 ? "+" : "−") + usdBig(Math.abs(inp.issuance_direction.evidence.net_90d_usd)) : "—"} (${pctFmt(inp.issuance_direction.evidence.net_90d_pct_of_supply)}) · demand: ${esc(inp.issuance_direction.evidence.demand_direction || "—")}`}${inp.issuance_direction.evidence.trend_label ? ` · ${esc(inp.issuance_direction.evidence.trend_label)}` : ""}` : ""],
    ["Concentration", inp.concentration, inp.concentration ? `Top 10 holders ${inp.concentration.evidence.top_10_share_pct != null ? Number(inp.concentration.evidence.top_10_share_pct).toFixed(2) + "%" : "—"} of ${esc(inp.concentration.evidence.primary_chain === "tron" ? "Tron" : "Ethereum")} supply (top 1: ${inp.concentration.evidence.top_1_share_pct != null ? Number(inp.concentration.evidence.top_1_share_pct).toFixed(2) + "%" : "—"} · top 50: ${inp.concentration.evidence.top_50_share_pct != null ? Number(inp.concentration.evidence.top_50_share_pct).toFixed(2) + "%" : "—"}) · exchange-identified ${inp.concentration.evidence.exchange_share_pct != null ? Number(inp.concentration.evidence.exchange_share_pct).toFixed(2) + "%" : "—"} (LOWER BOUND, ${num(inp.concentration.evidence.exchange_wallets_identified)} wallet(s) in top 50)` : ""],
    ["Freeze exposure", inp.freeze_exposure, inp.freeze_exposure ? `${inp.freeze_exposure.evidence.can_freeze ? "<b>Issuer can freeze balances</b>" : "<b>No freeze function</b>"}${(inp.freeze_exposure.evidence.probe_functions_found || []).length ? ` (${esc(inp.freeze_exposure.evidence.probe_functions_found.join(", "))})` : ""} · ${num(inp.freeze_exposure.evidence.events_total)} freeze event(s) in ${num(inp.freeze_exposure.evidence.events_window_days)}d window · frozen ${usdBig(inp.freeze_exposure.evidence.frozen_supply_usd)}${inp.freeze_exposure.evidence.frozen_share_of_primary_supply_pct != null ? ` (${Number(inp.freeze_exposure.evidence.frozen_share_of_primary_supply_pct).toFixed(4)}% of primary supply — LOWER-BOUND ESTIMATE)` : " (no frozen supply measured)"} · black funds destroyed ${usdBig(inp.freeze_exposure.evidence.destroyed_total_usd)} · OFAC SDN matches ${num(inp.freeze_exposure.evidence.sanctions_matches_total)}` : ""],
    ["Adoption", inp.adoption, inp.adoption ? `Chain coverage ${inp.adoption.evidence.chain_coverage_score != null ? Number(inp.adoption.evidence.chain_coverage_score).toFixed(1) : "—"} / 100 (${num(inp.adoption.evidence.material_chains)} material chain(s)) · fees/finality ${inp.adoption.evidence.fees_finality_score != null ? Number(inp.adoption.evidence.fees_finality_score).toFixed(1) : "—"} / 100 (typical transfer ${fmtFee(inp.adoption.evidence.typical_transfer_fee_usd)} · finality ${fmtFinality(inp.adoption.evidence.finality_seconds)}) — feature-8 legs only, peg/liquidity/freeze excluded to avoid double-counting` : ""],
  ];
  return `<div class="card"><div class="card-head">Issuer risk <span class="muted">(composite of features 1, 3, 4, 6, 8 &amp; 10 · 0–100, higher = riskier · weights: depeg 25% · reserve transparency 25% · issuance 15% · concentration 15% · freeze exposure 15% · adoption 5%)</span></div>
    <div style="padding:2px 0 8px">
      <div style="font-size:20px">Score: ${d.score != null ? `<b>${Number(d.score).toFixed(1)}</b> / 100` : "—"} ${bandBadge(d.band)}</div>
      <div class="muted">Price ${priceFmt(d.price_usd)} (${bpsFmt(d.deviation_bps)}) · supply ${usdBig(d.supply_usd)}${d.inputs_unknown && d.inputs_unknown.length ? ` · unknown inputs (weight redistributed): ${esc(d.inputs_unknown.join(", "))}` : ""}</div>
    </div>
    <table class="tbl" style="margin-top:10px"><thead><tr><th>Input</th><th style="text-align:right">Weight</th><th style="text-align:right">Sub-score</th><th>Evidence</th></tr></thead><tbody>
    ${rows.map(([label, v, ev]) => `<tr><td>${esc(label)}</td><td style="text-align:right">${v ? Math.round(v.weight * 100) + "%" : "—"}</td><td style="text-align:right">${v ? scoreCell(v.score) : "—"}</td><td class="muted">${ev || '<span class="muted">unknown — could not be measured; weight redistributed to the other inputs</span>'}</td></tr>`).join("")}
    </tbody></table>
    <div class="notice" style="margin-top:12px">${esc(d.methodology || "")}</div>
  </div>`;
}

function readinessBadge(band) {
  if (band === "ready") return '<span class="badge ok">Ready</span>';
  if (band === "usable") return '<span class="badge grey">Usable</span>';
  if (band === "limited") return '<span class="badge warn">Limited</span>';
  if (band === "not_ready") return '<span class="badge fail">Not ready</span>';
  return '<span class="badge grey">Unknown</span>';
}
/* Payments readiness card (feature 8) — one 0-100 score for a
   company choosing a stablecoin for payments, with the five input
   sub-scores and their values always shown with the headline score
   — never a bare number. d may be null (still loading — the home
   page fetches /payments-readiness separately because its activity
   scan is slow) or {error:true} if the fetch failed. */
function paymentsReadinessCard(d) {
  if (!d) return `<div class="card"><div class="card-head">Payments readiness</div><div class="empty"><span class="spinner"></span> Loading payments readiness data…</div></div>`;
  if (d.error) return `<div class="card"><div class="card-head">Payments readiness</div><div class="empty">Payments readiness data unavailable right now (endpoint failed).</div></div>`;
  const inp = d.inputs || {};
  const fmtFee = (v) => (v == null ? "—" : "$" + Number(v).toFixed(4));
  const fmtFinality = (s) => {
    if (s == null) return "—";
    const x = Number(s);
    if (x < 60) return "~" + x + "s";
    if (x < 3600) return "~" + Math.round(x / 60) + " min";
    return "~" + (x / 3600).toFixed(1) + "h";
  };
  const freezeEv = (v) => {
    if (!v) return "";
    const fns = (v.evidence.freeze_functions_found || []);
    const base = fns.length
      ? `Freeze/blacklist function present (${esc(fns.join(", "))})${v.evidence.pause_function_found ? " · pause also present" : ""}`
      : v.evidence.pause_function_found
        ? "No freeze/blacklist function · pause function present"
        : "No freeze/blacklist or pause function found on the primary-chain contract";
    return base + " · freeze events tracked below (Freeze &amp; blacklist)";
  };
  const rows = [
    ["Peg stability", inp.peg_stability, inp.peg_stability ? `Depeg risk ${inp.peg_stability.evidence.depeg_risk_score != null ? Number(inp.peg_stability.evidence.depeg_risk_score).toFixed(1) : "—"} / 100 · price ${priceFmt(inp.peg_stability.evidence.price_usd)} (${bpsFmt(inp.peg_stability.evidence.current_deviation_bps)}) · ${num(inp.peg_stability.evidence.depeg_days_365d)} depeg day(s) in 365d · worst ${bpsFmt(inp.peg_stability.evidence.worst_depeg_365d_bps)}` : ""],
    ["Liquidity", inp.liquidity, inp.liquidity ? `Near-par tracked liquidity ${usdBig(inp.liquidity.evidence.near_par_liquidity_usd)} (${inp.liquidity.evidence.liquidity_to_supply_pct != null ? inp.liquidity.evidence.liquidity_to_supply_pct + "%" : "—"} of supply) · daily transfer volume ${usdBig(inp.liquidity.evidence.daily_transfer_volume_usd)}${inp.liquidity.evidence.daily_volume_basis ? ` (${esc(inp.liquidity.evidence.daily_volume_basis)})` : ""}` : ""],
    ["Chain coverage", inp.chain_coverage, inp.chain_coverage ? `${num(inp.chain_coverage.evidence.material_chains)} material chain(s) (≥1% of supply) of ${num(inp.chain_coverage.evidence.chain_count)} chains · ${inp.chain_coverage.evidence.material_share_of_total_pct != null ? inp.chain_coverage.evidence.material_share_of_total_pct.toFixed(1) + "%" : "—"} of supply on material chains${(inp.chain_coverage.evidence.top_chains || []).length ? " · top: " + inp.chain_coverage.evidence.top_chains.map((c) => `${esc(c.chain)} ${c.share_of_total_pct != null ? c.share_of_total_pct.toFixed(1) + "%" : "—"}`).join(", ") : ""}` : ""],
    ["Fees / finality", inp.fees_finality, inp.fees_finality ? `Typical transfer ${fmtFee(inp.fees_finality.evidence.typical_transfer_fee_usd)} on ${esc(d.primary_chain === "tron" ? "Tron" : "Ethereum")} (primary chain only) · finality ${fmtFinality(inp.fees_finality.evidence.finality_seconds)}${inp.fees_finality.evidence.gas_price_gwei != null ? ` · gas ${Number(inp.fees_finality.evidence.gas_price_gwei).toFixed(2)} gwei` : ""}${inp.fees_finality.evidence.energy_price_sun != null ? ` · energy ${num(inp.fees_finality.evidence.energy_price_sun)} sun/unit` : ""} — fee is an ESTIMATE (see note below)` : ""],
    ["Freeze risk", inp.freeze_risk, freezeEv(inp.freeze_risk)],
  ];
  return `<div class="card"><div class="card-head">Payments readiness <span class="muted">(for a company choosing a stablecoin for payments · weights: peg 30% · liquidity 25% · chain coverage 20% · fees/finality 15% · freeze risk 10%)</span></div>
    <div style="padding:2px 0 8px">
      <div style="font-size:20px">Score: ${d.score != null ? `<b>${Number(d.score).toFixed(1)}</b> / 100` : "—"} ${readinessBadge(d.band)}</div>
      <div class="muted">Supply ${usdBig(d.supply_usd)} · primary chain ${esc(d.primary_chain === "tron" ? "Tron" : "Ethereum")}${d.inputs_unknown && d.inputs_unknown.length ? ` · unknown inputs (weight redistributed): ${esc(d.inputs_unknown.join(", "))}` : ""}</div>
    </div>
    <table class="tbl" style="margin-top:10px"><thead><tr><th>Input</th><th style="text-align:right">Weight</th><th style="text-align:right">Sub-score</th><th>Evidence</th></tr></thead><tbody>
    ${rows.map(([label, v, ev]) => `<tr><td>${esc(label)}</td><td style="text-align:right">${v ? Math.round(v.weight * 100) + "%" : "—"}</td><td style="text-align:right">${v ? scoreCell(v.score) : "—"}</td><td class="muted">${ev || '<span class="muted">unknown — could not be measured; weight redistributed to the other inputs</span>'}</td></tr>`).join("")}
    </tbody></table>
    <div class="notice" style="margin-top:12px">${esc(d.methodology || "")}</div>
  </div>`;
}

/* Peg-stability leaderboard card (feature 2) — this coin's rank and
   its row against the other seven scopes, from this app's own
   /peg-leaderboard endpoint. lb may be null if the endpoint failed;
   the home page still renders without it. Every figure carries its
   window in the column header. */
function pegLeaderboardCard(lb) {
  if (!lb) return `<div class="card"><div class="card-head">Peg stability vs peers</div><div class="empty">Peg leaderboard unavailable right now (endpoint failed).</div></div>`;
  const rows = lb.rows || [];
  const mine = rows.find((r) => r.is_this_scope);
  const offCell = (o) => (o && o.days != null) ? `${num(o.days)}${o.share_pct != null ? ` (${o.share_pct.toFixed(1)}%)` : ""}` : "—";
  const worstCell = (bps, date) => (bps != null) ? `${bpsFmt(bps)}<br><span class="muted">${dateStr(date)}</span>` : "—";
  const recCell = (e) => {
    if (!e || e.count == null) return "—";
    const med = e.median_recovery_days != null ? `${num(e.median_recovery_days)}d` : "—";
    const mx = e.max_recovery_days != null ? `${num(e.max_recovery_days)}d` : "—";
    return `${num(e.count)} ep · med ${med} · max ${mx}${e.ongoing ? ` · ${num(e.ongoing)} ongoing` : ""}`;
  };
  return `<div class="card"><div class="card-head">Peg stability vs peers <span class="muted">(all 8 stablecoins · DeFiLlama daily closes)</span></div>
    <div style="padding:2px 0 8px">${mine ? `<div style="font-size:16px">${esc(mine.symbol)} ranks <b>#${num(mine.rank)}</b> of ${num(rows.length)} — ${num(mine.off_peg_365d.days)} off-peg day(s) in 365d, worst ${bpsFmt(mine.max_dev_alltime_bps)} all-time</div>` : ""}<div class="muted">${esc(lb.ranking_rule || "")}</div></div>
    <div style="overflow-x:auto"><table class="tbl"><thead><tr><th>#</th><th>Coin</th><th style="text-align:right">Now</th><th style="text-align:right">Worst 30d</th><th style="text-align:right">Worst all-time</th><th style="text-align:right">Off-peg 30d</th><th style="text-align:right">Off-peg 90d</th><th style="text-align:right">Off-peg 365d</th><th>Episodes 365d (recovery)</th><th style="text-align:right">Vol 30d</th></tr></thead><tbody>
    ${rows.map((r) => r.error ? `<tr${r.is_this_scope ? ' style="background:#eef6fb;font-weight:700"' : ""}><td>${r.rank != null ? num(r.rank) : "—"}</td><td><b>${esc(r.symbol)}</b> <span class="muted">${esc(r.name || "")}</span></td><td colspan="8" class="muted">Unavailable: ${esc(r.error)}</td></tr>`
      : `<tr${r.is_this_scope ? ' style="background:#eef6fb;font-weight:700"' : ""}><td>${r.rank != null ? num(r.rank) : "—"}</td><td><b>${esc(r.symbol)}</b> <span class="muted">${esc(r.name || "")}</span></td><td style="text-align:right">${bpsFmt(r.deviation_bps)}</td><td style="text-align:right">${worstCell(r.max_dev_30d_bps, r.max_dev_30d_date)}</td><td style="text-align:right">${worstCell(r.max_dev_alltime_bps, r.max_dev_alltime_date)}</td><td style="text-align:right">${offCell(r.off_peg_30d)}</td><td style="text-align:right">${offCell(r.off_peg_90d)}</td><td style="text-align:right">${offCell(r.off_peg_365d)}</td><td>${recCell(r.episodes_365d)}</td><td style="text-align:right">${r.volatility_30d_pct != null ? Number(r.volatility_30d_pct).toFixed(2) + "%/day" : "—"}</td></tr>`).join("")}
    </tbody></table></div>
    <div class="notice" style="margin-top:12px">${esc(lb.episode_rule || "")}<br>${esc(lb.volatility_note || "")}<br>${esc(lb.caveat || "")}</div>
  </div>`;
}

/* Mint/burn monitor card (feature 4) — net issuance from the
   DeFiLlama daily supply series + large on-chain mint/burn events on
   the primary chain. mb may be null if the endpoint failed; the home
   page still renders without it. */
function mintBurnCard(mb) {
  if (!mb) return `<div class="card"><div class="card-head">Mint / burn</div><div class="empty">Mint/burn monitor unavailable right now (endpoint failed).</div></div>`;
  const ni = mb.net_issuance || {};
  const w = (k) => ni[k] || {};
  const netUsd = (v) => (v == null || v === "") ? "—" : (v < 0 ? "−" : "+") + usdBig(Math.abs(v));
  const winRow = (label, x) => `<tr><td>${label}</td><td style="text-align:right">${netUsd(x.net_usd)}</td><td style="text-align:right">${x.pct_of_supply != null ? pctFmt(x.pct_of_supply) : "—"}</td><td class="muted">${x.start_date ? dateStr(x.start_date) + " → " + dateStr(x.end_date) : "—"}${x.partial ? ` (covers ${num(x.window_days)}d, not ${num(x.window_days_requested)}d — series short or gapped)` : ""}</td></tr>`;
  const lm = ni.largest_mint_day, lb = ni.largest_burn_day;
  const tr = ni.trend_30d_vs_prior_30d || {};
  const le = mb.large_events || {};
  const evs = le.events || [];
  const ds = mb.demand_signal || {};
  const dirBadge = (d) => d === "mint" ? '<span class="badge ok">Mint</span>' : d === "burn" ? '<span class="badge fail">Burn</span>' : '<span class="badge grey">—</span>';
  const chain = le.primary_chain || "ethereum";
  return `<div class="card"><div class="card-head">Mint / burn <span class="muted">(net issuance from DeFiLlama daily supply · large on-chain events on ${esc(chain === "tron" ? "Tron" : "Ethereum")} only)</span></div>
    <table class="tbl"><thead><tr><th>Window</th><th style="text-align:right">Net issuance</th><th style="text-align:right">% of supply</th><th>Period</th></tr></thead><tbody>
    ${winRow("7 days", w("window_7d"))}
    ${winRow("30 days", w("window_30d"))}
    ${winRow("90 days", w("window_90d"))}
    </tbody></table>
    <div class="detail" style="margin-top:10px">
      <div class="dk">Largest single-day net mint (90d)</div><div class="dv">${lm ? `${netUsd(lm.delta_usd)} on ${dateStr(lm.date)}` : "—"}</div>
      <div class="dk">Largest single-day net burn (90d)</div><div class="dv">${lb ? `${netUsd(lb.delta_usd)} on ${dateStr(lb.date)}` : "—"}</div>
      <div class="dk">Trend — 30d vs prior 30d</div><div class="dv">${tr.label ? `${esc(tr.label)} (${netUsd(tr.net_30d_usd)} vs ${netUsd(tr.net_prior_30d_usd)})` : "—"}</div>
      ${ni.flat_supply ? `<div class="dk">Supply</div><div class="dv"><b>Flat over the 90d window</b> — no net mint/burn (frozen supply).</div>` : ""}
    </div>
    <div class="notice" style="margin-top:12px">${esc(ni.note || "")}</div>
    <div class="card-head" style="margin-top:16px">Large on-chain mint / burn events <span class="muted">(≥ ${usdBig(mb.threshold_usd)} — max($1M, 0.1% of supply) · latest ${num(le.cap || 20)})</span></div>
    ${le.status_note ? `<div class="notice" style="margin-top:8px">${esc(le.status_note)}</div>` : ""}
    ${le.status === "unavailable" ? '<div class="empty">No on-chain mint/burn events could be retrieved for this coin.</div>'
      : evs.length ? `<div style="overflow-x:auto"><table class="tbl" style="margin-top:10px"><thead><tr><th>Time (UTC)</th><th>Direction</th><th style="text-align:right">Amount</th><th style="text-align:right">USD value</th><th>Tx</th><th>From</th><th>To</th></tr></thead><tbody>
      ${evs.map((e) => `<tr><td>${dateTimeStr(e.timestamp)}</td><td>${dirBadge(e.direction)}</td><td style="text-align:right">${tokenAmt(e.amount)} ${esc(mb.symbol || "")}</td><td style="text-align:right">${usd(e.amount_usd)}</td><td>${extTxLink(e.tx_hash, chain)}</td><td>${extAddrLink(e.from, chain)}</td><td>${extAddrLink(e.to, chain)}</td></tr>`).join("")}
      </tbody></table></div>`
      : `<div class="empty">No single event ≥ ${usdBig(mb.threshold_usd)} in the last ${num(le.scanned_zero_address_transfers)} zero-address transfers scanned${le.scan_earliest ? ` (back to ${dateStr(le.scan_earliest)})` : ""}.</div>`}
    <div class="muted" style="margin-top:8px">${esc(le.chain_scope_note || "")}${evs.length ? ` Scanned ${num(le.scanned_zero_address_transfers)} zero-address transfers${le.scan_earliest ? ` back to ${dateStr(le.scan_earliest)}` : ""}; showing latest ${num(evs.length)}.` : ""}${mb.price_assumed_1_usd ? " USD values assume $1.00 (no live price available)." : ""}</div>
    <div class="card-head" style="margin-top:16px">Issuance as a demand signal</div>
    <div style="margin-top:6px">${esc(ds.summary || "")}</div>
    <div class="notice" style="margin-top:12px">${esc(ds.caveat || "")}</div>
  </div>`;
}

/* Whale & exchange exposure card (feature 6) — holder concentration
   (top-1/10/50 share of primary-chain supply), exchange wallets
   identified among the top 50 by a source name tag (lower bound), and
   large recent transfers above the stated threshold. w may be null
   (still loading — the home page fetches /whales separately because
   its transfer scan is slow) or {error:true} if the fetch failed. */
function whalesCard(w) {
  if (!w) return `<div class="card"><div class="card-head">Whales &amp; exchanges</div><div class="empty"><span class="spinner"></span> Loading whale &amp; exchange data…</div></div>`;
  if (w.error) return `<div class="card"><div class="card-head">Whales &amp; exchanges</div><div class="empty">Whale &amp; exchange data unavailable right now (endpoint failed).</div></div>`;
  const c = w.concentration || {};
  const chain = w.primary_chain === "tron" ? "Tron" : "Ethereum";
  const top = w.top_holders || [];
  const ex = w.exchanges || {};
  const byEx = ex.by_exchange || [];
  const lt = w.large_transfers || {};
  const lts = lt.items || [];
  const shareCell = (v) => (v != null ? Number(v).toFixed(2) + "%" : "—");
  return `<div class="card"><div class="card-head">Whales &amp; exchanges <span class="muted">(top holders on ${esc(chain)} · concentration is share of ${esc(chain)} supply, not all-chain supply)</span></div>
    <div class="detail" style="margin-top:6px">
      <div class="dk">Top 1 holder</div><div class="dv"><b>${shareCell(c.top_1_share_pct)}</b> of ${esc(chain)} supply</div>
      <div class="dk">Top 10 holders</div><div class="dv"><b>${shareCell(c.top_10_share_pct)}</b> of ${esc(chain)} supply</div>
      <div class="dk">Top 50 holders</div><div class="dv"><b>${shareCell(c.top_50_share_pct)}</b> of ${esc(chain)} supply</div>
      <div class="dk">Holders counted</div><div class="dv">${num(c.holders_counted)}${w.holders_total != null ? ` of ${num(w.holders_total)} total holders` : ""}${w.holders_total_note ? ` <span class="muted">(${esc(w.holders_total_note)})</span>` : ""}</div>
      <div class="dk">${esc(chain)} supply (denominator)</div><div class="dv">${usdBig(c.primary_chain_supply_usd)}${w.primary_chain_share_pct != null ? ` · ${Number(w.primary_chain_share_pct).toFixed(1)}% of total ${usdBig(w.supply_usd)}` : ""}</div>
    </div>
    <div class="notice" style="margin-top:12px">${esc(c.denominator_note || "")} ${esc(c.holders_counted_note || "")}</div>
    <div class="card-head" style="margin-top:16px">Top 10 holders</div>
    ${top.length ? `<div style="overflow-x:auto"><table class="tbl" style="margin-top:10px"><thead><tr><th>#</th><th>Holder</th><th style="text-align:right">Balance (${esc(w.symbol || "")})</th><th style="text-align:right">USD value</th><th style="text-align:right">Share</th></tr></thead><tbody>
    ${top.map((h) => `<tr><td>${num(h.rank)}</td><td>${extAddrLink(h.address, w.primary_chain)}${h.label ? ` <span class="badge grey">${esc(h.label)}</span>` : ""}${h.is_contract ? ' <span class="badge grey" title="Contract">C</span>' : ""}${h.exchange ? ` <span class="badge ok" title="Identified as an exchange wallet by the source tag: ${esc(h.exchange_tag || "")}">${esc(h.exchange)}</span>` : ""} ${copyBtn(h.address || "")}</td><td style="text-align:right">${tokenAmt(h.balance)}</td><td style="text-align:right">${usd(h.balance_usd)}</td><td style="text-align:right">${shareCell(h.share_pct)}</td></tr>`).join("")}
    </tbody></table></div>
    <div class="muted" style="margin-top:8px"><a href="#/holders">Full top-holders list →</a></div>`
    : '<div class="empty">No holder data returned for this coin.</div>'}
    <div class="card-head" style="margin-top:16px">Exchange exposure — identified wallets in the top 50 <span class="muted">(lower bound, not a total)</span></div>
    ${byEx.length ? `<div style="overflow-x:auto"><table class="tbl" style="margin-top:10px"><thead><tr><th>Exchange</th><th style="text-align:right">Wallets (in top 50)</th><th style="text-align:right">Balance</th><th style="text-align:right">Share of ${esc(chain)} supply</th></tr></thead><tbody>
    ${byEx.map((r) => `<tr><td><b>${esc(r.exchange)}</b></td><td style="text-align:right">${num(r.wallets)}</td><td style="text-align:right">${usdBig(r.balance_usd)}</td><td style="text-align:right">${shareCell(r.share_pct)}</td></tr>`).join("")}
    <tr><td><b>Total identified</b></td><td style="text-align:right">${num(ex.wallets_identified)}</td><td style="text-align:right"><b>${usdBig(ex.total_balance_usd)}</b></td><td style="text-align:right"><b>${shareCell(ex.total_share_pct)}</b></td></tr>
    </tbody></table></div>
    <div style="overflow-x:auto"><table class="tbl" style="margin-top:10px"><thead><tr><th>#</th><th>Exchange</th><th>Wallet</th><th>Source tag (verbatim)</th><th style="text-align:right">Balance</th></tr></thead><tbody>
    ${(ex.identified || []).map((r) => `<tr><td>${num(r.rank)}</td><td>${esc(r.exchange)}</td><td>${extAddrLink(r.address, w.primary_chain)} ${copyBtn(r.address || "")}</td><td class="muted">${esc(r.matched_tag || "")} <span class="muted">(${esc(r.attribution_source || "")})</span></td><td style="text-align:right">${usd(r.balance_usd)}</td></tr>`).join("")}
    </tbody></table></div>`
    : `<div class="empty">${esc(ex.empty_note || "No exchange wallets identified among the top 50 holders.")}</div>`}
    <div class="notice" style="margin-top:12px">${esc(ex.scope_note || "")}</div>
    <div class="card-head" style="margin-top:16px">Large transfers <span class="muted">(≥ ${usdBig(lt.threshold_usd)} — ${esc(lt.threshold_rule || "")} · latest ${num(lt.cap || 10)})</span></div>
    ${lt.status_note ? `<div class="notice" style="margin-top:8px">${esc(lt.status_note)}</div>` : ""}
    ${lt.status === "unavailable" ? '<div class="empty">No large transfers could be scanned for this coin.</div>'
      : lts.length ? `<div style="overflow-x:auto"><table class="tbl" style="margin-top:10px"><thead><tr><th>Time (UTC)</th><th>From</th><th>To</th><th style="text-align:right">Amount</th><th style="text-align:right">USD value</th><th>Tx</th></tr></thead><tbody>
      ${lts.map((e) => `<tr><td>${dateTimeStr(e.timestamp)}</td><td>${extAddrLink(e.from, w.primary_chain)}${e.from_label && e.from_label !== e.from ? ` <span class="badge grey">${esc(e.from_label)}</span>` : ""}</td><td>${extAddrLink(e.to, w.primary_chain)}${e.to_label && e.to_label !== e.to ? ` <span class="badge grey">${esc(e.to_label)}</span>` : ""}</td><td style="text-align:right">${tokenAmt(e.amount)} ${esc(w.symbol || "")}</td><td style="text-align:right">${usd(e.amount_usd)}</td><td>${extTxLink(e.tx_hash, w.primary_chain)}</td></tr>`).join("")}
      </tbody></table></div>`
      : `<div class="empty">No transfer ≥ ${usdBig(lt.threshold_usd)} in the last ${num(lt.scanned_transfers)} transfers scanned${lt.scan_earliest ? ` (${dateTimeStr(lt.scan_earliest)} → ${dateTimeStr(lt.scan_latest)})` : ""}.</div>`}
    <div class="muted" style="margin-top:8px">${esc(lt.chain_scope_note || "")}${lts.length ? ` Scanned ${num(lt.scanned_transfers)} transfers${lt.scan_earliest ? ` (${dateTimeStr(lt.scan_earliest)} → ${dateTimeStr(lt.scan_latest)})` : ""}; showing latest ${num(lts.length)}.` : ""}${w.price_assumed_1_usd ? " USD values assume $1.00 (no live price available)." : ""}</div>
    <div class="notice" style="margin-top:12px">${esc(w.methodology || "")}</div>
  </div>`;
}

/* On-chain activity & velocity card (feature 7) — active addresses,
   tx counts, transfer volume, median transfer, velocity and
   period-over-period trends on the primary chain, from a scan of the
   recent-transfers feed. a may be null (still loading — the home page
   fetches /activity separately because its transfer scan is slow) or
   {error:true} if the fetch failed. Every window states whether it
   is complete or a lower bound; nothing here is presented as an
   all-chain total. */
function activityCard(a) {
  if (!a) return `<div class="card"><div class="card-head">Activity &amp; velocity</div><div class="empty"><span class="spinner"></span> Loading activity &amp; velocity data…</div></div>`;
  if (a.error) return `<div class="card"><div class="card-head">Activity &amp; velocity</div><div class="empty">Activity &amp; velocity data unavailable right now (endpoint failed).</div></div>`;
  const w = a.windows || {};
  const cov = a.coverage || {};
  const vel = a.velocity || {};
  const daily = a.daily || {};
  const med = a.median_transfer || {};
  const tr = a.trends || {};
  const chain = a.primary_chain === "tron" ? "Tron" : "Ethereum";
  const winRow = (label, x) => {
    if (!x) return "";
    const badge = x.complete ? '<span class="badge ok">Complete</span>' : '<span class="badge warn">Lower bound</span>';
    return `<tr><td>${label} ${badge}</td><td style="text-align:right">${num(x.tx_count)}</td><td style="text-align:right">${num(x.unique_addresses)}${x.active_share_of_holders_pct != null ? `<br><span class="muted">${Number(x.active_share_of_holders_pct).toFixed(2)}% of holders</span>` : ""}</td><td style="text-align:right">${usdBig(x.volume_usd)}</td><td style="text-align:right">${usd(x.median_transfer_usd)}</td><td style="text-align:right">${x.tx_per_day != null ? num(x.tx_per_day) + "/day" : "—"}</td></tr>`;
  };
  const trendRow = (label, t) => {
    if (!t) return "";
    const cell = (m) => {
      if (!m) return "—";
      if (!m.comparable) return `<span class="muted">not observable</span>`;
      return `${num(m.current)} → ${num(m.prior)} · <b>${pctFmt(m.change_pct)}</b>`;
    };
    return `<tr><td>${label}</td><td style="text-align:right">${cell(t.tx_count)}</td><td style="text-align:right">${cell(t.volume_usd)}</td><td style="text-align:right">${cell(t.active_addresses)}</td></tr>`;
  };
  const trendNote = (label, t) => {
    if (!t) return "";
    const r = [t.tx_count, t.volume_usd, t.active_addresses].map((m) => m && m.reason).find(Boolean);
    return r ? `<div class="muted" style="margin-top:6px">${esc(label)}: ${esc(r)}</div>` : "";
  };
  const basisBadge = (b) => b === "observed_24h" ? '<span class="badge ok">Observed 24h</span>'
    : b === "observed_7d" ? '<span class="badge ok">Observed 7d</span>'
    : b === "observed_30d" ? '<span class="badge ok">Observed 30d</span>'
    : b === "extrapolated_sample" ? '<span class="badge warn">Extrapolated from sample</span>'
    : '<span class="badge grey">Unavailable</span>';
  const lt = a.largest_transfers_in_sample || [];
  const w24 = w["1d"] || {};
  return `<div class="card"><div class="card-head">Activity &amp; velocity <span class="muted">(primary chain only — ${esc(chain)} · ${num(cov.scanned_transfers)} recent transfers scanned${cov.scan_window_label ? `, covering ${esc(cov.scan_window_label)}` : ""})</span></div>
    ${cov.status_note ? `<div class="notice" style="margin-top:8px">${esc(cov.status_note)}</div>` : ""}
    <div class="detail" style="margin-top:6px">
      <div class="dk">Velocity (annualised)</div><div class="dv"><b>${vel.annualized != null ? Number(vel.annualized).toFixed(2) + "× / yr" : "—"}</b> ${basisBadge(daily.basis)}${vel.annualized_vs_total_supply != null ? `<span class="muted"> · ${Number(vel.annualized_vs_total_supply).toFixed(2)}× vs total supply (lower bound)</span>` : ""}</div>
      <div class="dk">Daily transfer volume</div><div class="dv"><b>${usdBig(daily.volume_per_day_usd)}</b>${daily.tx_per_day != null ? ` · ${num(daily.tx_per_day)} transfers/day` : ""}</div>
      <div class="dk">Active addresses (24h)</div><div class="dv"><b>${num(w24.unique_addresses)}</b> ${w24.complete ? '<span class="badge ok">Complete</span>' : '<span class="badge warn">Lower bound</span>'}${w24.active_share_of_holders_pct != null ? ` · ${Number(w24.active_share_of_holders_pct).toFixed(2)}% of ${num(a.holders_total)} holders` : ""}</div>
      <div class="dk">Median transfer</div><div class="dv"><b>${usd(med.median_transfer_usd)}</b> <span class="muted">(${med.basis === "observed_24h" ? "observed 24h window" : "scanned sample"} · ${num(med.transfers_counted)} transfers)</span></div>
      <div class="dk">Supply (primary chain)</div><div class="dv">${usdBig(a.primary_chain_supply_usd)}${a.primary_chain_share_pct != null ? ` · ${Number(a.primary_chain_share_pct).toFixed(1)}% of total ${usdBig(a.supply_usd)}` : ""} · 7d ${pctFmt(a.supply_change_7d_pct)} · 30d ${pctFmt(a.supply_change_30d_pct)}</div>
    </div>
    <div class="muted" style="margin-top:8px">${esc(vel.interpretation || "")}</div>
    <div class="notice" style="margin-top:8px">${esc(daily.basis_note || "")}</div>
    <div class="card-head" style="margin-top:16px">Windows — primary chain</div>
    <div style="overflow-x:auto"><table class="tbl" style="margin-top:10px"><thead><tr><th>Window</th><th style="text-align:right">Transfers</th><th style="text-align:right">Active addresses</th><th style="text-align:right">Volume</th><th style="text-align:right">Median transfer</th><th style="text-align:right">Rate</th></tr></thead><tbody>
    ${winRow("24 hours", w["1d"])}
    ${winRow("7 days", w["7d"])}
    ${winRow("30 days", w["30d"])}
    </tbody></table></div>
    <div class="muted" style="margin-top:8px">${esc(cov.chain_scope_note || "")} Scan: ${num(cov.scanned_transfers)} transfers${cov.scan_earliest ? ` (${dateTimeStr(cov.scan_earliest)} → ${dateTimeStr(cov.scan_latest)})` : ""}${cov.scan_exhausted_feed ? " — feed exhausted (no older transfers exist), so all windows are complete." : ""}${a.holders_total_note ? ` Holders total: ${esc(a.holders_total_note)}` : ""}${a.price_assumed_1_usd ? " USD values assume $1.00 (no live price available)." : ""}</div>
    <div class="card-head" style="margin-top:16px">Growth trends — period over period</div>
    <div style="overflow-x:auto"><table class="tbl" style="margin-top:10px"><thead><tr><th>Comparison</th><th style="text-align:right">Transfers (current → prior)</th><th style="text-align:right">Volume (current → prior)</th><th style="text-align:right">Active addresses (current → prior)</th></tr></thead><tbody>
    ${trendRow("24h vs prior 24h", tr["1d_vs_prior_1d"])}
    ${trendRow("7d vs prior 7d", tr["7d_vs_prior_7d"])}
    </tbody></table></div>
    ${trendNote("24h vs prior 24h", tr["1d_vs_prior_1d"])}
    ${trendNote("7d vs prior 7d", tr["7d_vs_prior_7d"])}
    <div class="card-head" style="margin-top:16px">Largest transfers in the scanned sample</div>
    ${lt.length ? `<div style="overflow-x:auto"><table class="tbl" style="margin-top:10px"><thead><tr><th>Time (UTC)</th><th>From</th><th>To</th><th style="text-align:right">Amount</th><th style="text-align:right">USD value</th><th>Tx</th></tr></thead><tbody>
    ${lt.map((e) => `<tr><td>${dateTimeStr(e.timestamp)}</td><td>${extAddrLink(e.from, a.primary_chain)}</td><td>${extAddrLink(e.to, a.primary_chain)}</td><td style="text-align:right">${tokenAmt(e.amount)} ${esc(a.symbol || "")}</td><td style="text-align:right">${usd(e.amount_usd)}</td><td>${extTxLink(e.tx_hash, a.primary_chain)}</td></tr>`).join("")}
    </tbody></table></div>`
    : '<div class="empty">No transfers with amounts in the scanned sample.</div>'}
    <div class="muted" style="margin-top:8px">Market context: CoinGecko 24h trading volume ${usdBig(a.market ? a.market.trading_volume_24h_usd : null)} — ${esc(a.market ? a.market.note || "" : "")}</div>
    <div class="notice" style="margin-top:12px">${esc(a.methodology || "")}</div>
  </div>`;
}

/* DeFi deployment & yield card (feature 9) — lending rates (range
   across venues, never one pool's APY), largest LP pools holding the
   coin, and collateral usage with its honest limits. d may be null
   (still loading — the home page fetches /defi separately because the
   first load downloads DeFiLlama's ~11MB yields snapshot) or
   {error:true} if the fetch failed. */
function defiCard(d) {
  if (!d) return `<div class="card"><div class="card-head">DeFi deployment &amp; yield</div><div class="empty"><span class="spinner"></span> Loading DeFi deployment &amp; yield data…</div></div>`;
  if (d.error) return `<div class="card"><div class="card-head">DeFi deployment &amp; yield</div><div class="empty">DeFi deployment &amp; yield data unavailable right now (endpoint failed).</div></div>`;
  const lend = d.lending || {};
  const lp = d.lp || {};
  const col = d.collateral || {};
  const tot = d.totals || {};
  const rng = lend.supply_apy_range_pct || {};
  const best = lend.best || {};
  const apyFmt = (v) => (v == null ? "—" : Number(v).toFixed(2) + "%");
  const markets = lend.markets || [];
  const pools = lp.pools || [];
  return `<div class="card"><div class="card-head">DeFi deployment &amp; yield <span class="muted">(DeFiLlama yields snapshot · as of ${dateTimeStr(d.as_of)})</span></div>
    <div class="detail" style="margin-top:6px">
      <div class="dk">Total DeFi TVL</div><div class="dv"><b>${usdBig(tot.defi_tvl_usd)}</b>${tot.defi_tvl_share_of_supply_pct != null ? ` <span class="muted">· ${Number(tot.defi_tvl_share_of_supply_pct).toFixed(2)}% of ${usdBig(d.supply_usd)} supply</span>` : ""}</div>
      <div class="dk">Lending TVL</div><div class="dv"><b>${usdBig(lend.tvl_usd)}</b> <span class="muted">across ${num(lend.markets_total)} lending market(s)</span></div>
      <div class="dk">LP TVL</div><div class="dv"><b>${usdBig(lp.tvl_usd)}</b> <span class="muted">across ${num(lp.pairs_total)} pair(s) — whole-pool TVL (both sides)</span></div>
      <div class="dk">Lending supply APY — range across venues</div><div class="dv"><b>${apyFmt(rng.min_pct)} – ${apyFmt(rng.max_pct)}</b> <span class="muted">· median ${apyFmt(rng.median_pct)} · TVL-weighted ${apyFmt(rng.tvl_weighted_pct)} (${num(rng.markets_counted)} markets ≥ $100k TVL)</span></div>
      <div class="dk">Best lending market</div><div class="dv">${best.supply_apy_pct != null ? `<b>${apyFmt(best.supply_apy_pct)}</b> — ${esc(best.protocol_label || "")} on ${esc(best.chain || "")} (TVL ${usdBig(best.tvl_usd)})` : "—"}</div>
    </div>
    <div class="card-head" style="margin-top:16px">Lending markets — supply APY <span class="muted">(top ${num(lend.markets_shown)} of ${num(lend.markets_total)} by TVL · APY = base + reward)</span></div>
    ${markets.length ? `<div style="overflow-x:auto"><table class="tbl" style="margin-top:10px"><thead><tr><th>Protocol</th><th>Chain</th><th style="text-align:right">Supply APY</th><th style="text-align:right">Base</th><th style="text-align:right">Reward</th><th style="text-align:right">30d mean</th><th style="text-align:right">TVL</th></tr></thead><tbody>
    ${markets.map((m) => `<tr><td><b>${esc(m.protocol_label || m.protocol || "")}</b>${m.pool_meta ? ` <span class="muted">${esc(m.pool_meta)}</span>` : ""}</td><td>${esc(m.chain || "")}</td><td style="text-align:right"><b>${apyFmt(m.supply_apy_pct)}</b></td><td style="text-align:right">${apyFmt(m.base_apy_pct)}</td><td style="text-align:right">${apyFmt(m.reward_apy_pct)}</td><td style="text-align:right">${apyFmt(m.apy_30d_mean_pct)}</td><td style="text-align:right">${usdBig(m.tvl_usd)}</td></tr>`).join("")}
    </tbody></table></div>`
    : '<div class="empty">No lending market for this coin in DeFiLlama yields data.</div>'}
    <div class="muted" style="margin-top:8px">${lend.majors_absent && lend.majors_absent.length ? `No pool found in yields data for: ${esc(lend.majors_absent.join(", "))} — absent from the data, not necessarily unsupported by the protocol.` : `All named majors present: ${esc((lend.majors_present || []).join(", "))}.`} ${esc(lend.coverage_note || "")}</div>
    <div class="card-head" style="margin-top:16px">Largest liquidity pools holding ${esc(d.symbol || "")} <span class="muted">(top ${num(lp.pools_shown)} of ${num(lp.pairs_total)} pairs by TVL · one row per DEX/chain/pair)</span></div>
    ${pools.length ? `<div style="overflow-x:auto"><table class="tbl" style="margin-top:10px"><thead><tr><th>Pair</th><th>DEX</th><th>Chain</th><th style="text-align:right">TVL</th><th style="text-align:right">APY</th><th style="text-align:right">Volume 24h</th><th>Pair type</th><th>IL risk</th></tr></thead><tbody>
    ${pools.map((p) => `<tr><td><b>${esc(p.pair || "")}</b>${p.fee_tier ? ` <span class="muted">${esc(p.fee_tier)}</span>` : ""}</td><td>${esc(p.project_label || p.project || "")}</td><td>${esc(p.chain || "")}</td><td style="text-align:right"><b>${usdBig(p.tvl_usd)}</b></td><td style="text-align:right">${apyFmt(p.apy_pct)}</td><td style="text-align:right">${usdBig(p.volume_24h_usd)}</td><td>${p.stable_pair ? '<span class="badge ok">Stable pair</span>' : '<span class="badge grey">Volatile pair</span>'}</td><td>${esc(p.il_risk || "—")}</td></tr>`).join("")}
    </tbody></table></div>`
    : '<div class="empty">No liquidity pool holding this coin in DeFiLlama yields data.</div>'}
    <div class="muted" style="margin-top:8px">${esc(lp.note || "")}</div>
    <div class="card-head" style="margin-top:16px">Collateral usage</div>
    <div style="margin-top:6px">Supplied in <b>${num(col.supplied_in_lending_markets)}</b> lending market(s) (${usdBig(col.supplied_tvl_usd)} supplied)${(col.top_protocols_by_supplied_tvl || []).length ? ` — largest by supplied TVL: ${(col.top_protocols_by_supplied_tvl || []).map((t) => `${esc(t.protocol_label)} ${usdBig(t.tvl_usd)}`).join(" · ")}` : ""}.</div>
    <div style="overflow-x:auto"><table class="tbl" style="margin-top:10px"><thead><tr><th>Protocol</th><th>How collateral works (general mechanics — not live per-market data)</th></tr></thead><tbody>
    ${(col.mechanics || []).map((m) => `<tr><td><b>${esc(m.protocol)}</b></td><td class="muted">${esc(m.rule)}</td></tr>`).join("")}
    </tbody></table></div>
    <div class="notice" style="margin-top:12px">${esc(col.borrow_apy_status || "")}</div>
    <div class="muted" style="margin-top:8px">${esc(col.borrowable_against_note || "")}</div>
    <div class="notice" style="margin-top:12px">${esc(d.methodology || "")}</div>
  </div>`;
}

/* Freeze & blacklist card (feature 10) — issuer freeze capability
   (feature-8 probe reused, cross-checked against the live contract
   ABI), frozen supply (LOWER-BOUND ESTIMATE — the blacklist is not
   enumerable on-chain), freeze/blacklist/pause events on the primary
   chain, destroyed black funds, and sanctions exposure against the
   vendored OFAC SDN set. f may be null (still loading — the home
   page fetches /freeze separately because its log scan + balance
   batch are slow) or {error:true} if the fetch failed. */
function freezeCard(f) {
  if (!f) return `<div class="card"><div class="card-head">Freeze &amp; blacklist</div><div class="empty"><span class="spinner"></span> Loading freeze &amp; blacklist data…</div></div>`;
  if (f.error) return `<div class="card"><div class="card-head">Freeze &amp; blacklist</div><div class="empty">Freeze &amp; blacklist data unavailable right now (endpoint failed).</div></div>`;
  const cap = f.capability || {};
  const frz = f.frozen_supply || {};
  const evs = f.events || {};
  const win = evs.window || {};
  const dst = f.destroyed_black_funds || {};
  const san = f.sanctions || {};
  const chain = f.primary_chain === "tron" ? "Tron" : "Ethereum";
  const evList = evs.events || [];
  const kindLabel = (k) => ({
    blacklisted: "Blacklisted", unblacklisted: "Unblacklisted",
    frozen: "Frozen", unfrozen: "Unfrozen",
    blacklist_set: "Blacklist set", destroyed: "Black funds destroyed",
    wiped: "Frozen funds wiped", paused: "Paused (all transfers)",
    unpaused: "Unpaused",
  }[k] || k);
  const kindBadge = (k) => (k === "blacklisted" || k === "frozen" || k === "blacklist_set" || k === "paused")
    ? '<span class="badge fail">' + esc(kindLabel(k)) + "</span>"
    : (k === "destroyed" || k === "wiped")
      ? '<span class="badge warn">' + esc(kindLabel(k)) + "</span>"
      : '<span class="badge ok">' + esc(kindLabel(k)) + "</span>";
  const counts = evs.counts_by_kind || {};
  const countsStr = Object.keys(counts).length
    ? Object.entries(counts).map(([k, v]) => `${kindLabel(k)}: ${num(v)}`).join(" · ")
    : "none";
  const topFrozen = frz.top_frozen_holders || [];
  const matches = san.matches || [];
  return `<div class="card"><div class="card-head">Freeze &amp; blacklist <span class="muted">(primary chain: ${esc(chain)} · frozen supply is a lower-bound estimate, never the total)</span></div>
    <div class="detail" style="margin-top:6px">
      <div class="dk">Issuer can freeze balances?</div><div class="dv">${cap.can_freeze ? `<b>Yes</b>${(cap.probe_functions_found || []).length ? ` — ${esc(cap.probe_functions_found.join(", "))} on-chain` : ""}${cap.pause_function_found ? " · pause also present" : ""}` : "<b>No freeze function found</b>"}</div>
      <div class="dk">Frozen supply (estimate)</div><div class="dv">${frz.amount != null ? `<b>${tokenAmt(frz.amount)} ${esc(f.symbol || "")} (${usdBig(frz.amount_usd)})</b> across ${num(frz.addresses_count)} address(es)${frz.addresses_with_balance != null ? `, ${num(frz.addresses_with_balance)} holding a balance` : ""}${frz.share_of_primary_supply_pct != null ? ` · ${Number(frz.share_of_primary_supply_pct).toFixed(4)}% of ${esc(chain)} supply` : ""}` : "—"}</div>
      <div class="dk">Freeze events in window</div><div class="dv">${evs.status === "not_applicable" ? "Not applicable — no freeze function to track" : `${num(evs.total_events)} event(s)${win.window_days ? ` in the last ${num(win.window_days)} day(s)` : ""}${win.extended_lookback ? " (5-year lookback — silent for a year)" : ""} · ${esc(countsStr)}`}</div>
      <div class="dk">Black funds destroyed</div><div class="dv">${dst.events_count != null ? `${num(dst.events_count)} event(s) · ${usdBig(dst.total_usd)} seized and burned` : "No destroy function — frozen funds stay frozen"}</div>
      <div class="dk">Sanctions exposure</div><div class="dv">${san.matches_total ? `<b>${num(san.matches_total)} sanctioned address(es) found</b>` : "No OFAC SDN match in the sets checked"}</div>
    </div>
    ${cap.probe_note ? `<div class="notice" style="margin-top:12px">${esc(cap.probe_note)}</div>` : ""}
    ${cap.events_tracked ? `<div class="notice" style="margin-top:8px">${esc(cap.events_tracked)}</div>` : ""}
    ${cap.abi_note ? `<div class="notice" style="margin-top:8px">${esc(cap.abi_note)}</div>` : ""}
    ${frz.note ? `<div class="notice" style="margin-top:8px"><b>Frozen supply — lower-bound estimate:</b> ${esc(frz.note)}</div>` : ""}
    <div class="card-head" style="margin-top:16px">Largest currently-frozen balances</div>
    ${topFrozen.length ? `<div style="overflow-x:auto"><table class="tbl" style="margin-top:10px"><thead><tr><th>Address</th><th style="text-align:right">Frozen balance (${esc(f.symbol || "")})</th><th style="text-align:right">USD value</th><th>Frozen since</th></tr></thead><tbody>
    ${topFrozen.map((h) => `<tr><td>${extAddrLink(h.address, f.primary_chain)} ${copyBtn(h.address || "")}</td><td style="text-align:right">${tokenAmt(h.balance)}</td><td style="text-align:right">${usd(h.balance_usd)}</td><td>${dateStr(h.frozen_since)}</td></tr>`).join("")}
    </tbody></table></div>`
    : `<div class="empty">${frz.status === "not_applicable" ? "No freeze function — no frozen balances to list." : "No frozen balance found in the scanned window."}</div>`}
    <div class="card-head" style="margin-top:16px">Freeze / blacklist events <span class="muted">(latest ${num(evs.events_shown)}${win.window_days ? ` · ${num(win.window_days)}d window` : ""})</span></div>
    ${evs.status_note ? `<div class="notice" style="margin-top:8px">${esc(evs.status_note)}</div>` : ""}
    ${evs.status === "unavailable" ? '<div class="empty">No freeze/blacklist events could be retrieved for this coin.</div>'
      : evList.length ? `<div style="overflow-x:auto"><table class="tbl" style="margin-top:10px"><thead><tr><th>Time (UTC)</th><th>Event</th><th>Address</th><th style="text-align:right">Amount</th><th>Tx</th></tr></thead><tbody>
      ${evList.map((e) => `<tr><td>${dateTimeStr(e.timestamp)}</td><td>${kindBadge(e.kind)}</td><td>${e.address ? extAddrLink(e.address, f.primary_chain) : '<span class="muted">— (global)</span>'} ${e.address ? copyBtn(e.address) : ""}</td><td style="text-align:right">${e.amount != null ? `${tokenAmt(e.amount)} ${esc(f.symbol || "")} (${usd(e.amount_usd)})` : "—"}</td><td>${extTxLink(e.tx_hash, f.primary_chain)}</td></tr>`).join("")}
      </tbody></table></div>`
      : `<div class="empty">${evs.status === "not_applicable" ? "No freeze function — no freeze events exist to list." : `No freeze/blacklist events in the scanned window${win.window_days ? ` (${num(win.window_days)} days)` : ""}.`}</div>`}
    <div class="muted" style="margin-top:8px">${dst.note ? esc(dst.note) : ""}</div>
    <div class="card-head" style="margin-top:16px">Check an address <span class="muted">(is this address frozen for ${esc(f.symbol || "")}?)</span></div>
    <form id="freeze-watch-form" class="search" autocomplete="off" style="max-width:640px;margin-top:10px">
      <input id="freeze-watch-input" type="text" placeholder="0x… Ethereum address" aria-label="Address to check for a freeze">
      <button type="submit" title="Check freeze status">Check</button>
    </form>
    <div id="freeze-watch-result" style="margin-top:10px"></div>
    <div class="muted" style="margin-top:6px">Live read probe + freeze events naming the address in the scanned window above. The issuer blacklist is not enumerable on-chain — “not frozen” cannot prove the address was never frozen before the window. For other coins, or a Solana address, use the <a href="https://walletscope.vercel.app/#/freeze-watch" target="_blank" rel="noopener">freeze watch in WalletScope →</a></div>
    <div class="card-head" style="margin-top:16px">Sanctions exposure <span class="muted">(OFAC SDN list, ${esc((san.sdn || {}).date || "")} · ${(san.sdn || {}).labels_total != null ? num(san.sdn.labels_total) + " labels" : ""})</span></div>
    <div style="margin-top:6px">${esc(san.summary || "")}</div>
    ${matches.length ? `<div style="overflow-x:auto"><table class="tbl" style="margin-top:10px"><thead><tr><th>Address</th><th>Name (OFAC)</th><th>Program</th><th>Found as</th></tr></thead><tbody>
    ${matches.map((m) => `<tr><td>${extAddrLink(m.address, f.primary_chain)} ${copyBtn(m.address || "")}</td><td>${esc(m.name || "")}</td><td>${esc(m.program || "")}</td><td>${esc((m.contexts || []).join(", "))}</td></tr>`).join("")}
    </tbody></table></div>` : ""}
    ${san.coverage ? `<div class="muted" style="margin-top:8px">Checked: ${num(san.coverage.frozen_addresses_checked)} frozen address(es) · ${num(san.coverage.top_holders_checked)} top holders · ${num(san.coverage.transfers_scanned)} recent transfers (${num(san.coverage.counterparties_checked)} counterparties)${san.coverage.transfer_window_earliest ? ` · transfers ${dateStr(san.coverage.transfer_window_earliest)} → ${dateStr(san.coverage.transfer_window_latest)}` : ""}. ${esc(san.coverage.transfer_note || "")}</div>` : ""}
    <div class="notice" style="margin-top:12px">${esc(f.methodology || "")}</div>
  </div>`;
}

/* Freeze watch result (phase 2, Q6) — one address checked against
   this coin: GET /api/<scope>/freeze/watch?address=. w is the watch
   payload (never null here — bindFreezeWatch only renders on a
   successful fetch). The verdict badge is the headline; the read
   probe, the events naming the address, and the balance are always
   shown with it — never a bare verdict. */
function freezeWatchHtml(w) {
  const v = String(w.verdict || "unknown").toLowerCase();
  const badgeCls = { frozen: "fail", not_frozen: "ok", unknown: "grey", not_freezable: "grey", not_applicable: "grey" }[v] || "grey";
  const vLabel = {
    frozen: "FROZEN — blacklisted",
    not_frozen: "Not frozen",
    unknown: "UNKNOWN — could not be verified",
    not_freezable: "Cannot be frozen",
    not_applicable: "Not applicable",
  }[v] || v;
  const rp = w.read_probe || {};
  const evs = w.events || {};
  const evList = evs.events || [];
  const bal = w.balance || {};
  const kindLabel = (k) => ({
    blacklisted: "Blacklisted", unblacklisted: "Unblacklisted",
    frozen: "Frozen", unfrozen: "Unfrozen",
    blacklist_set: "Blacklist set", destroyed: "Black funds destroyed",
    wiped: "Frozen funds wiped", paused: "Paused (all transfers)",
    unpaused: "Unpaused",
  }[k] || k);
  const kindBadge = (k) => (k === "blacklisted" || k === "frozen" || k === "blacklist_set" || k === "paused")
    ? '<span class="badge fail">' + esc(kindLabel(k)) + "</span>"
    : (k === "destroyed" || k === "wiped")
      ? '<span class="badge warn">' + esc(kindLabel(k)) + "</span>"
      : '<span class="badge ok">' + esc(kindLabel(k)) + "</span>";
  return `<div class="card" style="border-left:4px solid ${v === "frozen" ? "var(--red)" : v === "not_frozen" ? "var(--green)" : "var(--muted)"}">
    <div class="card-head">Freeze watch — ${esc(w.symbol || "")} <span class="spacer"></span><span class="badge ${badgeCls}">${esc(vLabel)}</span></div>
    <div style="padding:12px 16px">
      <div class="mono" style="word-break:break-all">${esc(w.address || "")}</div>
      <div style="margin-top:6px">${esc(w.summary || "")}</div>
      <div class="detail" style="margin-top:10px">
        <div class="dk">Live read probe</div><div class="dv">${rp.function ? `${esc(rp.function)}() → <b>${rp.frozen == null ? "unknown" : rp.frozen ? "frozen" : "not frozen"}</b>` : "No read function"}${rp.note ? `<br><span class="muted">${esc(rp.note)}</span>` : ""}</div>
        <div class="dk">Balance in ${esc(w.symbol || "")}</div><div class="dv">${bal.known ? `${tokenAmt(bal.amount)} ${esc(w.symbol || "")}${bal.amount_usd != null ? ` (${usd(bal.amount_usd)})` : ""}` : "Unknown (balance lookup failed)"}</div>
        <div class="dk">Freeze events naming it</div><div class="dv">${num(evs.total_events_naming_address)} event(s)${evs.window && evs.window.window_days ? ` in the ${num(evs.window.window_days)}d window` : ""}${evs.in_currently_frozen_set ? " · in the currently-frozen set" : ""}${evs.status_note ? `<br><span class="muted">${esc(evs.status_note)}</span>` : ""}</div>
      </div>
      ${evList.length ? `<div style="overflow-x:auto"><table class="tbl" style="margin-top:10px"><thead><tr><th>Time (UTC)</th><th>Event</th><th style="text-align:right">Amount</th><th>Tx</th></tr></thead><tbody>
      ${evList.map((e) => `<tr><td>${dateTimeStr(e.timestamp)}</td><td>${kindBadge(e.kind)}</td><td style="text-align:right">${e.amount != null ? `${tokenAmt(e.amount)} ${esc(w.symbol || "")} (${usd(e.amount_usd)})` : "—"}</td><td>${extTxLink(e.tx_hash, w.primary_chain)}</td></tr>`).join("")}
      </tbody></table></div>` : ""}
      ${(w.limits || []).length ? `<div class="notice" style="margin-top:12px">${(w.limits || []).map((n) => esc(n)).join("<br>")}</div>` : ""}
    </div>
  </div>`;
}

/* Wire the freeze-watch form inside the rendered freeze card. Called
   by homePage after /freeze fills the card in; no-op when the card
   has not rendered (still loading / failed). */
function bindFreezeWatch() {
  const form = $("#freeze-watch-form");
  if (!form) return;
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const addr = $("#freeze-watch-input").value.trim();
    const out = $("#freeze-watch-result");
    if (!addr) { if (out) out.innerHTML = '<div class="empty">Enter an address to check.</div>'; return; }
    if (out) out.innerHTML = '<div class="empty"><span class="spinner"></span> Checking freeze status…</div>';
    try {
      const w = await fetchJson(API + "/freeze/watch?address=" + encodeURIComponent(addr), null, 120000);
      if (out) out.innerHTML = freezeWatchHtml(w);
    } catch (err) {
      if (out) out.innerHTML = `<div class="empty">Freeze check failed: ${esc(err.message)}</div>`;
    }
  });
}

/* Cross-chain supply map card (feature 5) — per-chain supply with
   7d/30d net change per chain and the covered share. sm may be null
   if the endpoint failed; the home page still renders without it. */
function supplyMapCard(sm) {
  if (!sm) return `<div class="card"><div class="card-head">Cross-chain supply map</div><div class="empty">Supply map unavailable right now (endpoint failed).</div></div>`;
  const signed = (v) => (v == null || v === "") ? "—" : (v < 0 ? "−" : "+") + usdBig(Math.abs(v));
  const chgCell = (ch) => ch && ch.net_usd != null
    ? `${signed(ch.net_usd)} (${pctFmt(ch.pct)})${ch.partial && ch.start_date ? ` <span class="muted" title="Series starts ${dateStr(ch.start_date)} — covers ${num(ch.window_days)}d, not the full window">· partial</span>` : ""}`
    : "—";
  const chains = sm.chains || [];
  const top = chains.slice(0, 10);
  const rest = chains.slice(10);
  const restTotal = rest.reduce((a, c) => a + (c.supply_usd || 0), 0);
  return `<div class="card"><div class="card-head">Cross-chain supply map <span class="muted">(DeFiLlama chain balances · as of ${dateStr(sm.as_of)})</span></div>
    <div style="overflow-x:auto"><table class="tbl"><thead><tr><th>Chain</th><th style="text-align:right">Supply</th><th style="text-align:right">Share of total</th><th style="text-align:right">Net change 7d</th><th style="text-align:right">Net change 30d</th><th style="text-align:right">Bridged-in</th></tr></thead><tbody>
    ${top.map((c) => `<tr><td>${esc(c.chain)}</td><td style="text-align:right">${usdBig(c.supply_usd)}</td><td style="text-align:right">${c.share_of_total_pct != null ? c.share_of_total_pct.toFixed(1) + "%" : "—"}</td><td style="text-align:right">${chgCell(c.change_7d)}</td><td style="text-align:right">${chgCell(c.change_30d)}</td><td style="text-align:right">${c.bridged_usd ? usdBig(c.bridged_usd) + (c.bridged_share_pct != null ? ` (${c.bridged_share_pct.toFixed(1)}%)` : "") : "—"}</td></tr>`).join("") || '<tr><td colspan="6" class="empty">No chain data</td></tr>'}
    ${rest.length ? `<tr><td class="muted">…and ${num(rest.length)} more chains</td><td style="text-align:right" class="muted">${usdBig(restTotal)}</td><td style="text-align:right" class="muted">—</td><td style="text-align:right" class="muted">—</td><td style="text-align:right" class="muted">—</td><td style="text-align:right" class="muted">—</td></tr>` : ""}
    </tbody></table></div>
    <div class="muted" style="margin-top:8px">Mapped ${usdBig(sm.covered_supply_usd)} of ${usdBig(sm.total_supply_usd)} total supply (${sm.covered_share_pct != null ? sm.covered_share_pct.toFixed(1) + "%" : "—"} covered) across ${num(sm.chain_count)} chains${sm.unknown_supply_usd ? ` · ${usdBig(sm.unknown_supply_usd)} unattributed to any chain` : ""} · primary chain for holders/transfers: ${esc(sm.primary_chain === "tron" ? "Tron" : "Ethereum")}</div>
    ${sm.timing_note ? `<div class="notice" style="margin-top:8px">${esc(sm.timing_note)}</div>` : ""}
    <div class="notice" style="margin-top:8px">Net change per chain is bridges + local mint/burn combined — the two cannot be separated from supply data alone, so it is not labelled a bridge flow. Bridged-in is the current stock of supply that arrived via a bridge, not how much moved in a window.</div>
  </div>`;
}

/* Bridges card (feature 5) — which bridges move this coin, as
   current bridged-in stock per bridge. Shown in the two-column row. */
function bridgesCard(sm) {
  if (!sm) return `<div class="card"><div class="card-head">Bridges</div><div class="empty">Bridge data unavailable right now (endpoint failed).</div></div>`;
  const bs = sm.bridges_summary || [];
  return `<div class="card"><div class="card-head">Bridges — bridged-in stock</div>
    ${bs.length ? `<div style="overflow-x:auto"><table class="tbl"><thead><tr><th>Bridge</th><th style="text-align:right">Bridged-in</th><th style="text-align:right">Share</th></tr></thead><tbody>
    ${bs.map((b) => `<tr><td>${esc(b.bridge_label || b.bridge)}</td><td style="text-align:right">${usdBig(b.total_usd)}</td><td style="text-align:right">${b.share_of_bridged_pct != null ? b.share_of_bridged_pct.toFixed(1) + "%" : "—"}</td></tr>`).join("")}
    </tbody></table></div>
    <div class="muted" style="margin-top:8px">Total bridged-in stock: ${usdBig(sm.bridged_total_usd)} — supply currently sitting on a chain it arrived at via a bridge. A stock, not a flow: no keyless source reports per-coin bridge flows over a window (Wormholescan ignores the symbol filter; DeFiLlama bridges is paywalled).</div>`
    : `<div class="empty">No bridged supply reported for this coin — either it lives on its native chain(s) or DeFiLlama reports no bridged stock.</div>`}
  </div>`;
}

/* ---------------- views ---------------- */
async function homePage() {
  render(`<div class="card" style="margin-top:22px"><div class="empty"><span class="spinner"></span> Loading ${esc(TITLE)}…</div></div>`);
  try {
    const [s, peg, sup, dep, dscore, guard, lb, mb, sm] = await Promise.all([
      loadStats(), get("/peg?days=" + PEG_DAYS), get("/supply?days=" + SUPPLY_DAYS), get("/depegs"),
      get("/depeg-score").catch(() => null),
      get("/quote-guard").catch(() => null),
      get("/peg-leaderboard").catch(() => null),
      get("/mint-burn").catch(() => null),
      get("/supply-map").catch(() => null),
    ]);
    const denom = primarySupplyDenominator();
    const pegPts = (peg.points || []).map((p) => ({ x: dateStr(p.date), y: p.price, bps: p.deviation_bps }));
    const supPts = (sup.points || []).map((p) => ({ x: dateStr(p.date), y: p.supply_usd }));
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

    ${depegScoreCard(dscore)}

    ${quoteGuardCard(guard)}

    <div id="issuer-risk-card">${issuerRiskCard(null)}</div>

    <div id="payments-card">${paymentsReadinessCard(null)}</div>

    ${pegLeaderboardCard(lb)}

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

    ${supplyMapCard(sm)}

    ${mintBurnCard(mb)}

    <div id="whales-card">${whalesCard(null)}</div>

    <div id="activity-card">${activityCard(null)}</div>

    <div id="defi-card">${defiCard(null)}</div>

    <div id="freeze-card">${freezeCard(null)}</div>

    <div class="cols2">
      ${bridgesCard(sm)}
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
    // Whales & exchanges (feature 6): fetched separately — its
    // transfer scan is slow — and filled in without blocking the rest
    // of the page. Guarded: the user may have navigated away by the
    // time it resolves.
    get("/whales").then((w) => {
      const el = document.getElementById("whales-card");
      if (el) el.innerHTML = whalesCard(w);
    }).catch(() => {
      const el = document.getElementById("whales-card");
      if (el) el.innerHTML = whalesCard({ error: true });
    });
    // Activity & velocity (feature 7): fetched separately — its
    // transfer scan is slow — and filled in without blocking the rest
    // of the page. Same guard as /whales above.
    get("/activity").then((act) => {
      const el = document.getElementById("activity-card");
      if (el) el.innerHTML = activityCard(act);
    }).catch(() => {
      const el = document.getElementById("activity-card");
      if (el) el.innerHTML = activityCard({ error: true });
    });
    // Issuer risk (feature 11): fetched separately — it reuses the
    // slow freeze log scans + activity scans — and filled in
    // without blocking the rest of the page. Same guard as above.
    fetchJson(API + "/issuer-risk", null, 180000).then((ir) => {
      const el = document.getElementById("issuer-risk-card");
      if (el) el.innerHTML = issuerRiskCard(ir);
    }).catch(() => {
      const el = document.getElementById("issuer-risk-card");
      if (el) el.innerHTML = issuerRiskCard({ error: true });
    });
    // Payments readiness (feature 8): fetched separately — it reuses
    // the feature-7 activity scan, so it is slow — and filled in
    // without blocking the rest of the page. Same guard as above.
    fetchJson(API + "/payments-readiness", null, 120000).then((pr) => {
      const el = document.getElementById("payments-card");
      if (el) el.innerHTML = paymentsReadinessCard(pr);
    }).catch(() => {
      const el = document.getElementById("payments-card");
      if (el) el.innerHTML = paymentsReadinessCard({ error: true });
    });
    // DeFi deployment & yield (feature 9): fetched separately — the
    // first load downloads DeFiLlama's ~11MB yields snapshot — and
    // filled in without blocking the rest of the page. Same guard.
    fetchJson(API + "/defi", null, 120000).then((df) => {
      const el = document.getElementById("defi-card");
      if (el) el.innerHTML = defiCard(df);
    }).catch(() => {
      const el = document.getElementById("defi-card");
      if (el) el.innerHTML = defiCard({ error: true });
    });
    // Freeze & blacklist (feature 10): fetched separately — its
    // log scan + Multicall3 balance batch are slow — and filled in
    // without blocking the rest of the page. Same guard as above.
    // The Q6 watch form inside the card is wired once it renders.
    fetchJson(API + "/freeze", null, 120000).then((fz) => {
      const el = document.getElementById("freeze-card");
      if (el) { el.innerHTML = freezeCard(fz); bindFreezeWatch(); }
    }).catch(() => {
      const el = document.getElementById("freeze-card");
      if (el) el.innerHTML = freezeCard({ error: true });
    });
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
    ${(() => { const t = r.attestation_tracker || {}; const rf = t.reserve_figures || null; return `
    <div class="card"><div class="card-head">Attestation tracker ${trackerBadge(t.status)} <span class="muted">as of ${esc(t.as_of || "—")}</span></div>
      <div class="detail">
        <div class="dk">Auditor</div><div class="dv">${esc(t.auditor || "—")}</div>
        <div class="dk">Cadence</div><div class="dv">${esc(t.cadence || "—")}</div>
        <div class="dk">Last attestation</div><div class="dv">${t.last_attestation ? esc(t.last_attestation) + (t.days_since != null ? ` (${num(t.days_since)} days ago)` : "") + (t.last_attestation_source ? ` · <a href="${esc(t.last_attestation_source)}" target="_blank" rel="noopener">report</a>` : "") : "No dated attestation"}</div>
        <div class="dk">Next expected</div><div class="dv">${t.next_expected ? esc(t.next_expected) : "—"}</div>
        ${t.last_attestation_note ? `<div class="dk">Note</div><div class="dv">${esc(t.last_attestation_note)}</div>` : ""}
      </div>
      ${(t.alerts || []).length ? (t.alerts || []).map((a) => `<div class="notice" style="margin-top:12px">⚠ ${esc(a)}</div>`).join("") : ""}
      ${rf ? `
      <div class="detail" style="margin-top:12px">
        <div class="dk">Reserve total (dated, published)</div><div class="dv">${usdBig(rf.reserve_total_usd)}${rf.reserve_total_approx ? " (approx. — rounded in source)" : ""} <span class="muted">as of ${esc(rf.as_of || "—")}</span></div>
        ${rf.tokens_outstanding != null ? `<div class="dk">Tokens outstanding (same attestation)</div><div class="dv">${tokenAmt(rf.tokens_outstanding)} ${esc(s.symbol)}</div>` : ""}
        ${rf.excess_usd != null ? `<div class="dk">Excess over liabilities</div><div class="dv">${usdBig(rf.excess_usd)}</div>` : ""}
        ${rf.reserve_to_outstanding_ratio != null ? `<div class="dk">Reserve / outstanding</div><div class="dv">${num(rf.reserve_to_outstanding_ratio)}× <span class="muted">(same attestation, ${esc(rf.as_of || "")})</span></div>` : ""}
        ${rf.reserve_to_supply_ratio != null ? `<div class="dk">Reserve / current supply</div><div class="dv">${num(rf.reserve_to_supply_ratio)}× <span class="muted">(${usdBig(rf.reserve_total_usd)} as of ${esc(rf.as_of || "—")} ÷ ${usdBig(rf.ratio_supply_usd)} current supply as of ${esc(rf.ratio_supply_as_of || "—")})</span></div>` : ""}
        <div class="dk">Source</div><div class="dv">${rf.source_url ? `<a href="${esc(rf.source_url)}" target="_blank" rel="noopener">${esc(rf.source || "")}</a>` : esc(rf.source || "—")}</div>
      </div>
      ${(rf.reserve_mix || []).length ? `<table class="tbl" style="margin-top:12px"><thead><tr><th>Reserve mix — as of ${esc(rf.as_of || "—")}</th><th style="text-align:right">Amount</th><th style="text-align:right">Share</th></tr></thead><tbody>
        ${(rf.reserve_mix || []).map((m) => `<tr><td>${esc(m.label)}${m.detail ? ` <span class="muted">(${esc(m.detail)})</span>` : ""}</td><td style="text-align:right">${usdBig(m.amount_usd)}</td><td style="text-align:right">${m.share_pct != null ? num(m.share_pct) + "%" : "—"}</td></tr>`).join("")}
      </tbody></table>` : ""}
      ${rf.mix_note ? `<div class="notice info" style="margin-top:12px">${esc(rf.mix_note)}</div>` : ""}` : `<div class="notice" style="margin-top:12px">No dated published reserve figures could be verified for this coin — reserve totals and mix are not shown rather than estimated. See the attestation links above for the issuer's own disclosures.</div>`}
      ${t.methodology ? `<p class="muted" style="margin:12px 16px">${esc(t.methodology)}</p>` : ""}
    </div>`; })()}
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
  if (bname) { const _bm = TITLE.match(/^(.*)(scope)$/i); bname.innerHTML = _bm ? esc(_bm[1]) + '<span class="brand-accent">' + esc(_bm[2]) + '</span>' : esc(TITLE) + '<span class="brand-accent">Scope</span>'; }
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
