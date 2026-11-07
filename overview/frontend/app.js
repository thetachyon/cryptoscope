"use strict";
/* Cryptoscope overview frontend — cross-chain analytics landing page.
   No build step, no CDN. Per-app behaviour comes from /config.js
   (window.SCOPE_CONFIG). API: /api/overview/* — the aggregate endpoint
   returns global/chains/stablecoins/bridges/movers; the peg leaderboard
   and the mint/burn rollup are fetched separately (their per-coin work
   is slow). Each section degrades independently server-side.
   Same light Etherscan-like theme/classes as the other Scope apps
   (styles.css is shared): .hero, .card, .tbl, .badge, .statgrid, .cols2,
   .gascards, .topbar, .wrap, .error-banner, .footer. */
const CFG = window.SCOPE_CONFIG || {};
const SCOPE = CFG.scope || "overview";
const TITLE = CFG.title || "Cryptoscope";
const API = CFG.apiPrefix || "/api/overview";
const WALLET_URL = CFG.walletUrl || "https://walletscope.vercel.app";
const SIBLINGS = Array.isArray(CFG.siblings) ? CFG.siblings : [];

/* ---------------- helpers ---------------- */
const $ = (s) => document.querySelector(s);
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
async function fetchJson(url, opts, timeoutMs) {
  // 25s client-side guard so a stalled upstream can never hang the UI.
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs || 25000);
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
/* Big USD values: $2.58T, $177.71B, $289.80M, $1.23K. */
function usdBig(n) {
  if (n == null || n === "") return "—";
  const x = Number(n);
  if (!Number.isFinite(x)) return "—";
  const abs = Math.abs(x);
  const sign = x < 0 ? "-" : "";
  if (abs >= 1e12) return sign + "$" + (abs / 1e12).toFixed(2) + "T";
  if (abs >= 1e9) return sign + "$" + (abs / 1e9).toFixed(2) + "B";
  if (abs >= 1e6) return sign + "$" + (abs / 1e6).toFixed(2) + "M";
  if (abs >= 1e3) return sign + "$" + (abs / 1e3).toFixed(2) + "K";
  return "$" + x.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
function usd(n) {
  if (n == null || n === "") return "—";
  const x = Number(n);
  return Number.isFinite(x) ? "$" + x.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : "—";
}
/* USD price that can be tiny (token prices): up to 6 decimals below $1. */
function usdPrice(n) {
  if (n == null || n === "") return "—";
  const x = Number(n);
  if (!Number.isFinite(x)) return "—";
  if (x > 0 && x < 1) return "$" + x.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 6 });
  return usd(x);
}
function pctChange(v, suffix) {
  if (v == null || v === "") return "";
  const x = Number(v);
  if (!Number.isFinite(x)) return "";
  const color = x >= 0 ? "var(--green)" : "var(--red)";
  return ` <small style="color:${color}">(${x >= 0 ? "+" : ""}${x.toFixed(2)}%${suffix || ""})</small>`;
}
function bpsFmt(v) {
  if (v == null || v === "") return "—";
  const x = Number(v);
  if (!Number.isFinite(x)) return "—";
  const color = Math.abs(x) <= 5 ? "var(--green)" : Math.abs(x) <= 50 ? "var(--amber)" : "var(--red)";
  return `<span style="color:${color};font-weight:700">${x >= 0 ? "+" : ""}${x.toFixed(1)} bps</span>`;
}
function healthBadge(h) {
  if (h === "pegged") return '<span class="badge ok">Pegged</span>';
  if (h === "watch") return '<span class="badge warn">Watch</span>';
  if (h === "depegged") return '<span class="badge fail">Depegged</span>';
  return `<span class="badge grey">${esc(h || "—")}</span>`;
}
function scopeLink(url, text) {
  return `<a href="${esc(url)}" target="_blank" rel="noopener">${esc(text)} ↗</a>`;
}

/* ---------------- banner / view ---------------- */
function banner(msg) {
  const b = $("#error-banner");
  if (!b) return;
  if (!msg) { b.classList.add("hidden"); b.textContent = ""; return; }
  b.textContent = msg;
  b.classList.remove("hidden");
}
function render(html) { $("#view").innerHTML = html; banner(null); }
function spinner() { return '<div class="spinner">Loading…</div>'; }
function emptyBox(msg) { return `<div class="empty">${esc(msg)}</div>`; }
function errBox(msg) { return `<div class="error-banner" style="margin:16px">${esc(msg)}</div>`; }
function sectionError(name, msg) {
  return `<div class="card"><div class="card-head">${esc(name)}</div><div style="padding:16px"><div class="notice info" style="margin:0">${esc(msg || "Data unavailable.")}</div></div></div>`;
}

/* ---------------- polling ---------------- */
let pollTimer = null;
function clearPoll() { if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } }

