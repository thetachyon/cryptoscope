"use strict";
/* Cryptoscope overview frontend — cross-chain analytics landing page.
   No build step, no CDN. Per-app behaviour comes from /config.js
   (window.SCOPE_CONFIG). API: /api/overview/* — the aggregate endpoint
   returns every section; each section degrades independently server-side.
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
      <td class="r">${r.scope_url ? scopeLink(r.scope_url + "/#/", "Open →") : "—"}</td>
    </tr>`).join("")}
  </tbody></table>
  <div class="total-row"><span>Total stablecoin supply (8 scopes)</span><span>${usdBig(s.total_supply_usd)}</span></div>`;
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

/* ---------------- API docs ---------------- */
async function apiDocsPage() {
  document.title = "API Docs — " + TITLE;
  render(`<div class="pagehead"><h2>API Documentation</h2></div><div class="card">${spinner()}</div>`);
  try {
    const d = await get("/docs");
    const eps = (d && d.endpoints) || [];
    const chains = (d && d.chains) || [];
    render(`
      <div class="pagehead"><h2>API Documentation</h2></div>
      <div class="card"><div style="padding:14px 16px">Cryptoscope's overview API aggregates ${esc(chains.join(", "))}. All responses are JSON; errors are <span class="mono">{"error": "..."}</span> with a non-2xx status. No API keys required. Each section of <span class="mono">/overview</span> degrades independently — a failed upstream blanks its section, never the whole page.</div></div>
      <div class="card">
        ${eps.length ? `<table class="tbl"><thead><tr><th>Method</th><th>Path</th><th>Description</th><th>Params</th></tr></thead><tbody>
          ${eps.map((ep) => `<tr>
            <td><span class="method get">${esc(ep.method || "GET")}</span></td>
            <td class="mono" style="word-break:break-all">${esc(ep.path || "")}${ep.example ? `<br><span class="muted" style="font-size:12px">e.g. <span class="mono">${esc(ep.example)}</span></span>` : ""}</td>
            <td>${esc(ep.description || "—")}</td>
            <td>${Array.isArray(ep.params) && ep.params.length ? ep.params.map((p) => `<code>${esc(typeof p === "string" ? p : (p.name || JSON.stringify(p)))}</code>`).join(" ") : "—"}</td>
          </tr>`).join("")}
        </tbody></table>` : emptyBox("No endpoints listed by the server.")}
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