/* ---------------- section renderers ---------------- */
function renderGlobal(g) {
  const el = $("#market-stats");
  if (!el) return;
  if (!g || g.error) {
    el.innerHTML = `<div class="card" style="grid-column:1/-1">${errBox("Market overview unavailable: " + (g && g.error ? g.error : "unknown error"))}</div>`;
    return;
  }
  el.innerHTML = `
    <div class="stat"><div class="k">Total Market Cap</div><div class="v">${usdBig(g.total_market_cap_usd)}</div><div class="sub">Crypto total${pctChange(g.market_cap_change_24h_pct, " 24h")}</div></div>
    <div class="stat"><div class="k">BTC Dominance</div><div class="v">${g.btc_dominance_pct != null ? Number(g.btc_dominance_pct).toFixed(1) + "%" : "—"}</div><div class="sub">ETH dominance ${g.eth_dominance_pct != null ? Number(g.eth_dominance_pct).toFixed(1) + "%" : "—"}</div></div>
    <div class="stat"><div class="k">Total Stablecoin Supply</div><div class="v">${usdBig(g.total_stablecoin_supply_usd)}</div><div class="sub">Sum of the 8 stablecoin scopes</div></div>
    <div class="stat"><div class="k">Active Cryptocurrencies</div><div class="v">${num(g.active_cryptocurrencies)}</div><div class="sub">${num(g.markets)} markets tracked</div></div>`;
}
function renderChains(c) {
  const el = $("#chains-body");
  if (!el) return;
  if (!c || c.error || !Array.isArray(c.rows) || !c.rows.length) {
    el.innerHTML = sectionError("Chains", (c && c.error) || "Chain data unavailable.");
    return;
  }
  el.innerHTML = `<table class="tbl"><thead><tr><th>Chain</th><th class="r">Price</th><th class="r">24h</th><th class="r">Latest</th><th class="r">Block Time / TPS</th><th class="r">Fee</th><th></th></tr></thead><tbody>
    ${c.rows.map((r) => {
      const latest = r.latest != null
        ? `<span class="mono">${num(r.latest)}</span> <span class="muted">${esc(r.latest_label || "block")}</span>` : "—";
      const speed = r.tps != null
        ? `${Number(r.tps).toLocaleString("en-US", { maximumFractionDigits: 1 })} TPS`
        : r.avg_block_time_s != null ? `${num(r.avg_block_time_s)}s avg block` : "—";
      const chg = r.change_24h_pct != null
        ? `<span style="color:${Number(r.change_24h_pct) >= 0 ? "var(--green)" : "var(--red)"}">${Number(r.change_24h_pct) >= 0 ? "+" : ""}${Number(r.change_24h_pct).toFixed(2)}%</span>` : "—";
      return `<tr>
        <td><strong>${esc(r.label || r.id)}</strong> <span class="chain-badge">${esc(r.native || "")}</span>${r.error ? ` <span class="muted" title="${esc(r.error)}">(degraded)</span>` : ""}</td>
        <td class="r">${usdPrice(r.price_usd)}</td>
        <td class="r">${chg}</td>
        <td class="r">${latest}</td>
        <td class="r">${speed}</td>
        <td class="r">${esc(r.fee || "—")}</td>
        <td class="r">${r.scope_url ? scopeLink(r.scope_url + "/#/", "Open →") : "—"}</td>
      </tr>`;
    }).join("")}
  </tbody></table>`;
}
function renderStablecoins(s) {
  const el = $("#stablecoins-body");
  if (!el) return;
  if (!s || s.error || !Array.isArray(s.rows) || !s.rows.length) {
    el.innerHTML = sectionError("Stablecoins", (s && s.error) || "Stablecoin data unavailable.");
    return;
  }
  el.innerHTML = `<table class="tbl"><thead><tr><th>Stablecoin</th><th class="r">Price</th><th class="r">Deviation</th><th class="r">Health</th><th class="r">Supply</th><th></th></tr></thead><tbody>
    ${s.rows.map((r) => `<tr>
      <td><strong>${esc(r.symbol || "")}</strong> <span class="muted">${esc(r.name || "")}</span>${r.error ? ` <span class="muted" title="${esc(r.error)}">(degraded)</span>` : ""}</td>
      <td class="r">${usdPrice(r.price_usd)}</td>
      <td class="r">${bpsFmt(r.deviation_bps)}</td>
      <td class="r">${healthBadge(r.peg_health)}</td>
      <td class="r">${usdBig(r.supply_usd)}</td>
      <td class="r">${r.scope_url ? scopeLink(r.scope_url + "/#/", "Open →") + " · " + scopeLink(r.scope_url + "/#/depeg-history", "Score history →") : "—"}</td>
    </tr>`).join("")}
  </tbody></table>
  <div class="total-row"><span>Total stablecoin supply (8 scopes)</span><span>${usdBig(s.total_supply_usd)}</span></div>`;
}
function renderPegLeaderboard(lb) {
  const el = $("#peg-leaderboard-body");
  if (!el) return;
  if (!lb || !Array.isArray(lb.rows) || !lb.rows.length) {
    el.innerHTML = sectionError("Stablecoin Peg Stability", "Peg leaderboard unavailable right now.");
    return;
  }
  const offCell = (o) => (o && o.days != null) ? `${num(o.days)}${o.share_pct != null ? ` (${Number(o.share_pct).toFixed(1)}%)` : ""}` : "—";
  const worstCell = (bps, date) => (bps != null)
    ? `${bpsFmt(bps)}<br><span class="muted">${date ? new Date(Number(date) * 1000).toISOString().slice(0, 10) : "—"}</span>` : "—";
  const recCell = (e) => {
    if (!e || e.count == null) return "—";
    const med = e.median_recovery_days != null ? `${num(e.median_recovery_days)}d` : "—";
    const mx = e.max_recovery_days != null ? `${num(e.max_recovery_days)}d` : "—";
    return `${num(e.count)} ep · med ${med} · max ${mx}${e.ongoing ? ` · ${num(e.ongoing)} ongoing` : ""}`;
  };
  el.innerHTML = `<div style="padding:12px 16px 0" class="muted">${esc(lb.ranking_rule || "")}</div>
  <div style="overflow-x:auto"><table class="tbl"><thead><tr><th>#</th><th>Coin</th><th class="r">Now</th><th class="r">Worst 30d</th><th class="r">Worst all-time</th><th class="r">Off-peg 30d</th><th class="r">Off-peg 90d</th><th class="r">Off-peg 365d</th><th>Episodes 365d (recovery)</th><th class="r">Vol 30d</th><th></th></tr></thead><tbody>
    ${lb.rows.map((r) => r.error ? `<tr>
      <td>—</td><td><strong>${esc(r.symbol || "")}</strong> <span class="muted">${esc(r.name || "")}</span></td>
      <td colspan="8" class="muted">Unavailable: ${esc(r.error)}</td><td></td></tr>`
      : `<tr>
      <td><strong>${r.rank != null ? num(r.rank) : "—"}</strong></td>
      <td><strong>${esc(r.symbol || "")}</strong> <span class="muted">${esc(r.name || "")}</span></td>
      <td class="r">${bpsFmt(r.deviation_bps)}</td>
      <td class="r">${worstCell(r.max_dev_30d_bps, r.max_dev_30d_date)}</td>
      <td class="r">${worstCell(r.max_dev_alltime_bps, r.max_dev_alltime_date)}</td>
      <td class="r">${offCell(r.off_peg_30d)}</td>
      <td class="r">${offCell(r.off_peg_90d)}</td>
      <td class="r">${offCell(r.off_peg_365d)}</td>
      <td>${recCell(r.episodes_365d)}</td>
      <td class="r">${r.volatility_30d_pct != null ? Number(r.volatility_30d_pct).toFixed(2) + "%/day" : "—"}</td>
      <td class="r">${r.scope ? scopeLink("https://" + r.scope + ".vercel.app/#/", "Open →") : "—"}</td>
    </tr>`).join("")}
  </tbody></table></div>
  <div style="padding:12px 16px" class="muted">${esc(lb.episode_rule || "")}<br>${esc(lb.volatility_note || "")}<br>${esc(lb.caveat || "")}</div>`;
}
function renderMintBurn(mb) {
  const el = $("#mint-burn-body");
  if (!el) return;
  if (!mb || !Array.isArray(mb.rows) || !mb.rows.length) {
    el.innerHTML = sectionError("Stablecoin Mint / Burn", "Mint/burn rollup unavailable right now.");
    return;
  }
  const signedUsd = (v) => {
    if (v == null || v === "") return "—";
    const x = Number(v);
    if (!Number.isFinite(x)) return "—";
    const color = x > 0 ? "var(--green)" : x < 0 ? "var(--red)" : "inherit";
    return `<span style="color:${color};font-weight:700">${x > 0 ? "+" : x < 0 ? "−" : ""}${usdBig(Math.abs(x))}</span>`;
  };
  const dirBadge = (d) => d === "growing" ? '<span class="badge ok">Growing</span>'
    : d === "shrinking" ? '<span class="badge fail">Shrinking</span>'
    : d === "mixed" ? '<span class="badge warn">Mixed</span>'
    : d === "flat" ? '<span class="badge grey">Flat</span>' : '<span class="badge grey">—</span>';
  el.innerHTML = `<div style="overflow-x:auto"><table class="tbl"><thead><tr><th>Stablecoin</th><th class="r">Net issuance · 7d</th><th class="r">Net issuance · 30d</th><th class="r">Net issuance · 90d</th><th>Demand direction</th><th class="r">Large on-chain events</th><th></th></tr></thead><tbody>
    ${mb.rows.map((r) => r.error ? `<tr>
      <td><strong>${esc(r.symbol || "")}</strong> <span class="muted">${esc(r.name || "")}</span></td>
      <td colspan="5" class="muted">Unavailable: ${esc(r.error)}</td><td></td></tr>`
      : `<tr>
      <td><strong>${esc(r.symbol || "")}</strong> <span class="muted">${esc(r.name || "")}</span>${r.flat_supply ? ` <span class="muted">(frozen supply)</span>` : ""}</td>
      <td class="r">${signedUsd(r.net_7d_usd)}</td>
      <td class="r">${signedUsd(r.net_30d_usd)}</td>
      <td class="r">${signedUsd(r.net_90d_usd)}</td>
      <td>${dirBadge(r.demand_direction)}</td>
      <td class="r">${r.large_events_status === "unavailable" ? '<span class="muted">unavailable</span>' : num(r.large_events_count)}</td>
      <td class="r">${r.scope_url ? scopeLink(r.scope_url + "/#/", "Open →") : "—"}</td>
    </tr>`).join("")}
  </tbody></table></div>
  <div class="total-row"><span>Total net issuance · 30d (${num(mb.coins_included)} of 8 coins)</span><span>${signedUsd(mb.total_net_30d_usd)}</span></div>
  <div class="total-row"><span>Total net issuance · 90d</span><span>${signedUsd(mb.total_net_90d_usd)}</span></div>
  <div style="padding:12px 16px" class="muted">${esc(mb.note || "")}<br>${esc(mb.caveat || "")} Large on-chain events are primary-chain only (see each coin's Mint / burn section for thresholds and scan coverage).<br><a href="#/supply-feed">Full supply &amp; flow feed →</a> daily net-issuance series + large on-chain mint/burn events, filterable by coin, record type and time window (GET /api/overview/supply-feed).</div>`;
}
function renderWhales(wh) {
  const el = $("#whales-body");
  if (!el) return;
  if (!wh || !Array.isArray(wh.rows) || !wh.rows.length) {
    el.innerHTML = sectionError("Stablecoin Whales & Exchanges", "Whale/exchange rollup unavailable right now.");
    return;
  }
  const share = (v) => (v != null ? Number(v).toFixed(2) + "%" : "—");
  const largestCell = (r) => {
    const l = r.largest_transfer;
    if (!l) return '<span class="muted">none in scan</span>';
    return `${usdBig(l.amount_usd)}<br><span class="muted">${l.timestamp ? new Date(Number(l.timestamp) * 1000).toISOString().slice(0, 19).replace("T", " ") : ""}</span>`;
  };
  el.innerHTML = `<div style="overflow-x:auto"><table class="tbl"><thead><tr><th>Stablecoin</th><th class="r">Top 1</th><th class="r">Top 10</th><th class="r">Top 50</th><th class="r">Exchange-identified (lower bound)</th><th class="r">Large transfers</th><th>Largest in scan</th><th></th></tr></thead><tbody>
    ${wh.rows.map((r) => r.error ? `<tr>
      <td><strong>${esc(r.symbol || "")}</strong> <span class="muted">${esc(r.name || "")}</span></td>
      <td colspan="6" class="muted">Unavailable: ${esc(r.error)}</td><td></td></tr>`
      : `<tr>
      <td><strong>${esc(r.symbol || "")}</strong> <span class="muted">${esc(r.name || "")}</span><br><span class="muted">${esc(r.primary_chain === "tron" ? "Tron" : "Ethereum")} · ${r.holders_total != null ? num(r.holders_total) + " holders" : ""}</span></td>
      <td class="r">${share(r.top_1_share_pct)}</td>
      <td class="r">${share(r.top_10_share_pct)}</td>
      <td class="r">${share(r.top_50_share_pct)}</td>
      <td class="r">${r.exchange_wallets_identified ? `${usdBig(r.exchange_total_usd)} (${share(r.exchange_total_share_pct)}) · ${num(r.exchange_wallets_identified)} wallet(s): ${esc((r.exchanges_identified || []).join(", "))}` : '<span class="muted">none identified</span>'}</td>
      <td class="r">${r.large_transfers_status === "unavailable" ? '<span class="muted">unavailable</span>' : `${num(r.large_transfers_count)} ≥ ${usdBig(r.threshold_usd)}`}<br><span class="muted">${num(r.scanned_transfers)} scanned</span></td>
      <td>${largestCell(r)}</td>
      <td class="r">${r.scope_url ? scopeLink(r.scope_url + "/#/", "Open →") : "—"}</td>
    </tr>`).join("")}
  </tbody></table></div>
  <div class="total-row"><span>Total exchange-identified across 8 coins (lower bound — top 50, primary chain only)</span><span>${usdBig(wh.total_exchange_identified_usd)}</span></div>
  <div class="total-row"><span>Coins with any exchange wallet identified</span><span>${num(wh.coins_with_exchange_identified)} of ${num(wh.coins_included)}</span></div>
  <div class="total-row"><span>Total large transfers found</span><span>${num(wh.total_large_transfers)}</span></div>
  <div style="padding:12px 16px" class="muted">${esc(wh.note || "")}<br><a href="#/whale-feed">Full whale feed →</a> large transfers across all 8 coins, filterable by coin, minimum amount and time window (GET /api/overview/whale-feed).</div>`;
}
function renderActivity(ac) {
  const el = $("#activity-body");
  if (!el) return;
  if (!ac || !Array.isArray(ac.rows) || !ac.rows.length) {
    el.innerHTML = sectionError("Stablecoin Activity & Velocity", "Activity rollup unavailable right now.");
    return;
  }
  const vel = (v) => (v != null ? Number(v).toFixed(2) + "×/yr" : "—");
  const chg = (v) => (v != null ? (Number(v) >= 0 ? "+" : "") + Number(v).toFixed(2) + "%" : "—");
  const winCell = (n, complete) => {
    if (n == null) return "—";
    return `${num(n)}${complete ? "" : ' <span class="muted" title="Incomplete window — lower bound, not the window total">≥</span>'}`;
  };
  el.innerHTML = `<div style="overflow-x:auto"><table class="tbl"><thead><tr><th>Stablecoin</th><th class="r">Active addrs · 24h</th><th class="r">Transfers · 24h</th><th class="r">Volume · 24h</th><th class="r">Median transfer</th><th class="r">Velocity (annualised)</th><th class="r">Trend 24h vs prior 24h</th><th></th></tr></thead><tbody>
    ${ac.rows.map((r) => r.error ? `<tr>
      <td><strong>${esc(r.symbol || "")}</strong> <span class="muted">${esc(r.name || "")}</span></td>
      <td colspan="6" class="muted">Unavailable: ${esc(r.error)}</td><td></td></tr>`
      : `<tr>
      <td><strong>${esc(r.symbol || "")}</strong> <span class="muted">${esc(r.name || "")}</span><br><span class="muted">${esc(r.primary_chain === "tron" ? "Tron" : "Ethereum")} · ${r.holders_total != null ? num(r.holders_total) + " holders" : ""}${r.scan_window_label ? ` · ${num(r.scanned_transfers)} transfers over ${esc(r.scan_window_label)}` : ""}</span></td>
      <td class="r">${winCell(r.active_24h, r.active_24h_complete)}${r.active_share_of_holders_pct != null ? `<br><span class="muted">${Number(r.active_share_of_holders_pct).toFixed(2)}% of holders</span>` : ""}</td>
      <td class="r">${winCell(r.tx_24h, r.tx_24h_complete)}<br><span class="muted">${r.daily_basis === "extrapolated_sample" ? "daily rate extrapolated" : num(r.tx_per_day) + "/day · " + esc(r.daily_basis || "")}</span></td>
      <td class="r">${winCell(r.volume_24h_usd, r.volume_24h_complete)}<br><span class="muted">${usdBig(r.volume_per_day_usd)}/day</span></td>
      <td class="r">${r.median_transfer_usd != null ? usdBig(r.median_transfer_usd) : "—"}<br><span class="muted">${esc(r.median_transfer_basis || "")}</span></td>
      <td class="r">${vel(r.velocity_annualized)}<br><span class="muted">vs total supply ${vel(r.velocity_vs_total_supply)}</span></td>
      <td class="r">${r.trends_comparable ? `tx ${chg(r.trend_tx_24h_pct)} · vol ${chg(r.trend_volume_24h_pct)} · active ${chg(r.trend_active_24h_pct)}` : '<span class="muted">not observable (prior 24h not covered by scan)</span>'}</td>
      <td class="r">${r.scope_url ? scopeLink(r.scope_url + "/#/", "Open →") : "—"}</td>
    </tr>`).join("")}
  </tbody></table></div>
  <div class="total-row"><span>24h volume, complete windows only — summed (${num(ac.coins_with_complete_24h)} of ${num(ac.coins_included)} coins complete)</span><span>${usdBig(ac.total_volume_24h_complete_only)}</span></div>
  <div class="total-row"><span>Estimated daily transfer volume, all 8 coins (observed + extrapolated daily rates)</span><span>${usdBig(ac.total_volume_per_day_usd_estimate)}</span></div>
  <div class="total-row"><span>Estimated daily transfers, all 8 coins</span><span>${num(ac.total_tx_per_day_estimate)}</span></div>
  <div style="padding:12px 16px" class="muted">${esc(ac.note || "")}</div>`;
}
function renderPaymentsReadiness(pr) {
  const el = $("#payments-readiness-body");
  if (!el) return;
  if (!pr || !Array.isArray(pr.rows) || !pr.rows.length) {
    el.innerHTML = sectionError("Stablecoin Payments Readiness", "Payments-readiness rollup unavailable right now.");
    return;
  }
  const feeFmt = (v) => (v != null ? "$" + Number(v).toFixed(4) : "—");
  const finFmt = (s) => {
    if (s == null) return "—";
    const x = Number(s);
    if (x < 60) return "~" + x + "s";
    if (x < 3600) return "~" + Math.round(x / 60) + " min";
    return "~" + (x / 3600).toFixed(1) + "h";
  };
  const bandBadge = (b) => b === "ready" ? '<span class="badge ok">Ready</span>'
    : b === "usable" ? '<span class="badge grey">Usable</span>'
    : b === "limited" ? '<span class="badge warn">Limited</span>'
    : b === "not_ready" ? '<span class="badge fail">Not ready</span>'
    : '<span class="badge grey">Unknown</span>';
  const sub = (v) => (v != null ? Number(v).toFixed(1) : "—");
  const freezeCell = (r) => {
    const fns = r.freeze_functions_found || [];
    if (fns.length) return `freeze fn present (${esc(fns.join(", "))})`;
    if (r.freeze_score != null && r.freeze_score >= 100) return "no freeze fn found";
    if (r.freeze_score != null && r.freeze_score >= 70) return "pause-only";
    return "—";
  };
  el.innerHTML = `<div style="overflow-x:auto"><table class="tbl"><thead><tr><th>#</th><th>Stablecoin</th><th class="r">Score</th><th class="r">Peg</th><th class="r">Liquidity</th><th class="r">Chains</th><th class="r">Fees / finality</th><th class="r">Freeze risk</th><th></th></tr></thead><tbody>
    ${pr.rows.map((r) => r.error ? `<tr>
      <td>—</td><td><strong>${esc(r.symbol || "")}</strong> <span class="muted">${esc(r.name || "")}</span></td>
      <td colspan="7" class="muted">Unavailable: ${esc(r.error)}</td><td></td></tr>`
      : `<tr>
      <td><strong>${r.rank != null ? num(r.rank) : "—"}</strong></td>
      <td><strong>${esc(r.symbol || "")}</strong> <span class="muted">${esc(r.name || "")}</span><br><span class="muted">${esc(r.primary_chain === "tron" ? "Tron" : "Ethereum")} · ${r.material_chains != null ? num(r.material_chains) + " material chain(s) of " + num(r.chain_count) : ""}${r.inputs_unknown && r.inputs_unknown.length ? ` · unknown: ${esc(r.inputs_unknown.join(", "))}` : ""}</span></td>
      <td class="r"><strong>${r.score != null ? Number(r.score).toFixed(1) : "—"}</strong> ${bandBadge(r.band)}</td>
      <td class="r">${sub(r.peg_score)}<br><span class="muted">${r.deviation_bps != null ? (Number(r.deviation_bps) >= 0 ? "+" : "") + Number(r.deviation_bps).toFixed(2) + " bps" : "—"}</span></td>
      <td class="r">${sub(r.liquidity_score)}<br><span class="muted">${r.near_par_liquidity_usd != null ? usdBig(r.near_par_liquidity_usd) + " near-par" : "—"}</span></td>
      <td class="r">${sub(r.chain_coverage_score)}<br><span class="muted">${r.material_chains != null ? num(r.material_chains) + " ≥1% chains" : "—"}</span></td>
      <td class="r">${sub(r.fees_score)}<br><span class="muted">${feeFmt(r.typical_transfer_fee_usd)} / ${finFmt(r.finality_seconds)}</span></td>
      <td class="r">${sub(r.freeze_score)}<br><span class="muted">${freezeCell(r)}</span></td>
      <td class="r">${r.scope_url ? scopeLink(r.scope_url + "/#/", "Open →") : "—"}</td>
    </tr>`).join("")}
  </tbody></table></div>
  <div class="total-row"><span>Coins scored (${num(pr.coins_scored)} of ${num(pr.coins_included)} included)</span><span>${pr.rows.filter((r) => !r.error && r.band === "ready").length} ready · ${pr.rows.filter((r) => !r.error && r.band === "usable").length} usable</span></div>
  <div style="padding:12px 16px" class="muted">${esc(pr.methodology || "")}<br>${esc(pr.note || "")}</div>`;
}
function renderDefi(df) {
  const el = $("#defi-body");
  if (!el) return;
  if (!df || !Array.isArray(df.rows) || !df.rows.length) {
    el.innerHTML = sectionError("Stablecoin DeFi Deployment & Yield", "DeFi rollup unavailable right now.");
    return;
  }
  const apy = (v) => (v != null ? Number(v).toFixed(2) + "%" : "—");
  const range = (r) => (r.lending_apy_min_pct != null && r.lending_apy_max_pct != null)
    ? `${apy(r.lending_apy_min_pct)} – ${apy(r.lending_apy_max_pct)}` : "—";
  el.innerHTML = `<div style="overflow-x:auto"><table class="tbl"><thead><tr><th>#</th><th>Stablecoin</th><th class="r">Best lending APY</th><th class="r">Lending APY range</th><th class="r">Lending markets</th><th class="r">Lending TVL</th><th class="r">Largest LP pair</th><th class="r">LP TVL</th><th class="r">Total DeFi TVL</th><th class="r">Share of supply</th><th></th></tr></thead><tbody>
    ${df.rows.map((r) => r.error ? `<tr>
      <td>—</td><td><strong>${esc(r.symbol || "")}</strong> <span class="muted">${esc(r.name || "")}</span></td>
      <td colspan="8" class="muted">Unavailable: ${esc(r.error)}</td><td></td></tr>`
      : `<tr>
      <td><strong>${r.rank != null ? num(r.rank) : "—"}</strong></td>
      <td><strong>${esc(r.symbol || "")}</strong> <span class="muted">${esc(r.name || "")}</span><br><span class="muted">${r.majors_absent && r.majors_absent.length ? "no pool for: " + esc(r.majors_absent.join(", ")) : "all named majors present"}</span></td>
      <td class="r"><strong>${apy(r.best_lending_apy_pct)}</strong><br><span class="muted">${esc(r.best_lending_protocol || "")}${r.best_lending_chain ? " · " + esc(r.best_lending_chain) : ""}</span></td>
      <td class="r">${range(r)}<br><span class="muted">median ${apy(r.lending_apy_median_pct)} · wtd ${apy(r.lending_apy_tvl_weighted_pct)}</span></td>
      <td class="r">${num(r.lending_markets_total)}</td>
      <td class="r">${usdBig(r.lending_tvl_usd)}</td>
      <td class="r">${r.top_lp_pair ? `<strong>${esc(r.top_lp_pair)}</strong><br><span class="muted">${esc(r.top_lp_project || "")}${r.top_lp_chain ? " · " + esc(r.top_lp_chain) : ""} · ${usdBig(r.top_lp_tvl_usd)} · ${apy(r.top_lp_apy_pct)}</span>` : "—"}</td>
      <td class="r">${usdBig(r.lp_tvl_usd)}<br><span class="muted">${num(r.lp_pairs_total)} pairs</span></td>
      <td class="r"><strong>${usdBig(r.defi_tvl_usd)}</strong></td>
      <td class="r">${r.defi_tvl_share_of_supply_pct != null ? Number(r.defi_tvl_share_of_supply_pct).toFixed(2) + "%" : "—"}</td>
      <td class="r">${r.scope_url ? scopeLink(r.scope_url + "/#/", "Open →") : "—"}</td>
    </tr>`).join("")}
  </tbody></table></div>
  <div class="total-row"><span>Total DeFi TVL — all ${num(df.coins_included)} coins (lending + LP)</span><span>${usdBig(df.total_defi_tvl_usd)}</span></div>
  <div class="total-row"><span>Lending TVL / LP TVL</span><span>${usdBig(df.total_lending_tvl_usd)} / ${usdBig(df.total_lp_tvl_usd)}</span></div>
  <div style="padding:12px 16px" class="muted">${esc(df.methodology || "")}<br>${esc(df.note || "")}${df.as_of ? `<br>Yields snapshot as of ${new Date(Number(df.as_of) * 1000).toISOString().slice(0, 19).replace("T", " ")} UTC.` : ""}</div>`;
}
function renderFreeze(fz) {
  const el = $("#freeze-body");
  if (!el) return;
  if (!fz || !Array.isArray(fz.rows) || !fz.rows.length) {
    el.innerHTML = sectionError("Stablecoin Freeze & Blacklist", "Freeze & blacklist rollup unavailable right now.");
    return;
  }
  const capCell = (r) => {
    if (r.can_freeze == null) return "—";
    if (!r.can_freeze) return '<span class="badge ok">No freeze fn</span>';
    const fns = r.probe_functions_found || [];
    return '<span class="badge fail">Can freeze</span><br><span class="muted">' + esc(fns.length ? fns.join(", ") : "write-only (ABI)") + "</span>";
  };
  const frzCell = (r) => {
    if (r.frozen_supply_status === "not_applicable") return '<span class="muted">n/a — no freeze fn</span>';
    if (r.frozen_supply_status === "none_observed") return '<span class="muted">none in window</span>';
    if (r.frozen_supply_usd == null) return "—";
    return `<strong>${usdBig(r.frozen_supply_usd)}</strong><br><span class="muted">${num(r.frozen_addresses_count)} addr(s) · ${r.frozen_share_of_primary_supply_pct != null ? Number(r.frozen_share_of_primary_supply_pct).toFixed(4) + "% of primary supply" : ""}</span>`;
  };
  const evCell = (r) => {
    if (r.events_status === "not_applicable") return '<span class="muted">n/a</span>';
    if (r.events_status === "unavailable") return '<span class="muted">unavailable</span>';
    return `${num(r.events_total)}<br><span class="muted">${r.events_window_days ? num(r.events_window_days) + "d window" : ""}${r.events_extended_lookback ? " (5y lookback)" : ""}</span>`;
  };
  el.innerHTML = `<div style="overflow-x:auto"><table class="tbl"><thead><tr><th>Stablecoin</th><th>Freeze capability</th><th class="r">Frozen supply (lower-bound est.)</th><th class="r">Freeze events</th><th class="r">Black funds destroyed</th><th class="r">Sanctions matches</th><th></th></tr></thead><tbody>
    ${fz.rows.map((r) => r.error ? `<tr>
      <td><strong>${esc(r.symbol || "")}</strong> <span class="muted">${esc(r.name || "")}</span></td>
      <td colspan="5" class="muted">Unavailable: ${esc(r.error)}</td><td></td></tr>`
      : `<tr>
      <td><strong>${esc(r.symbol || "")}</strong> <span class="muted">${esc(r.name || "")}</span><br><span class="muted">${esc(r.primary_chain === "tron" ? "Tron" : "Ethereum")}</span></td>
      <td>${capCell(r)}${r.pause_function_found ? '<br><span class="muted">pause also present</span>' : ""}</td>
      <td class="r">${frzCell(r)}</td>
      <td class="r">${evCell(r)}</td>
      <td class="r">${r.destroyed_total_usd != null ? usdBig(r.destroyed_total_usd) + `<br><span class="muted">${num(r.destroyed_events_count)} event(s)</span>` : '<span class="muted">no destroy fn</span>'}</td>
      <td class="r">${r.sanctions_matches_total ? `<strong>${num(r.sanctions_matches_total)}</strong>` : "0"}</td>
      <td class="r">${r.scope_url ? scopeLink(r.scope_url + "/#/", "Open →") : "—"}</td>
    </tr>`).join("")}
  </tbody></table></div>
  <div class="total-row"><span>Total frozen supply — LOWER-BOUND ESTIMATE (${num(fz.coins_measured)} of ${num(fz.coins_included)} coins measured)</span><span>${usdBig(fz.total_frozen_supply_usd_lower_bound)}</span></div>
  <div class="total-row"><span>Currently-frozen addresses (scanned windows only)</span><span>${num(fz.total_frozen_addresses)}</span></div>
  <div class="total-row"><span>Total freeze/blacklist events in windows</span><span>${num(fz.total_freeze_events)}</span></div>
  <div class="total-row"><span>Total black funds destroyed (seized)</span><span>${usdBig(fz.total_destroyed_usd)}</span></div>
  <div class="total-row"><span>Sanctions matches across all 8 coins</span><span>${num(fz.total_sanctions_matches)}</span></div>
  <div style="padding:12px 16px" class="muted">${esc(fz.note || "")}</div>`;
}
/* Ranking table for the issuer-risk rollup — shared by the home
   section (renderIssuerRisk) and the dedicated #/issuer-risk detail
   page, so both show the same ranking. `ir` is a /issuer-risk
   payload, or the `ranking` half of /issuer-risk/detail. */
function issuerRiskTableHtml(ir) {
  const riskBadge = (b) => b === "low" ? '<span class="badge ok">Low</span>'
    : b === "moderate" ? '<span class="badge grey">Moderate</span>'
    : b === "elevated" ? '<span class="badge warn">Elevated</span>'
    : b === "high" ? '<span class="badge fail">High</span>'
    : '<span class="badge grey">Unknown</span>';
  const sub = (v) => (v != null ? Number(v).toFixed(1) : "—");
  const pct = (v) => (v != null ? (Number(v) >= 0 ? "+" : "") + Number(v).toFixed(2) + "%" : "—");
  return `<div style="overflow-x:auto"><table class="tbl"><thead><tr><th>#</th><th>Stablecoin</th><th class="r">Risk score</th><th class="r">Depeg</th><th class="r">Reserve transparency</th><th class="r">Issuance</th><th class="r">Concentration</th><th class="r">Freeze exposure</th><th class="r">Adoption</th><th></th></tr></thead><tbody>
    ${ir.rows.map((r) => r.error ? `<tr>
      <td>—</td><td><strong>${esc(r.symbol || "")}</strong> <span class="muted">${esc(r.name || "")}</span></td>
      <td colspan="8" class="muted">Unavailable: ${esc(r.error)}</td><td></td></tr>`
      : `<tr>
      <td><strong>${r.rank != null ? num(r.rank) : "—"}</strong></td>
      <td><strong>${esc(r.symbol || "")}</strong> <span class="muted">${esc(r.name || "")}</span><br><span class="muted">${esc(r.issuer || "")}${r.inputs_unknown && r.inputs_unknown.length ? ` · unknown: ${esc(r.inputs_unknown.join(", "))}` : ""}</span></td>
      <td class="r"><strong>${r.score != null ? Number(r.score).toFixed(1) : "—"}</strong> ${riskBadge(r.band)}</td>
      <td class="r">${sub(r.depeg_score)}<br><span class="muted">${r.deviation_bps != null ? (Number(r.deviation_bps) >= 0 ? "+" : "") + Number(r.deviation_bps).toFixed(2) + " bps" : "—"}${r.depeg_days_365d != null ? ` · ${num(r.depeg_days_365d)} depeg day(s)/365d` : ""}</span></td>
      <td class="r">${sub(r.reserve_score)}<br><span class="muted">${esc(r.attestation_status_label || r.attestation_status || "—")}${r.auditor ? ` · ${esc(r.auditor)}` : ""}${r.days_since_attestation != null ? ` · ${num(r.days_since_attestation)}d ago` : ""}${r.reserve_to_outstanding_ratio != null ? ` · reserves/outstanding ${Number(r.reserve_to_outstanding_ratio).toFixed(4)}x` : ""}</span></td>
      <td class="r">${sub(r.issuance_score)}<br><span class="muted">${r.flat_supply ? "supply frozen" : `30d ${pct(r.net_30d_pct_of_supply)} · 90d ${pct(r.net_90d_pct_of_supply)}${r.demand_direction ? ` · ${esc(r.demand_direction)}` : ""}`}</span></td>
      <td class="r">${sub(r.concentration_score)}<br><span class="muted">${r.top_10_share_pct != null ? "top 10: " + Number(r.top_10_share_pct).toFixed(2) + "%" : "—"}${r.exchange_share_pct != null ? ` · exch. ${Number(r.exchange_share_pct).toFixed(2)}% (lower bound)` : ""}</span></td>
      <td class="r">${sub(r.freeze_score)}<br><span class="muted">${r.can_freeze == null ? "—" : r.can_freeze ? `can freeze · ${num(r.freeze_events_total)} event(s)` : "no freeze fn"}${r.frozen_supply_usd != null ? ` · frozen ${usdBig(r.frozen_supply_usd)}` : ""}${r.sanctions_matches_total ? ` · ${num(r.sanctions_matches_total)} SDN match(es)` : ""}</span></td>
      <td class="r">${sub(r.adoption_score)}<br><span class="muted">${r.material_chains != null ? num(r.material_chains) + " material chain(s)" : "—"}${r.typical_transfer_fee_usd != null ? ` · $${Number(r.typical_transfer_fee_usd).toFixed(4)}/transfer` : ""}</span></td>
      <td class="r">${r.scope_url ? scopeLink(r.scope_url + "/#/", "Open →") : "—"}</td>
    </tr>`).join("")}
  </tbody></table></div>
  <div class="total-row"><span>Coins scored (${num(ir.coins_scored)} of ${num(ir.coins_included)} included) — rank 1 = least risky issuer</span><span>${ir.rows.filter((r) => !r.error && r.band === "low").length} low · ${ir.rows.filter((r) => !r.error && r.band === "moderate").length} moderate · ${ir.rows.filter((r) => !r.error && r.band === "elevated").length} elevated · ${ir.rows.filter((r) => !r.error && r.band === "high").length} high</span></div>`;
}
function renderIssuerRisk(ir) {
  const el = $("#issuer-risk-body");
  if (!el) return;
  if (!ir || !Array.isArray(ir.rows) || !ir.rows.length) {
    el.innerHTML = sectionError("Stablecoin Issuer Risk", "Issuer-risk rollup unavailable right now.");
    return;
  }
  el.innerHTML = issuerRiskTableHtml(ir)
    + `<div style="padding:12px 16px" class="muted">${esc(ir.methodology || "")}<br>${esc(ir.note || "")}</div>`
    + `<div style="padding:0 16px 12px"><a href="#/issuer-risk">Full per-issuer breakdowns →</a> <span class="muted">ranking + all six input breakdowns for every issuer (GET /api/overview/issuer-risk/detail)</span></div>`;
}
/* ---------------- attestations ---------------- */
/* Attestation monitor (phase 3, item 7): GET
   /api/overview/attestations — the feature-3 tracker reused
   wholesale per coin. Shared table for the home section
   (renderAttestations) and the dedicated #/attestations page,
   so both show the same monitor. */
function attestationStatusBadge(s) {
  return s === "overdue" ? '<span class="badge fail">Overdue</span>'
    : s === "due_soon" ? '<span class="badge warn">Due soon</span>'
    : s === "current" ? '<span class="badge ok">Current</span>'
    : s === "live" ? '<span class="badge info">Live</span>'
    : s === "on_chain" ? '<span class="badge grey">On-chain</span>'
    : s === "no_attestation" ? '<span class="badge grey">No attestation</span>'
    : `<span class="badge grey">${esc(s || "—")}</span>`;
}
function attestationsTableHtml(d) {
  const ratioCell = (c) => {
    const r2s = c.reserve_to_supply_ratio != null ? Number(c.reserve_to_supply_ratio).toFixed(4) + "x supply" : null;
    const r2o = c.reserve_to_outstanding_ratio != null ? Number(c.reserve_to_outstanding_ratio).toFixed(4) + "x outst." : null;
    if (!r2s && !r2o) return '<span class="muted">no dated figure verified</span>';
    return `${r2s || "—"}${r2o ? `<br><span class="muted">${r2o}</span>` : ""}${c.reserve_figures_as_of ? `<br><span class="muted">as of ${esc(c.reserve_figures_as_of)}</span>` : ""}`;
  };
  const srcCell = (c) => {
    const links = [];
    if (c.last_attestation_source) links.push(`<a href="${esc(c.last_attestation_source)}" target="_blank" rel="noopener">Latest report ↗</a>`);
    if (c.scope_url && c.scope) links.push(`<a href="${esc(c.scope_url)}/api/${esc(c.scope)}/reserves" target="_blank" rel="noopener">Reserves API ↗</a>`);
    return links.length ? links.join(" · ") : "—";
  };
  return `<div style="overflow-x:auto"><table class="tbl"><thead><tr><th>Stablecoin</th><th>Auditor</th><th>Cadence</th><th class="r">Last attestation</th><th class="r">Days since</th><th>Status</th><th class="r">Next expected</th><th class="r">Overdue by</th><th class="r">Reserve ratio</th><th>Sources</th></tr></thead><tbody>
    ${d.coins.map((c) => c.error ? `<tr>
      <td><strong>${esc(c.symbol || "")}</strong> <span class="muted">${esc(c.name || "")}</span></td>
      <td colspan="8" class="muted">Unavailable: ${esc(c.error)}</td><td></td></tr>`
      : `<tr>
      <td><strong>${esc(c.symbol || "")}</strong> <span class="muted">${esc(c.name || "")}</span><br><span class="muted">${esc(c.issuer || "")}</span></td>
      <td>${esc(c.auditor || "—")}</td>
      <td>${esc(c.cadence || "—")}</td>
      <td class="r">${esc(c.last_attestation || "—")}</td>
      <td class="r">${c.days_since != null ? num(c.days_since) + "d" : "—"}</td>
      <td>${attestationStatusBadge(c.status)}<br><span class="muted">${esc(c.status_label || "")}</span></td>
      <td class="r">${esc(c.next_expected || "—")}</td>
      <td class="r">${c.overdue_by_days != null ? `<b style="color:var(--red)">${num(c.overdue_by_days)}d</b>` : "—"}</td>
      <td class="r">${ratioCell(c)}</td>
      <td>${srcCell(c)}</td>
    </tr>`).join("")}
  </tbody></table></div>
  <div class="total-row"><span>Coins included (${num(d.coins_included)} of 8) — ordered overdue first</span><span>${(d.summary && d.summary.overdue && d.summary.overdue.length) ? `<b style="color:var(--red)">${num(d.summary.overdue.length)} overdue: ${esc(d.summary.overdue.join(", "))}</b>` : "none overdue"}</span></div>`;
}
function renderAttestations(d) {
  const el = $("#attestations-body");
  if (!el) return;
  if (!d || !Array.isArray(d.coins) || !d.coins.length) {
    el.innerHTML = sectionError("Stablecoin Attestations", "Attestation monitor unavailable right now.");
    return;
  }
  el.innerHTML = attestationsTableHtml(d)
    + `<div style="padding:12px 16px" class="muted">${esc(d.note || "")}</div>`
    + `<div style="padding:0 16px 12px"><a href="#/attestations">Full attestation monitor →</a> <span class="muted">all 8 coins with methodology, history coverage and honest limits (GET /api/overview/attestations)</span></div>`;
}
async function attestationMonitorPage() {
  document.title = "Attestation monitor — " + TITLE;
  render(`<div class="pagehead"><h2>Attestation monitor — all 8 stablecoins</h2></div>
    <div class="card"><div class="card-head">Status <span class="muted">(ordered overdue first)</span></div><div id="att-monitor-table">${spinner()}</div></div>
    <div class="card"><div style="padding:14px 16px" id="att-monitor-method">${spinner()}</div></div>`);
  let d = null;
  try {
    d = await fetchJson(API + "/attestations", null, 60000);
  } catch (e) {
    render(`<div class="pagehead"><h2>Attestation monitor — all 8 stablecoins</h2></div><div class="card">${errBox("Could not load attestation monitor: " + e.message)}</div>`);
    return;
  }
  const t = $("#att-monitor-table");
  if (t) t.innerHTML = attestationsTableHtml(d);
  const m = $("#att-monitor-method");
  if (m) {
    const h = d.history || {};
    m.innerHTML = `<b>Methodology:</b> ${esc(d.methodology || "")}`
      + `<br><br><b>History coverage:</b> ${esc(h.coverage || "—")} — ${esc(h.note || "")}`
      + `${(d.limits || []).length ? `<br><br><b>Honest limits:</b><ul>${d.limits.map((l) => `<li>${esc(l)}</li>`).join("")}</ul>` : ""}`
      + `<br><span class="muted">API: <a href="#/api-docs">GET /api/overview/attestations</a> · per-coin tracker: GET /api/&lt;scope&gt;/reserves (attestation_tracker) · as of ${d.as_of ? new Date(Number(d.as_of) * 1000).toISOString().slice(0, 19).replace("T", " ") + " UTC" : "—"}</span>`;
  }
}
/* ---------------- supply & flow feed ---------------- */
/* Dedicated demo page for the supply & flow feed (phase 3, item 8):
   GET /api/overview/supply-feed — daily net-issuance series + large
   on-chain mint/burn events across all 8 stablecoin scopes,
   filterable by coin, record kind and time window, paginated. Daily
   rows are the SERIES form of net issuance (labelled on every row),
   never discrete mint/burn events. A cold load runs 8 coins'
   mint/burn evaluations (Blockscout zero-address scans) and
   supply-map parses and can take ~40s; repeat loads are served from
   the 300s server cache. */
const SUPPLY_FEED_COINS = ["USDT", "USDC", "DAI", "USDe", "FDUSD", "PYUSD", "TUSD", "USDD"];
const SUPPLY_FEED_KINDS = ["net_change", "mint", "burn"];
const SUPPLY_FEED_KIND_LABELS = { net_change: "Daily net change (series)", mint: "Mint (on-chain event)", burn: "Burn (on-chain event)" };
let supplyFeedRun = 0;

function supplyFeedResultsHtml(d) {
  const signedUsd = (v) => {
    if (v == null || v === "") return "—";
    const x = Number(v);
    if (!Number.isFinite(x)) return "—";
    const color = x > 0 ? "var(--green)" : x < 0 ? "var(--red)" : "inherit";
    return `<span style="color:${color};font-weight:700">${x > 0 ? "+" : x < 0 ? "−" : ""}${usdBig(Math.abs(x))}</span>`;
  };
  const t = d.totals || {};
  const countsCoin = Object.entries(d.counts_by_coin || {}).map(([c, v]) => `${esc(c)}: ${num(v)}`).join(" · ") || "none";
  const countsKind = Object.entries(d.counts_by_kind || {}).map(([k, v]) => `${esc(SUPPLY_FEED_KIND_LABELS[k] || k)}: ${num(v)}`).join(" · ") || "none";
  const dateStr = (ts) => ts ? new Date(Number(ts) * 1000).toISOString().slice(0, 10) : "—";
  const txLink = (e) => {
    if (!e.tx_hash) return "—";
    const url = e.chain === "tron" ? `https://tronscan.org/#/transaction/${e.tx_hash}` : `https://eth.blockscout.com/tx/${e.tx_hash}`;
    return `<a href="${esc(url)}" target="_blank" rel="noopener" class="mono">${esc(e.tx_hash.slice(0, 10))}…</a>`;
  };
  const coverage = (d.coins || []).map((c) => {
    if (c.error) return `<div class="detail"><div class="dk">${esc(c.symbol || "")}</div><div class="dv"><span class="badge fail">Unavailable</span> <span class="muted">${esc(c.error)}</span></div></div>`;
    const ev = c.on_chain_events || {};
    const gap = c.supply_gap;
    const chains = (c.chains || []).map((ch) => {
      const c7 = ch.change_7d || {}, c30 = ch.change_30d || {};
      return `${esc(ch.chain)} ${usdBig(ch.supply_usd)}${ch.share_pct != null ? ` (${Number(ch.share_pct).toFixed(1)}%)` : ""} · 7d ${signedUsd(c7.net_usd)} · 30d ${signedUsd(c30.net_usd)}`;
    }).join("<br>");
    return `<div class="detail"><div class="dk">${esc(c.symbol)} <span class="muted">(${esc(c.issuer || "")})</span></div><div class="dv">
      Supply ${usdBig(c.supply_usd)} · net 7d ${signedUsd(c.net_7d_usd)}${c.net_7d_partial ? " (partial)" : ""} · 30d ${signedUsd(c.net_30d_usd)}${c.net_30d_partial ? " (partial)" : ""} · 90d ${signedUsd(c.net_90d_usd)}${c.net_90d_partial ? " (partial)" : ""}
      ${c.flat_supply ? ` · <span class="badge grey">Flat supply — frozen</span>` : ""}
      ${gap ? ` · <span class="badge warn">Series gap ${num(gap.gap_days)}d (${esc(gap.start ? gap.start.slice(0, 10) : "")} → ${esc(gap.end ? gap.end.slice(0, 10) : "")})</span>` : ""}
      <br><span class="muted">History ${dateStr(c.supply_history_first)} → ${dateStr(c.supply_history_last)} (${num(c.supply_history_points)} days) · on-chain events: ${ev.status === "unavailable" ? '<span class="badge warn">unavailable</span>' : num(ev.count) + " large event(s)"}${ev.status_note ? ` — ${esc(ev.status_note)}` : ""}</span>
      ${chains ? `<br><span class="muted">Top chains (net change = bridges + local mint/burn, not a bridge flow):<br>${chains}</span>` : ""}
      ${c.chain_note ? `<br><span class="muted">${esc(c.chain_note)}</span>` : ""}
    </div></div>`;
  }).join("");
  const rows = (d.items || []).map((e) => {
    if (e.record_type === "on_chain_event") {
      return `<tr><td class="mono">${e.date ? esc(e.date.slice(0, 19).replace("T", " ")) + " UTC" : "—"}</td><td><b>${esc(e.coin || "")}</b> <span class="muted">${esc(e.issuer || "")}</span></td><td><span class="badge ${e.kind === "mint" ? "ok" : "fail"}">${e.kind === "mint" ? "Mint" : "Burn"}</span> <span class="muted">on-chain event</span></td><td class="r">${e.amount != null ? `${num(e.amount)} ${esc(e.coin || "")}` : "—"}</td><td class="r">${signedUsd(e.amount_usd)}</td><td>${txLink(e)}</td></tr>`;
    }
    return `<tr><td class="mono">${e.date ? esc(e.date.slice(0, 10)) : "—"}</td><td><b>${esc(e.coin || "")}</b> <span class="muted">${esc(e.issuer || "")}</span></td><td><span class="badge grey">Net change</span> <span class="muted">daily series — not an event</span></td><td class="r">${signedUsd(e.delta_usd)}${e.delta_pct != null ? ` <span class="muted">(${Number(e.delta_pct) >= 0 ? "+" : ""}${Number(e.delta_pct).toFixed(3)}%)</span>` : ""}</td><td class="r"><span class="muted">supply after day</span> ${usdBig(e.supply_usd)}</td><td>—</td></tr>`;
  }).join("");
  return `
  <div class="card"><div style="padding:14px 16px">
    <div style="margin-bottom:8px"><b>${num(d.total_filtered)}</b> row(s) match${d.total_filtered !== d.total_items ? ` (of ${num(d.total_items)} in the time window)` : ""} · page ${num(d.page)} of ${num(d.total_pages)}</div>
    <div class="muted" style="font-size:12px">By coin: ${countsCoin} · By record: ${countsKind}</div>
    <div style="margin-top:10px;display:flex;gap:16px;flex-wrap:wrap">
      <div><div class="muted" style="font-size:12px">Total net issuance · 7d</div><div style="font-size:18px">${signedUsd(t.total_net_7d_usd)}</div></div>
      <div><div class="muted" style="font-size:12px">Total net issuance · 30d</div><div style="font-size:18px">${signedUsd(t.total_net_30d_usd)}</div></div>
      <div><div class="muted" style="font-size:12px">Total net issuance · 90d</div><div style="font-size:18px">${signedUsd(t.total_net_90d_usd)}</div></div>
      <div><div class="muted" style="font-size:12px">Total supply (${num(t.coins_included)} of 8 coins)</div><div style="font-size:18px">${usdBig(t.total_supply_usd)}</div></div>
      <div><div class="muted" style="font-size:12px">Large on-chain events in window</div><div style="font-size:18px">${num(t.large_events_in_window)}</div></div>
    </div>
    <div class="muted" style="font-size:12px;margin-top:8px">${esc(t.note || "")}</div>
    <div style="margin-top:10px">${coverage}</div>
  </div></div>
  <div class="card">${d.items && d.items.length ? `<div style="overflow-x:auto"><table class="tbl"><thead><tr><th>Date / time (UTC)</th><th>Coin</th><th>Record</th><th class="r">Net change / amount</th><th class="r">USD value / supply</th><th>Tx</th></tr></thead><tbody>${rows}</tbody></table></div>
  <div style="padding:12px 16px;display:flex;gap:8px;align-items:center"><button class="btn" id="sf-prev"${d.page <= 1 ? " disabled" : ""}>← Prev</button><button class="btn" id="sf-next"${d.page >= d.total_pages ? " disabled" : ""}>Next →</button><span class="muted" style="font-size:12px">Page ${num(d.page)} of ${num(d.total_pages)} · ${num(d.total_filtered)} row(s)</span></div>`
  : `<div class="empty">No supply/flow rows match these filters in the time window.</div>`}</div>
  <div class="card"><div style="padding:12px 16px"><div class="muted" style="font-size:12px">Honest limits</div><ul class="risk-reasons muted" style="font-size:12px">${(d.limits || []).map((n) => `<li>${esc(n)}</li>`).join("")}</ul><div class="muted" style="font-size:12px;margin-top:6px">${esc(d.method || "")}</div></div></div>`;
}

async function supplyFeedPage() {
  clearPoll();
  const run = ++supplyFeedRun;
  const alive = () => run === supplyFeedRun && document.getElementById("supply-feed-root");
  document.title = "Supply & Flow Feed — " + TITLE;
  const q = new URLSearchParams((location.hash.split("?")[1] || ""));
  const qCoins = (q.get("coin") || "").split(",").map((s) => s.trim()).filter(Boolean);
  const state = {
    coins: qCoins.length ? qCoins : [...SUPPLY_FEED_COINS],
    kind: q.get("kind") || "",
    days: q.get("days") || "30",
    from: q.get("from") || "",
    to: q.get("to") || "",
    page: 1, page_size: 50,
  };
  const kindOptions = (sel) => `<option value="">All records</option>` + SUPPLY_FEED_KINDS.map((k) => `<option value="${esc(k)}"${k === sel ? " selected" : ""}>${esc(SUPPLY_FEED_KIND_LABELS[k] || k)}</option>`).join("");
  render(`
    <div id="supply-feed-root">
    <div class="pagehead"><h2>📈 Supply &amp; Flow Feed</h2></div>
    <div class="card"><div style="padding:16px">
      <p class="muted" style="margin-top:0">Mint/burn events and net issuance across all 8 stablecoins — daily net-change rows (the <b>series form</b> of net issuance: a daily net figure, <b>not</b> a discrete mint/burn event) plus large individual on-chain mints/burns, newest first. Net issuance is net supply change from DeFiLlama, not gross mint/burn. USDT on-chain events are unavailable (Blockscout's zero-address query times out); TUSD supply is frozen; USDD's series has an 83-day gap. API: <a href="#/api-docs">GET /api/overview/supply-feed</a> · per coin: <span class="mono">GET /api/&lt;scope&gt;/supply-feed</span>. See also the <a href="#/">mint / burn rollup</a>.</p>
      <form id="sf-form" autocomplete="off">
        <div style="display:flex;flex-wrap:wrap;gap:12px;margin-bottom:10px" id="sf-coins">
          ${SUPPLY_FEED_COINS.map((c) => `<label class="muted" style="font-size:13px"><input type="checkbox" value="${esc(c)}"${state.coins.includes(c) ? " checked" : ""}> ${esc(c)}</label>`).join("")}
        </div>
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px">
          <label class="field">Record type<select id="sf-kind">${kindOptions(state.kind)}</select></label>
          <label class="field">Last N days<select id="sf-days">${["7", "30", "90", "365"].map((n) => `<option value="${n}"${n === state.days ? " selected" : ""}>${n} days</option>`).join("")}</select></label>
          <label class="field">From<input type="date" id="sf-from" value="${esc(state.from)}"></label>
          <label class="field">To<input type="date" id="sf-to" value="${esc(state.to)}"></label>
          <label class="field">Page size<select id="sf-size"><option value="25">25</option><option value="50" selected>50</option><option value="100">100</option></select></label>
        </div>
        <div style="margin-top:12px;display:flex;gap:8px;align-items:center">
          <button class="btn" type="submit">Apply filters</button>
          <span class="muted" style="font-size:12px">From / To override “Last N days”. Try: <a href="#/supply-feed" data-sf-try="kind:mint">large mints only</a> · <a href="#/supply-feed" data-sf-try="kind:burn">large burns only</a> · <a href="#/supply-feed" data-sf-try="coin:USDD">USDD (gapped series)</a> · <a href="#/supply-feed" data-sf-try="coin:TUSD">TUSD (frozen supply)</a></span>
        </div>
      </form>
    </div></div>
    <div id="sf-results"><div class="card"><div style="padding:16px"><div class="spinner">Loading supply &amp; flow rows… (a cold load runs 8 coins' mint/burn evaluations and can take ~40s)</div></div></div></div>
    </div>`);
  const load = async () => {
    if (!alive()) return;
    $("#sf-results").innerHTML = `<div class="card"><div style="padding:16px"><div class="spinner">Loading supply &amp; flow rows… (a cold load runs 8 coins' mint/burn evaluations and can take ~40s)</div></div></div>`;
    let d;
    try {
      const qs = new URLSearchParams({ page: String(state.page), page_size: String(state.page_size) });
      if (state.coins.length && state.coins.length < SUPPLY_FEED_COINS.length) qs.set("coin", state.coins.join(","));
      if (state.kind) qs.set("kind", state.kind);
      if (state.from) qs.set("from", String(Math.floor(new Date(state.from + "T00:00:00Z").getTime() / 1000)));
      else if (state.days) qs.set("days", state.days);
      if (state.to) qs.set("to", String(Math.floor(new Date(state.to + "T23:59:59Z").getTime() / 1000)));
      d = await fetchJson(API + "/supply-feed?" + qs.toString(), null, 180000);
    } catch (err) {
      if (!alive()) return;
      $("#sf-results").innerHTML = `<div class="card"><div class="empty">Supply &amp; flow feed failed to load: ${esc(err.message)}</div></div>`;
      return;
    }
    if (!alive()) return;
    $("#sf-results").innerHTML = supplyFeedResultsHtml(d);
    const prev = $("#sf-prev"), next = $("#sf-next");
    if (prev) prev.addEventListener("click", () => { if (state.page > 1) { state.page--; load(); } });
    if (next) next.addEventListener("click", () => { if (state.page < d.total_pages) { state.page++; load(); } });
  };
  $("#sf-form").addEventListener("submit", (e) => {
    e.preventDefault();
    state.coins = [...document.querySelectorAll("#sf-coins input:checked")].map((c) => c.value);
    if (!state.coins.length) state.coins = [...SUPPLY_FEED_COINS];
    state.kind = $("#sf-kind").value;
    state.days = $("#sf-days").value;
    state.from = $("#sf-from").value;
    state.to = $("#sf-to").value;
    state.page_size = Number($("#sf-size").value) || 50;
    state.page = 1;
    load();
  });
  document.querySelectorAll("[data-sf-try]").forEach((a) => a.addEventListener("click", (e) => {
    e.preventDefault();
    const v = a.dataset.sfTry;
    if (v.startsWith("kind:")) { state.kind = v.slice(5); $("#sf-kind").value = state.kind; }
    else if (v.startsWith("coin:")) {
      state.coins = [v.slice(5)];
      document.querySelectorAll("#sf-coins input").forEach((c) => { c.checked = state.coins.includes(c.value); });
    }
    state.page = 1; load();
  }));
  load();
}
/* ---------------- whale feed ---------------- */
/* Dedicated demo page for the whale feed (phase 3, item 14):
   GET /api/overview/whale-feed — large-transfer events across all
   8 stablecoin scopes, filterable by coin, minimum USD value and
   time window, paginated. Each coin contributes at most its latest
   10 large transfers (>= max($1M, 0.01% of supply)) from the
   recent-transfers feed — the scanned count and window are shown
   per coin, and an empty list for a coin means "none above its
   threshold in the scanned window". A cold load runs 8 coins'
   whales evaluations and can take ~40s; repeat loads are served
   from the 300s server cache. */
let whaleFeedRun = 0;

function whaleFeedResultsHtml(d) {
  const signedUsd = (v) => {
    if (v == null || v === "") return "—";
    const x = Number(v);
    if (!Number.isFinite(x)) return "—";
    return `<span style="font-weight:700">${usdBig(Math.abs(x))}</span>`;
  };
  const t = d.totals || {};
  const countsCoin = Object.entries(d.counts_by_coin || {}).map(([c, v]) => `${esc(c)}: ${num(v)}`).join(" · ") || "none";
  const countsKind = Object.entries(d.counts_by_kind || {}).map(([k, v]) => `${esc(k)}: ${num(v)}`).join(" · ") || "none";
  const dirBadge = (k) => k === "mint" ? '<span class="badge ok">Mint</span>' : k === "burn" ? '<span class="badge fail">Burn</span>' : '<span class="badge grey">Transfer</span>';
  const addrLink = (a, chain, label) => {
    if (!a) return "—";
    const url = chain === "tron" ? `https://tronscan.org/#/address/${a}` : `https://eth.blockscout.com/address/${a}`;
    return `<a href="${esc(url)}" target="_blank" rel="noopener" class="mono">${esc(a.slice(0, 10))}…</a>${label ? ` <span class="badge grey">${esc(label)}</span>` : ""}`;
  };
  const txLink = (e) => {
    if (!e.tx_hash) return "—";
    const url = e.chain === "tron" ? `https://tronscan.org/#/transaction/${e.tx_hash}` : `https://eth.blockscout.com/tx/${e.tx_hash}`;
    return `<a href="${esc(url)}" target="_blank" rel="noopener" class="mono">${esc(e.tx_hash.slice(0, 10))}…</a>`;
  };
  const winStr = (c) => {
    if (!c.scan_earliest && !c.scan_latest) return "—";
    const f = (ts) => ts ? new Date(Number(ts) * 1000).toISOString().slice(0, 19).replace("T", " ") : "—";
    return `${f(c.scan_earliest)} → ${f(c.scan_latest)}`;
  };
  const coverage = (d.coins || []).map((c) => {
    if (c.error) return `<div class="detail"><div class="dk">${esc(c.symbol || "")}</div><div class="dv"><span class="badge fail">Unavailable</span> <span class="muted">${esc(c.error)}</span></div></div>`;
    const st = c.status === "unavailable" ? '<span class="badge fail">unavailable</span>' : c.status === "partial" ? '<span class="badge warn">partial</span>' : '<span class="badge ok">ok</span>';
    return `<div class="detail"><div class="dk">${esc(c.symbol)} <span class="muted">(${esc(c.issuer || "")})</span></div><div class="dv">
      ${st} · <b>${num(c.events_count)}</b> large transfer(s) (cap ${num(c.cap)}) · threshold ${usdBig(c.threshold_usd)}${c.min_usd_requested != null ? ` · effective min ${usdBig(c.effective_min_usd)} (requested ${usdBig(c.min_usd_requested)})` : ""}
      <br><span class="muted">Scanned ${num(c.scanned_transfers)} recent transfers · window ${winStr(c)} · ${esc(c.chain_scope_note || "")}${c.price_assumed_1_usd ? " USD values assume $1.00 (no live price available)." : ""}</span>
      ${c.status_note ? `<br><span class="muted">${esc(c.status_note)}</span>` : ""}
    </div></div>`;
  }).join("");
  const rows = (d.items || []).map((e) => `<tr><td class="mono">${e.date ? esc(e.date.slice(0, 19).replace("T", " ")) + " UTC" : "—"}</td><td><b>${esc(e.coin || "")}</b> <span class="muted">${esc(e.issuer || "")}</span></td><td>${dirBadge(e.kind)}</td><td>${addrLink(e.from, e.chain, e.from_label)}</td><td>${addrLink(e.to, e.chain, e.to_label)}</td><td class="r">${e.amount != null ? `${num(e.amount)} ${esc(e.coin || "")}` : "—"}</td><td class="r">${signedUsd(e.amount_usd)}</td><td>${txLink(e)}</td></tr>`).join("");
  const largest = t.largest_transfer;
  return `
  <div class="card"><div style="padding:14px 16px">
    <div style="margin-bottom:8px"><b>${num(d.total_filtered)}</b> large transfer(s) match${d.total_filtered !== d.total_items ? ` (of ${num(d.total_items)} in the time window)` : ""} · page ${num(d.page)} of ${num(d.total_pages)}</div>
    <div class="muted" style="font-size:12px">By coin: ${countsCoin} · By direction: ${countsKind}</div>
    <div style="margin-top:10px;display:flex;gap:16px;flex-wrap:wrap">
      <div><div class="muted" style="font-size:12px">Total volume in window</div><div style="font-size:18px">${usdBig(t.total_volume_usd)}</div></div>
      <div><div class="muted" style="font-size:12px">Largest transfer</div><div style="font-size:18px">${largest ? `${usdBig(largest.amount_usd)} (${esc(largest.coin || "")} · ${largest.date ? esc(largest.date.slice(0, 19).replace("T", " ")) + " UTC" : "—"})` : "—"}</div></div>
      <div><div class="muted" style="font-size:12px">Coins included</div><div style="font-size:18px">${num(t.coins_included)} of 8</div></div>
    </div>
    <div class="muted" style="font-size:12px;margin-top:8px">${esc(t.note || "")}</div>
    <div style="margin-top:10px">${coverage}</div>
  </div></div>
  <div class="card">${d.items && d.items.length ? `<div style="overflow-x:auto"><table class="tbl"><thead><tr><th>Time (UTC)</th><th>Coin</th><th>Direction</th><th>From</th><th>To</th><th class="r">Amount</th><th class="r">USD value</th><th>Tx</th></tr></thead><tbody>${rows}</tbody></table></div>
  <div style="padding:12px 16px;display:flex;gap:8px;align-items:center"><button class="btn" id="wf-prev"${d.page <= 1 ? " disabled" : ""}>← Prev</button><button class="btn" id="wf-next"${d.page >= d.total_pages ? " disabled" : ""}>Next →</button><span class="muted" style="font-size:12px">Page ${num(d.page)} of ${num(d.total_pages)} · ${num(d.total_filtered)} transfer(s)</span></div>`
  : `<div class="empty">No large transfers match these filters in the scanned window. Check a coin's coverage above: an empty list means no transfer above its threshold in the transfers scanned (count + window shown), not that no large transfer happened — and a threshold below a coin's own minimum cannot surface smaller transfers.</div>`}</div>
  <div class="card"><div style="padding:12px 16px"><div class="muted" style="font-size:12px">Honest limits</div><ul class="risk-reasons muted" style="font-size:12px">${(d.limits || []).map((n) => `<li>${esc(n)}</li>`).join("")}</ul><div class="muted" style="font-size:12px;margin-top:6px">${esc(d.method || "")}</div></div></div>`;
}

async function whaleFeedPage() {
  clearPoll();
  const run = ++whaleFeedRun;
  const alive = () => run === whaleFeedRun && document.getElementById("whale-feed-root");
  document.title = "Whale Feed — " + TITLE;
  const q = new URLSearchParams((location.hash.split("?")[1] || ""));
  const qCoins = (q.get("coin") || "").split(",").map((s) => s.trim()).filter(Boolean);
  const state = {
    coins: qCoins.length ? qCoins : [...SUPPLY_FEED_COINS],
    min_usd: q.get("min_usd") || "",
    days: q.get("days") || "30",
    from: q.get("from") || "",
    to: q.get("to") || "",
    page: 1, page_size: 50,
  };
  render(`
    <div id="whale-feed-root">
    <div class="pagehead"><h2>🐋 Whale Feed</h2></div>
    <div class="card"><div style="padding:16px">
      <p class="muted" style="margin-top:0">Large transfers across all 8 stablecoins — individual primary-chain transfers ≥ max($1M, 0.01% of supply) per coin, newest first. Each coin contributes at most its latest 10, found by paging the recent-transfers feed, so the window is whatever the feed covers (scanned count + window per coin, below). A transfer with the zero address as counterparty is labelled <b>Mint</b> / <b>Burn</b>; a large mint is itself a whale event. API: <a href="#/api-docs">GET /api/overview/whale-feed</a> · per coin: <span class="mono">GET /api/&lt;scope&gt;/whale-feed</span>. See also the <a href="#/">whales &amp; exchanges rollup</a>.</p>
      <form id="wf-form" autocomplete="off">
        <div style="display:flex;flex-wrap:wrap;gap:12px;margin-bottom:10px" id="wf-coins">
          ${SUPPLY_FEED_COINS.map((c) => `<label class="muted" style="font-size:13px"><input type="checkbox" value="${esc(c)}"${state.coins.includes(c) ? " checked" : ""}> ${esc(c)}</label>`).join("")}
        </div>
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px">
          <label class="field">Min USD value<input type="number" id="wf-min" min="0" step="100000" placeholder="e.g. 5000000" value="${esc(state.min_usd)}"></label>
          <label class="field">Last N days<select id="wf-days">${["1", "7", "30"].map((n) => `<option value="${n}"${n === state.days ? " selected" : ""}>${n} day${n === "1" ? "" : "s"}</option>`).join("")}</select></label>
          <label class="field">From<input type="date" id="wf-from" value="${esc(state.from)}"></label>
          <label class="field">To<input type="date" id="wf-to" value="${esc(state.to)}"></label>
          <label class="field">Page size<select id="wf-size"><option value="10">10</option><option value="25">25</option><option value="50" selected>50</option><option value="100">100</option></select></label>
        </div>
        <div style="margin-top:12px;display:flex;gap:8px;align-items:center">
          <button class="btn" type="submit">Apply filters</button>
          <span class="muted" style="font-size:12px">From / To override “Last N days”. Min USD cannot go below a coin's own threshold (the scan never saw smaller transfers). Try: <a href="#/whale-feed" data-wf-try="min_usd:5000000">≥ $5M only</a> · <a href="#/whale-feed" data-wf-try="coin:USDT">USDT (high threshold)</a> · <a href="#/whale-feed" data-wf-try="coin:USDe">USDe (at the 10-event cap)</a></span>
        </div>
      </form>
    </div></div>
    <div id="wf-results"><div class="card"><div style="padding:16px"><div class="spinner">Loading large transfers… (a cold load runs 8 coins' whales evaluations and can take ~40s)</div></div></div></div>
    </div>`);
  const load = async () => {
    if (!alive()) return;
    $("#wf-results").innerHTML = `<div class="card"><div style="padding:16px"><div class="spinner">Loading large transfers… (a cold load runs 8 coins' whales evaluations and can take ~40s)</div></div></div>`;
    let d;
    try {
      const qs = new URLSearchParams({ page: String(state.page), page_size: String(state.page_size) });
      if (state.coins.length && state.coins.length < SUPPLY_FEED_COINS.length) qs.set("coin", state.coins.join(","));
      if (state.min_usd) qs.set("min_usd", String(state.min_usd));
      if (state.from) qs.set("from", String(Math.floor(new Date(state.from + "T00:00:00Z").getTime() / 1000)));
      else if (state.days) qs.set("days", state.days);
      if (state.to) qs.set("to", String(Math.floor(new Date(state.to + "T23:59:59Z").getTime() / 1000)));
      d = await fetchJson(API + "/whale-feed?" + qs.toString(), null, 180000);
    } catch (err) {
      if (!alive()) return;
      $("#wf-results").innerHTML = `<div class="card"><div class="empty">Whale feed failed to load: ${esc(err.message)}</div></div>`;
      return;
    }
    if (!alive()) return;
    $("#wf-results").innerHTML = whaleFeedResultsHtml(d);
    const prev = $("#wf-prev"), next = $("#wf-next");
    if (prev) prev.addEventListener("click", () => { if (state.page > 1) { state.page--; load(); } });
    if (next) next.addEventListener("click", () => { if (state.page < d.total_pages) { state.page++; load(); } });
  };
  $("#wf-form").addEventListener("submit", (e) => {
    e.preventDefault();
    state.coins = [...document.querySelectorAll("#wf-coins input:checked")].map((c) => c.value);
    if (!state.coins.length) state.coins = [...SUPPLY_FEED_COINS];
    state.min_usd = $("#wf-min").value;
    state.days = $("#wf-days").value;
    state.from = $("#wf-from").value;
    state.to = $("#wf-to").value;
    state.page_size = Number($("#wf-size").value) || 50;
    state.page = 1;
    load();
  });
  document.querySelectorAll("[data-wf-try]").forEach((a) => a.addEventListener("click", (e) => {
    e.preventDefault();
    const v = a.dataset.wfTry;
    if (v.startsWith("min_usd:")) { state.min_usd = v.slice(8); $("#wf-min").value = state.min_usd; }
    else if (v.startsWith("coin:")) {
      state.coins = [v.slice(5)];
      document.querySelectorAll("#wf-coins input").forEach((c) => { c.checked = state.coins.includes(c.value); });
    }
    state.page = 1; load();
  }));
  load();
}
function renderSupplyMap(sm) {
  const el = $("#supply-map-body");
  if (!el) return;
  if (!sm || !Array.isArray(sm.chains) || !sm.chains.length) {
    el.innerHTML = sectionError("Cross-Chain Supply Map", "Supply-map rollup unavailable right now.");
    return;
  }
  const top = sm.chains.slice(0, 10);
  const rest = sm.chains.slice(10);
  const restTotal = rest.reduce((a, c) => a + (c.total_supply_usd || 0), 0);
  el.innerHTML = `<div style="overflow-x:auto"><table class="tbl"><thead><tr><th>Chain</th><th class="r">Stablecoin supply</th><th class="r">Share of 8-coin total</th><th>Top coins on chain</th><th class="r">Coins</th></tr></thead><tbody>
    ${top.map((c) => `<tr>
      <td><strong>${esc(c.chain)}</strong></td>
      <td class="r">${usdBig(c.total_supply_usd)}</td>
      <td class="r">${c.share_pct != null ? c.share_pct.toFixed(1) + "%" : "—"}</td>
      <td class="muted">${(c.coins || []).map((x) => `${esc(x.symbol)} ${usdBig(x.supply_usd)}`).join(" · ") || "—"}</td>
      <td class="r">${num(c.coin_count)}</td>
    </tr>`).join("")}
    ${rest.length ? `<tr><td class="muted">…and ${num(rest.length)} more chains</td><td class="r muted">${usdBig(restTotal)}</td><td class="r muted">—</td><td class="muted">—</td><td class="r muted">—</td></tr>` : ""}
  </tbody></table></div>
  <div class="total-row"><span>Total stablecoin supply — mapped to chains (${num(sm.coins_included)} of 8 coins)</span><span>${usdBig(sm.total_covered_usd)}</span></div>
  <div class="total-row"><span>Unattributed supply (not assigned to any chain)</span><span>${usdBig(sm.total_unknown_usd)}</span></div>
  ${(sm.bridges_summary || []).length ? `<div style="padding:12px 16px" class="muted">Bridged-in stock by bridge: ${(sm.bridges_summary || []).map((b) => `${esc(b.bridge_label || b.bridge)} ${usdBig(b.total_usd)}`).join(" · ")} — current stock of supply that arrived via a bridge, not a flow.</div>` : ""}
  <div style="padding:0 16px 12px" class="muted">${esc(sm.note || "")}</div>`;
}
function renderBridges(b) {
  const el = $("#bridges-body");
  if (!el) return;
  if (!b || b.error) {
    el.innerHTML = `<div style="padding:16px"><div class="notice" style="margin:0"><strong>Bridge flows unavailable.</strong> ${esc((b && b.error) || "")}</div></div>`;
    return;
  }
  const hourly = (b.hourly_tx_counts || []).slice(-24);
  const maxC = hourly.reduce((m, h) => Math.max(m, Number(h.count) || 0), 0) || 1;
  el.innerHTML = `
    <div class="gascards" style="margin:0;padding:16px 16px 0">
      <div class="gascard"><div class="k">Volume · 24h</div><div class="v">${usdBig(b.volume_24h_usd)}</div><div class="sub">${num(b.messages_24h)} messages</div></div>
      <div class="gascard"><div class="k">Volume · 7d</div><div class="v">${usdBig(b.volume_7d_usd)}</div><div class="sub">${num(b.messages_7d)} messages</div></div>
      <div class="gascard"><div class="k">Volume · 30d</div><div class="v">${usdBig(b.volume_30d_usd)}</div><div class="sub">Total ${usdBig(b.volume_total_usd)}</div></div>
      <div class="gascard"><div class="k">TVL</div><div class="v">${usdBig(b.tvl_usd)}</div><div class="sub">Wormhole total value locked</div></div>
    </div>
    <div style="padding:16px">
      <div class="muted" style="font-size:12px;margin-bottom:8px">Transactions per hour (last 24h) — ${esc(b.provider || "Wormhole")}</div>
      <div style="display:flex;align-items:flex-end;gap:2px;height:64px">
        ${hourly.map((h) => `<div title="${esc(h.time || "")}: ${num(h.count)} txs" style="flex:1;height:${Math.max(4, Math.round((Number(h.count) || 0) / maxC * 100))}%;background:var(--blue);border-radius:2px 2px 0 0;opacity:.75"></div>`).join("")}
      </div>
      <div class="muted" style="font-size:12px;margin-top:8px">${esc(b.note || "")}</div>
    </div>`;
}
function moverRows(list) {
  if (!list || !list.length) return emptyBox("No movers in this set.");
  return `<table class="tbl"><thead><tr><th>Coin</th><th class="r">Price</th><th class="r">24h</th><th class="r">Market Cap</th></tr></thead><tbody>
    ${list.map((m) => `<tr>
      <td><strong>${esc(m.symbol || "")}</strong> <span class="muted">${esc(m.name || "")}</span></td>
      <td class="r">${usdPrice(m.price_usd)}</td>
      <td class="r"><span style="color:${Number(m.change_24h_pct) >= 0 ? "var(--green)" : "var(--red)"};font-weight:700">${Number(m.change_24h_pct) >= 0 ? "+" : ""}${Number(m.change_24h_pct).toFixed(2)}%</span></td>
      <td class="r">${usdBig(m.market_cap_usd)}</td>
    </tr>`).join("")}
  </tbody></table>`;
}
function renderMovers(m) {
  const g = $("#movers-gainers");
  const l = $("#movers-losers");
  if (!m || m.error) {
    const msg = sectionError("Top Movers", (m && m.error) || "Movers data unavailable.");
    if (g) g.innerHTML = msg;
    if (l) l.innerHTML = "";
    return;
  }
  if (g) g.innerHTML = moverRows(m.gainers);
  if (l) l.innerHTML = moverRows(m.losers);
}

/* ---------------- home ---------------- */
async function refreshHome() {
  let d = null;
  try {
    d = await get("/overview");
  } catch (e) {
    const el = $("#market-stats");
    if (el && !el.children.length) el.innerHTML = `<div class="card" style="grid-column:1/-1">${errBox("Could not load the overview: " + e.message)}</div>`;
    return;
  }
  renderGlobal(d.global);
  renderChains(d.chains);
  renderStablecoins(d.stablecoins);
  renderBridges(d.bridges);
  renderMovers(d.movers);
  let lb = null;
  try { lb = await get("/stablecoin-leaderboard"); } catch (e) { lb = null; }
  renderPegLeaderboard(lb);
  // Mint/burn rollup: fetched separately (its per-coin on-chain scans
  // are slow) with a longer client-side timeout than the 25s default.
  let mb = null;
  try { mb = await fetchJson(API + "/mint-burn", null, 90000); } catch (e) { mb = null; }
  renderMintBurn(mb);
  // Whales rollup: fetched separately (per-coin transfer scans are
  // slow) with a longer client-side timeout.
  let wh = null;
  try { wh = await fetchJson(API + "/whales", null, 120000); } catch (e) { wh = null; }
  renderWhales(wh);
  // Activity rollup: fetched separately (per-coin transfer scans are
  // slow) with a longer client-side timeout.
  let ac = null;
  try { ac = await fetchJson(API + "/activity", null, 120000); } catch (e) { ac = null; }
  renderActivity(ac);
  // Payments-readiness rollup: fetched separately (per-coin activity
  // scans are slow) with a longer client-side timeout.
  let pr = null;
  try { pr = await fetchJson(API + "/payments-readiness", null, 120000); } catch (e) { pr = null; }
  renderPaymentsReadiness(pr);
  // DeFi rollup: fetched separately (the yields snapshot is a ~11MB
  // download, cached server-side and shared across coins).
  let df = null;
  try { df = await fetchJson(API + "/defi", null, 120000); } catch (e) { df = null; }
  renderDefi(df);
  // Freeze & blacklist rollup: fetched separately (per-coin log
  // scans + Multicall3 balance batches are slow) with a longer
  // client-side timeout.
  let fz = null;
  try { fz = await fetchJson(API + "/freeze", null, 180000); } catch (e) { fz = null; }
  renderFreeze(fz);
  // Issuer risk rollup: fetched separately (per-coin freeze log
  // scans + activity scans are slow) with a longer client-side
  // timeout.
  let ir = null;
  try { ir = await fetchJson(API + "/issuer-risk", null, 180000); } catch (e) { ir = null; }
  renderIssuerRisk(ir);
  // Attestation monitor (phase 3, item 7): fetched separately
  // (8 per-coin reserves evaluations; cached 300s server-side).
  let at = null;
  try { at = await fetchJson(API + "/attestations", null, 60000); } catch (e) { at = null; }
  renderAttestations(at);
  // Supply-map rollup: fetched separately (per-coin maps parse
  // DeFiLlama's large per-chain history payloads).
  let sm = null;
  try { sm = await fetchJson(API + "/supply-map", null, 90000); } catch (e) { sm = null; }
  renderSupplyMap(sm);
}
async function homePage() {
  document.title = TITLE + " — Cross-Chain Crypto Analytics";
  render(`
    <div class="hero">
      <h1>Cryptoscope</h1>
      <p>Cross-chain crypto analytics — one search across every chain, every Scope.</p>
      <form id="hero-search-form" class="search" autocomplete="off">
        <input id="hero-search-input" type="text" placeholder="Search address / transaction / block / slot / stablecoin (e.g. vitalik.eth, USDC)" aria-label="Search">
        <button type="submit" title="Search">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/></svg>
        </button>
      </form>
      <div id="search-results" style="margin-top:14px"></div>
    </div>
    <div class="statgrid" id="market-stats">${spinner()}</div>
    <div class="card">
      <div class="card-head">Chains <span class="spacer"></span><span class="muted" style="font-weight:400;font-size:12px">Live latest block / slot per chain</span></div>
      <div id="chains-body">${spinner()}</div>
    </div>
    <div class="card">
      <div class="card-head">Stablecoin Pegs <span class="spacer"></span><span class="muted" style="font-weight:400;font-size:12px">All 8 stablecoin scopes</span></div>
      <div id="stablecoins-body">${spinner()}</div>
    </div>
    <div class="card">
      <div class="card-head">Stablecoin Peg Stability <span class="spacer"></span><span class="muted" style="font-weight:400;font-size:12px">Leaderboard · all 8 stablecoins · daily closes</span></div>
      <div id="peg-leaderboard-body">${spinner()}</div>
    </div>
    <div class="card">
      <div class="card-head">Stablecoin Mint / Burn — Net Issuance <span class="spacer"></span><span class="muted" style="font-weight:400;font-size:12px">All 8 stablecoin scopes · DeFiLlama daily supply</span></div>
      <div id="mint-burn-body">${spinner()}</div>
    </div>
    <div class="card">
      <div class="card-head">Stablecoin Whales &amp; Exchanges <span class="spacer"></span><span class="muted" style="font-weight:400;font-size:12px">All 8 stablecoin scopes · top holders on each primary chain</span></div>
      <div id="whales-body">${spinner()}</div>
    </div>
    <div class="card">
      <div class="card-head">Stablecoin Activity &amp; Velocity <span class="spacer"></span><span class="muted" style="font-weight:400;font-size:12px">All 8 stablecoin scopes · primary chain only · incomplete 24h windows are lower bounds (≥)</span></div>
      <div id="activity-body">${spinner()}</div>
    </div>
    <div class="card">
      <div class="card-head">Stablecoin Payments Readiness <span class="spacer"></span><span class="muted" style="font-weight:400;font-size:12px">All 8 stablecoin scopes · 0–100, higher = more ready for payments · fees are primary-chain estimates</span></div>
      <div id="payments-readiness-body">${spinner()}</div>
    </div>
    <div class="card">
      <div class="card-head">Stablecoin DeFi Deployment &amp; Yield <span class="spacer"></span><span class="muted" style="font-weight:400;font-size:12px">All 8 stablecoin scopes · best lending APY is shown with its range, never alone · LP TVL counts whole pools</span></div>
      <div id="defi-body">${spinner()}</div>
    </div>
    <div class="card">
      <div class="card-head">Stablecoin Freeze &amp; Blacklist <span class="spacer"></span><span class="muted" style="font-weight:400;font-size:12px">All 8 stablecoin scopes · frozen supply is a lower-bound estimate, never the total</span></div>
      <div id="freeze-body">${spinner()}</div>
    </div>
    <div class="card">
      <div class="card-head">Stablecoin Issuer Risk <span class="spacer"></span><span class="muted" style="font-weight:400;font-size:12px">All 8 stablecoin scopes · 0–100, higher = riskier · rank 1 = least risky issuer</span></div>
      <div id="issuer-risk-body">${spinner()}</div>
    </div>
    <div class="card">
      <div class="card-head">Stablecoin Attestations <span class="spacer"></span><span class="muted" style="font-weight:400;font-size:12px">All 8 stablecoin scopes · overdue first · current-status-only, no history</span></div>
      <div id="attestations-body">${spinner()}</div>
    </div>
    <div class="card">
      <div class="card-head">Cross-Chain Supply Map — Stablecoins <span class="spacer"></span><span class="muted" style="font-weight:400;font-size:12px">All 8 stablecoin scopes · DeFiLlama chain balances</span></div>
      <div id="supply-map-body">${spinner()}</div>
    </div>
    <div class="card">
      <div class="card-head">Bridge Flows — Wormhole <span class="spacer"></span><span class="muted" style="font-weight:400;font-size:12px">via Wormholescan</span></div>
      <div id="bridges-body">${spinner()}</div>
    </div>
    <div class="cols2">
      <div class="card">
        <div class="card-head">Top Gainers <span class="spacer"></span><span class="muted" style="font-weight:400;font-size:12px">24h · top 50 by market cap</span></div>
        <div id="movers-gainers">${spinner()}</div>
      </div>
      <div class="card">
        <div class="card-head">Top Losers <span class="spacer"></span><span class="muted" style="font-weight:400;font-size:12px">24h · top 50 by market cap</span></div>
        <div id="movers-losers">${spinner()}</div>
      </div>
    </div>
    <div class="card">
      <div class="card-head">All Scopes</div>
      <div id="scopes-grid" style="padding:16px;display:flex;flex-wrap:wrap;gap:8px"></div>
    </div>`);
  wireSearch($("#hero-search-form"), $("#hero-search-input"));
  const grid = $("#scopes-grid");
  if (grid) {
    grid.innerHTML = SIBLINGS.map((s) => `<a class="btn" href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.name)} ↗</a>`).join("")
      + ` <a class="btn primary" href="${WALLET_URL}/#/wallet" target="_blank" rel="noopener">Wallet Dashboard ↗</a>`;
  }
  await refreshHome();
  pollTimer = setInterval(refreshHome, 60000);
}

/* ---------------- issuer-risk detail page ---------------- */
/* Dedicated demo page for the issuer-risk API (phase 3, item 6):
   GET /api/overview/issuer-risk/detail — the 8-issuer ranking
   (the /issuer-risk rollup reused wholesale) plus the full
   per-issuer payload for every coin, with all six input
   sub-scores and their evidence. Ranking table first, then one
   issuer at a time in full: pick an issuer (default: rank 1)
   and its six input breakdowns render below. Weights, direction,
   bands, methodology and honest limits are stated on the page,
   as they are in every response. */
let IR_DETAIL = null;
let IR_SELECTED = null;
const IR_INPUT_LABELS = [
  ["depeg_risk", "Depeg risk"],
  ["reserve_transparency", "Reserve transparency"],
  ["issuance_direction", "Issuance direction"],
  ["concentration", "Concentration"],
  ["freeze_exposure", "Freeze exposure"],
  ["adoption", "Adoption"],
];
function irBandBadge(b) {
  return b === "low" ? '<span class="badge ok">Low</span>'
    : b === "moderate" ? '<span class="badge grey">Moderate</span>'
    : b === "elevated" ? '<span class="badge warn">Elevated</span>'
    : b === "high" ? '<span class="badge fail">High</span>'
    : '<span class="badge grey">Unknown</span>';
}
/* Key measured values + the evidence note for one input, adapted
   from the per-coin issuer-risk card in the stablecoin Scope apps
   (same fields, same wording) — never a bare sub-score. */
function irInputEvidence(key, v) {
  if (!v) return '<span class="muted">unknown — could not be measured; weight redistributed to the other inputs</span>';
  const ev = v.evidence || {};
  const fmtFee = (x) => (x == null ? "—" : "$" + Number(x).toFixed(4));
  const fmtFin = (s) => {
    if (s == null) return "—";
    const x = Number(s);
    if (x < 60) return "~" + x + "s";
    if (x < 3600) return "~" + Math.round(x / 60) + " min";
    return "~" + (x / 3600).toFixed(1) + "h";
  };
  const pct2 = (p) => (p != null ? (Number(p) >= 0 ? "+" : "") + Number(p).toFixed(2) + "%" : "—");
  let head = "";
  if (key === "depeg_risk") head = `Feature-1 score ${ev.depeg_risk_score != null ? Number(ev.depeg_risk_score).toFixed(1) : "—"} / 100 · price ${usdPrice(ev.price_usd)} (${bpsFmt(ev.current_deviation_bps)}) · ${num(ev.depeg_days_365d)} depeg day(s) in 365d · worst ${bpsFmt(ev.worst_depeg_365d_bps)}`;
  else if (key === "reserve_transparency") head = `${esc(ev.status_label || ev.status || "—")} · ${esc(ev.auditor || "no auditor")}${ev.auditor_tier ? ` (${esc(ev.auditor_tier)})` : ""} · last attestation ${esc(ev.last_attestation || "unverified")}${ev.days_since_attestation != null ? ` (${num(ev.days_since_attestation)}d ago)` : ""}${ev.reserve_to_outstanding_ratio != null ? ` · dated reserves/outstanding ${Number(ev.reserve_to_outstanding_ratio).toFixed(4)}x (${esc(ev.reserve_figures_as_of || "")})` : " · no dated reserve figures verified"} — TRANSPARENCY, not a live reserve measurement`;
  else if (key === "issuance_direction") head = ev.flat_supply ? "<b>Supply frozen</b> — no net mint/burn in 90d" : `Net 30d ${ev.net_30d_usd != null ? (Number(ev.net_30d_usd) >= 0 ? "+" : "−") + usdBig(Math.abs(ev.net_30d_usd)) : "—"} (${pct2(ev.net_30d_pct_of_supply)}) · net 90d ${ev.net_90d_usd != null ? (Number(ev.net_90d_usd) >= 0 ? "+" : "−") + usdBig(Math.abs(ev.net_90d_usd)) : "—"} (${pct2(ev.net_90d_pct_of_supply)}) · demand: ${esc(ev.demand_direction || "—")}${ev.trend_label ? ` · ${esc(ev.trend_label)}` : ""}`;
  else if (key === "concentration") head = `Top 10 holders ${ev.top_10_share_pct != null ? Number(ev.top_10_share_pct).toFixed(2) + "%" : "—"} of ${esc(ev.primary_chain === "tron" ? "Tron" : "Ethereum")} supply (top 1: ${ev.top_1_share_pct != null ? Number(ev.top_1_share_pct).toFixed(2) + "%" : "—"} · top 50: ${ev.top_50_share_pct != null ? Number(ev.top_50_share_pct).toFixed(2) + "%" : "—"}) · exchange-identified ${ev.exchange_share_pct != null ? Number(ev.exchange_share_pct).toFixed(2) + "%" : "—"} (LOWER BOUND, ${num(ev.exchange_wallets_identified)} wallet(s) in top 50)`;
  else if (key === "freeze_exposure") head = `${ev.can_freeze ? "<b>Issuer can freeze balances</b>" : "<b>No freeze function</b>"}${(ev.probe_functions_found || []).length ? ` (${esc(ev.probe_functions_found.join(", "))})` : ""} · ${num(ev.events_total)} freeze event(s) in ${num(ev.events_window_days)}d window · frozen ${usdBig(ev.frozen_supply_usd)}${ev.frozen_share_of_primary_supply_pct != null ? ` (${Number(ev.frozen_share_of_primary_supply_pct).toFixed(4)}% of primary supply — LOWER-BOUND ESTIMATE)` : " (no frozen supply measured)"} · black funds destroyed ${usdBig(ev.destroyed_total_usd)} · OFAC SDN matches ${num(ev.sanctions_matches_total)}`;
  else if (key === "adoption") head = `Chain coverage ${ev.chain_coverage_score != null ? Number(ev.chain_coverage_score).toFixed(1) : "—"} / 100 (${num(ev.material_chains)} material chain(s)) · fees/finality ${ev.fees_finality_score != null ? Number(ev.fees_finality_score).toFixed(1) : "—"} / 100 (typical transfer ${fmtFee(ev.typical_transfer_fee_usd)} · finality ${fmtFin(ev.finality_seconds)}) — feature-8 legs only, peg/liquidity/freeze excluded to avoid double-counting`;
  return `${head}${ev.note ? `<br><span class="muted" style="font-size:12px">${esc(ev.note)}</span>` : ""}`;
}
function irBreakdownHtml(iss) {
  const inp = iss.inputs || {};
  const rows = IR_INPUT_LABELS.map(([key, label]) => {
    const v = inp[key];
    return `<tr><td>${esc(label)}</td><td style="text-align:right">${v ? Math.round(v.weight * 100) + "%" : "—"}</td><td style="text-align:right">${v && v.score != null ? `<b>${Number(v.score).toFixed(1)}</b> / 100` : "—"}</td><td>${irInputEvidence(key, v)}</td></tr>`;
  }).join("");
  return `<div style="padding:12px 16px 4px">
      <div style="font-size:20px"><b>${esc(iss.symbol || "")}</b> — ${esc(iss.name || "")} <span class="muted">(${esc(iss.issuer || "")})</span> · Score: ${iss.score != null ? `<b>${Number(iss.score).toFixed(1)}</b> / 100` : "—"} ${irBandBadge(iss.band)}</div>
      <div class="muted">Price ${usdPrice(iss.price_usd)} (${bpsFmt(iss.deviation_bps)}) · supply ${usdBig(iss.supply_usd)}${iss.inputs_unknown && iss.inputs_unknown.length ? ` · unknown inputs (weight redistributed): ${esc(iss.inputs_unknown.join(", "))}` : ""}</div>
      ${iss.inputs_unknown && iss.inputs_unknown.length ? `<div class="muted" style="margin-top:4px">Weights actually used (unknown inputs excluded, rest renormalised): ${Object.entries(iss.weights_used || {}).map(([k, w]) => `${esc(k)} ${Math.round(Number(w) * 1000) / 10}%`).join(" · ")}</div>` : ""}
    </div>
    <div style="overflow-x:auto"><table class="tbl"><thead><tr><th>Input</th><th style="text-align:right">Weight</th><th style="text-align:right">Sub-score</th><th>Evidence</th></tr></thead><tbody>
    ${rows}
    </tbody></table></div>
    <div class="notice" style="margin:12px 16px">${esc(iss.methodology || "")}</div>`;
}
function renderIrPicker() {
  const el = $("#ir-detail-picker");
  if (!el || !IR_DETAIL) return;
  el.innerHTML = (IR_DETAIL.issuers || []).map((iss, i) => {
    const rank = (IR_DETAIL.ranking || []).find((r) => r.scope === iss.scope);
    return `<button class="btn${IR_SELECTED === iss.scope ? " primary" : ""}" data-ir-scope="${esc(iss.scope)}" style="margin:0 6px 8px 0">#${rank && rank.rank != null ? rank.rank : i + 1} ${esc(iss.symbol || "")} · ${iss.score != null ? Number(iss.score).toFixed(1) : "—"} (${esc(iss.band || "—")})</button>`;
  }).join("") + ((IR_DETAIL.issuers_missing || []).length ? `<span class="muted" style="font-size:12px">Full breakdown unavailable for: ${esc(IR_DETAIL.issuers_missing.join(", "))} — the ranking above still shows their scores where measured.</span>` : "");
  el.querySelectorAll("[data-ir-scope]").forEach((b) => {
    b.addEventListener("click", () => { IR_SELECTED = b.getAttribute("data-ir-scope"); renderIrPicker(); renderIrBreakdown(); });
  });
}
function renderIrBreakdown() {
  const el = $("#ir-detail-breakdown");
  if (!el) return;
  if (!IR_DETAIL) { el.innerHTML = spinner(); return; }
  const iss = (IR_DETAIL.issuers || []).find((x) => x.scope === IR_SELECTED) || (IR_DETAIL.issuers || [])[0];
  el.innerHTML = iss ? irBreakdownHtml(iss) : emptyBox("No full per-issuer breakdown available right now.");
}
async function issuerRiskDetailPage() {
  document.title = "Issuer risk — full breakdowns — " + TITLE;
  IR_DETAIL = null;
  IR_SELECTED = null;
  render(`<div class="pagehead"><h2>Issuer risk — all 8 issuers, full breakdowns</h2></div>
    <div class="card"><div class="card-head">Ranking <span class="muted">(0–100, higher = riskier · rank 1 = least risky issuer)</span></div><div id="ir-detail-ranking">${spinner()}</div></div>
    <div class="card"><div class="card-head">Per-issuer breakdown <span class="muted">(all six inputs with evidence — never a bare score)</span></div><div id="ir-detail-picker" style="padding:12px 16px 0"></div><div id="ir-detail-breakdown">${spinner()}</div></div>
    <div class="card"><div style="padding:14px 16px" id="ir-detail-method">${spinner()}</div></div>`);
  let d = null;
  try {
    d = await fetchJson(API + "/issuer-risk/detail", null, 180000);
  } catch (e) {
    render(`<div class="pagehead"><h2>Issuer risk — all 8 issuers, full breakdowns</h2></div><div class="card">${errBox("Could not load issuer-risk detail: " + e.message)}</div>`);
    return;
  }
  IR_DETAIL = d;
  const rk = $("#ir-detail-ranking");
  if (rk) rk.innerHTML = issuerRiskTableHtml({ rows: d.ranking || [], coins_scored: d.coins_scored, coins_included: d.coins_included });
  const m = $("#ir-detail-method");
  if (m) m.innerHTML = `<b>Weights:</b> ${Object.entries(d.weights || {}).map(([k, w]) => `${esc(k)} ${Math.round(Number(w) * 100)}%`).join(" · ")} · <b>Direction:</b> ${esc(d.direction || "higher = riskier")} · <b>Bands:</b> ${Object.entries(d.bands || {}).map(([b, r]) => `${esc(b)}: score ${esc(r)}`).join(" · ")}<br><br>${esc(d.methodology || "")}${(d.limits || []).length ? `<br><br><b>Honest limits:</b><ul>${d.limits.map((l) => `<li>${esc(l)}</li>`).join("")}</ul>` : ""}<br><span class="muted">API: <a href="#/api-docs">GET /api/overview/issuer-risk/detail</a> · per-coin score: GET /api/&lt;scope&gt;/issuer-risk · ranking only: GET /api/overview/issuer-risk</span>`;
  const issuers = d.issuers || [];
  IR_SELECTED = issuers.length ? issuers[0].scope : null;
  renderIrPicker();
  renderIrBreakdown();
}

/* ---------------- API docs ---------------- */
async function apiDocsPage() {
  document.title = "API Docs — " + TITLE;
  render(`<div class="pagehead"><h2>API Documentation</h2></div><div class="card">${spinner()}</div>`);
  // One row per endpoint. Paid-API entries (phase 3) may also carry
  // example_response, free_tier and paid_tier — shown when present;
  // entries without them render exactly as before.
  const docRow = (ep) => `<tr>
    <td><span class="method get">${esc(ep.method || "GET")}</span></td>
    <td class="mono" style="word-break:break-all">${esc(ep.path || "")}${ep.example ? `<br><span class="muted" style="font-size:12px">e.g. <span class="mono">${esc(ep.example)}</span></span>` : ""}</td>
    <td>${esc(ep.description || "—")}
      ${ep.example_response != null ? `<div style="margin-top:6px"><b>Example response:</b><pre class="mono" style="white-space:pre-wrap;word-break:break-word;margin:4px 0 0;font-size:12px">${esc(typeof ep.example_response === "string" ? ep.example_response : JSON.stringify(ep.example_response, null, 2))}</pre></div>` : ""}
      ${ep.free_tier ? `<div style="margin-top:6px"><b>Free tier today:</b> ${esc(ep.free_tier)}</div>` : ""}
      ${ep.paid_tier ? `<div style="margin-top:6px"><b>Paid tier (planned):</b> ${esc(ep.paid_tier)}</div>` : ""}
    </td>
    <td>${Array.isArray(ep.params) && ep.params.length ? ep.params.map((p) => `<code>${esc(typeof p === "string" ? p : (p.name || JSON.stringify(p)))}</code>`).join(" ") : "—"}</td>
  </tr>`;
  const docTable = (list) => `<table class="tbl"><thead><tr><th>Method</th><th>Path</th><th>Description</th><th>Params</th></tr></thead><tbody>
    ${list.map(docRow).join("")}
  </tbody></table>`;
  try {
    const d = await get("/docs");
    const eps = (d && d.endpoints) || [];
    const chains = (d && d.chains) || [];
    // Paid-API grouping (phase 3): paid entries get their own section
    // at the top so they are findable; absent when none exist.
    const paid = eps.filter((ep) => ep.paid_api);
    const rest = eps.filter((ep) => !ep.paid_api);
    render(`
      <div class="pagehead"><h2>API Documentation</h2></div>
      <div class="card"><div style="padding:14px 16px">Cryptoscope's overview API aggregates ${esc(chains.join(", "))}. All responses are JSON; errors are <span class="mono">{"error": "..."}</span> with a non-2xx status. No API keys required. Each section of <span class="mono">/overview</span> degrades independently — a failed upstream blanks its section, never the whole page.</div></div>
      ${paid.length ? `<div class="card"><div class="card-head">Paid API <span class="badge info">keyed &amp; metered in the paid tier — free &amp; keyless today</span></div><div style="padding:14px 16px">These endpoints will require an API key and be metered per call in the paid tier. They are free and keyless today; the free-tier limits and planned metering are listed on each entry.</div>${docTable(paid)}</div>` : ""}
      <div class="card">
        ${rest.length ? docTable(rest) : emptyBox("No endpoints listed by the server.")}
      </div>`);
  } catch (e) {
    render(`<div class="pagehead"><h2>API Documentation</h2></div>
      <div class="card">${errBox("Could not load API docs: " + e.message)}</div>`);
  }
}

/* ---------------- search ---------------- */
async function doSearch(q) {
  q = String(q || "").trim();
  if (!q) return;
  const box = $("#search-results");
  try {
    const d = await get("/search?q=" + encodeURIComponent(q));
    if (!d) { banner("No results for “" + q + "”"); return; }
    if (d.type === "ambiguous" && Array.isArray(d.candidates)) {
      if (box) {
        box.innerHTML = `<div class="card" style="background:rgba(255,255,255,.95);color:var(--ink)"><div class="card-head">“${esc(q)}” could be more than one thing</div>
          <div style="padding:14px 16px;display:flex;flex-wrap:wrap;gap:8px">
            ${d.candidates.map((c) => `<a class="btn" href="${esc(c.url)}">${esc(c.label)} →</a>`).join("")}
          </div></div>`;
      } else {
        banner("“" + q + "” is ambiguous: " + d.candidates.map((c) => c.label).join(", "));
      }
      return;
    }
    if (d.url) { window.location.href = d.url; return; }
    banner("No results for “" + q + "”");
  } catch (e) {
    banner(e.message);
  }
}
function wireSearch(form, input) {
  if (!form || !input) return;
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    doSearch(input.value);
  });
}

/* ---------------- router ---------------- */
const routes = [
  [/^#\/$/, () => homePage()],
  [/^#\/api-docs$/, () => apiDocsPage()],
  [/^#\/issuer-risk$/, () => issuerRiskDetailPage()],
  [/^#\/attestations$/, () => attestationMonitorPage()],
  [/^#\/supply-feed(\?.*)?$/, () => supplyFeedPage()],
  [/^#\/whale-feed(\?.*)?$/, () => whaleFeedPage()],
  [/^#\/wallet(?:\/([^/]+))?$/, (m) => {
    // Wallet lives in the separate WalletScope app — redirect out.
    window.location.href = WALLET_URL + "/#/wallet" + (m[1] ? "/" + m[1] : "");
  }],
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

document.addEventListener("DOMContentLoaded", async () => {
  // Per-app branding + navigation from /config.js.
  document.title = TITLE + " — Cross-Chain Crypto Analytics";
  const badge = document.getElementById("brand-badge");
  if (badge) badge.textContent = CFG.brandLetter || "C";
  const bname = document.getElementById("brand-name");
  if (bname) { const _bm = TITLE.match(/^(.*)(scope)$/i); bname.innerHTML = _bm ? esc(_bm[1]) + '<span class="brand-accent">' + esc(_bm[2]) + '</span>' : esc(TITLE) + '<span class="brand-accent">Scope</span>'; }
  const fbrand = document.getElementById("footer-brand");
  if (fbrand) fbrand.textContent = TITLE;
  const fav = document.querySelector('link[rel="icon"]');
  if (fav && CFG.brandLetter) {
    fav.href = "data:image/svg+xml," + encodeURIComponent("<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect width='32' height='32' rx='7' fill='%230784c3'/><text x='16' y='22' font-size='17' text-anchor='middle' fill='white' font-family='Arial' font-weight='bold'>" + CFG.brandLetter + "</text></svg>");
  }
  // Scope switcher: jump between sibling Scope apps.
  const scopeSel = document.getElementById("scope-select");
  if (scopeSel) {
    scopeSel.innerHTML = `<option value="">${esc(TITLE)}</option>` + SIBLINGS.map((s) => `<option value="${esc(s.url)}">${esc(s.name)}</option>`).join("");
    scopeSel.onchange = () => { if (scopeSel.value) window.location.href = scopeSel.value; };
  }
  // Wallet links point at WalletScope.
  const nw = document.getElementById("nav-wallet");
  if (nw && WALLET_URL) nw.href = WALLET_URL + "/#/wallet";
  const fw = document.getElementById("footer-wallet");
  if (fw && WALLET_URL) fw.href = WALLET_URL + "/#/wallet";
  // Footer: every Scope app.
  const fs = document.getElementById("footer-scopes");
  if (fs) {
    fs.innerHTML = "Scopes: " + SIBLINGS.map((s) => `<a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.name)}</a>`).join(" · ");
  }
  // Net pill.
  const np = document.getElementById("netpill-text");
  if (np) np.textContent = CFG.netpill || "Cross-chain";
  wireSearch($("#search-form"), $("#search-input"));
  window.addEventListener("hashchange", route);
  route();
});
