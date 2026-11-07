"use strict";
/* BitScope frontend — hash-routed single page app for the Bitcoin explorer.
   No build step, no CDN. Per-app behaviour comes from /config.js
   (window.SCOPE_CONFIG). API: /api/bitscope/* — one chain (bitcoin).
   Same light Etherscan-like theme/classes as the canonical *Scope apps
   (styles.css is shared): .card, .table/.tbl, .badge, .muted, .topbar,
   .wrap, .error-banner, .statgrid, .detail, .copybtn, .loadmore. */
const CFG = window.SCOPE_CONFIG || {};
const SCOPE = CFG.scope || "bitscope";
const TITLE = CFG.title || "BitScope";
const API = CFG.apiPrefix || "/api/bitscope";
const WALLET_URL = CFG.walletUrl || "https://walletscope.fly.dev";
const SIBLINGS = Array.isArray(CFG.siblings) ? CFG.siblings : [];
const SATS_PER_BTC = 1e8;

/* ---------------- helpers ---------------- */
const $ = (s) => document.querySelector(s);
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
async function fetchJson(url, opts, timeoutMs) {
  // 20s client-side guard so a stalled upstream can never hang the UI.
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs || 20000);
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
/* USD price that can be large: no decimals above $1k, 2 below. */
function usdPrice(n) {
  if (n == null || n === "") return "—";
  const x = Number(n);
  if (!Number.isFinite(x)) return "—";
  return "$" + x.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
/* Satoshis -> BTC string (up to 8 dp, trailing zeros trimmed). */
function btcFmt(sats) {
  if (sats == null || sats === "") return "—";
  const x = Number(sats) / SATS_PER_BTC;
  if (!Number.isFinite(x)) return "—";
  return x.toLocaleString("en-US", { minimumFractionDigits: 0, maximumFractionDigits: 8 });
}
/* BTC float -> string. */
function btcFloat(v) {
  if (v == null || v === "") return "—";
  const x = Number(v);
  if (!Number.isFinite(x)) return "—";
  return x.toLocaleString("en-US", { minimumFractionDigits: 0, maximumFractionDigits: 8 });
}
/* Unix seconds -> UTC string. */
function utcUnix(sec) {
  if (sec == null || sec === "") return "—";
  try { return new Date(Number(sec) * 1000).toUTCString().replace("GMT", "UTC"); } catch (e) { return String(sec); }
}
/* Relative age from unix seconds ("5 min ago"). */
function ageFromUnix(sec) {
  if (sec == null || sec === "") return "—";
  const diff = Math.max(0, Math.floor(Date.now() / 1000 - Number(sec)));
  if (diff < 5) return "just now";
  if (diff < 60) return diff + " sec ago";
  if (diff < 3600) { const m = Math.floor(diff / 60); return m + (m === 1 ? " min ago" : " mins ago"); }
  if (diff < 86400) { const h = Math.floor(diff / 3600); return h + (h === 1 ? " hr ago" : " hrs ago"); }
  const d = Math.floor(diff / 86400);
  return d + (d === 1 ? " day ago" : " days ago");
}
function ageOf(obj) {
  if (obj && obj.age) return esc(obj.age);
  return esc(ageFromUnix(obj && (obj.block_time || obj.timestamp)));
}
function shortHash(s) {
  s = String(s || "");
  return s.length > 18 ? s.slice(0, 10) + "…" + s.slice(-8) : s;
}
function copyBtn(text) {
  return `<button class="copybtn" data-copy="${esc(text)}" title="Copy to clipboard">⧉</button>`;
}
function statusBadge(confirmed) {
  if (confirmed === true) return '<span class="badge ok">Confirmed</span>';
  if (confirmed === false) return '<span class="badge warn">Unconfirmed</span>';
  return '<span class="badge grey">—</span>';
}
function pctChange(v) {
  if (v == null || v === "") return "";
  const x = Number(v);
  if (!Number.isFinite(x)) return "";
  const color = x >= 0 ? "var(--green)" : "var(--red)";
  return ` <small style="color:${color}">(${x >= 0 ? "+" : ""}${x.toFixed(2)}%)</small>`;
}
function hashRateFmt(hs) {
  if (hs == null || hs === "") return "—";
  const x = Number(hs);
  if (!Number.isFinite(x) || x <= 0) return "—";
  const units = [["EH/s", 1e18], ["PH/s", 1e15], ["TH/s", 1e12], ["GH/s", 1e9]];
  for (const [u, f] of units) {
    if (x >= f) return (x / f).toFixed(2) + " " + u;
  }
  return num(x) + " H/s";
}

/* ---------------- links ---------------- */
function linkBlock(n) { return `<a href="#/block/${esc(n)}" class="mono">${num(n)}</a>`; }
function linkTx(txid) { return `<a href="#/tx/${esc(txid)}" class="mono">${esc(shortHash(txid))}</a>`; }
function linkAddr(a) {
  if (!a) return "—";
  return `<a href="#/address/${esc(a)}" class="mono">${esc(shortHash(a))}</a>`;
}
function walletHref(addr) { return WALLET_URL + "/#/wallet/" + addr; }

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
function detailRow(k, v) {
  return `<div class="detail"><div class="dk">${esc(k)}</div><div class="dv">${v}</div></div>`;
}

/* ---------------- BTC price cache ---------------- */
/* /stats is the only source of BTC price; pages reuse it so USD values
   do not need a second stats round-trip per page load. */
let btcPriceUsd = null;
let btcPriceTried = false;
async function loadBtcPrice() {
  if (btcPriceTried) return btcPriceUsd;
  btcPriceTried = true;
  try {
    const s = await get("/stats");
    if (s && s.btc_price_usd != null) btcPriceUsd = Number(s.btc_price_usd);
  } catch (e) { /* price stays unknown; USD values render as "—" */ }
  return btcPriceUsd;
}
function usdOfBtc(btc, price) {
  if (btc == null || price == null) return "—";
  const x = Number(btc) * Number(price);
  return Number.isFinite(x) ? usd(x) : "—";
}

/* ---------------- tx table (shared) ---------------- */
function txRow(t) {
  const fee = t.fee != null ? `${num(t.fee)} sat <span class="muted">(${t.fee_rate_sat_vb != null ? t.fee_rate_sat_vb + " sat/vB" : "—"})</span>` : "—";
  return `<tr>
    <td>${linkTx(t.txid)}${t.is_coinbase ? ' <span class="badge info">Coinbase</span>' : ""}</td>
    <td>${t.block_height != null ? linkBlock(t.block_height) : "—"}</td>
    <td>${ageOf(t)}</td>
    <td>${statusBadge(t.confirmed)}</td>
    <td class="r mono">${fee}</td>
  </tr>`;
}
function txTable(txs, emptyMsg) {
  if (!txs || !txs.length) return emptyBox(emptyMsg || "No transactions found.");
  return `<table class="tbl"><thead><tr><th>Transaction</th><th>Block</th><th>Age</th><th>Status</th><th class="r">Fee</th></tr></thead><tbody>
    ${txs.map(txRow).join("")}
  </tbody></table>`;
}

/* ---------------- polling ---------------- */
let pollTimer = null;
function clearPoll() { if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } }

/* ---------------- home ---------------- */
function renderStats(s) {
  const el = $("#stats");
  if (!el) return;
  const price = s.btc_price_usd != null ? usdPrice(s.btc_price_usd) : "—";
  const chg = pctChange(s.btc_change_24h_pct);
  const fees = s.fees || {};
  el.innerHTML = `
    <div class="stat"><div class="k">BTC Price</div><div class="v">${price}${chg}</div><div class="sub">USD · 24h change</div></div>
    <div class="stat"><div class="k">Latest Block</div><div class="v">${s.height != null ? linkBlock(s.height) : "—"}</div><div class="sub">Difficulty ${s.difficulty != null ? Number(s.difficulty).toExponential(2) : "—"}</div></div>
    <div class="stat"><div class="k">Hashrate</div><div class="v">${hashRateFmt(s.hashrate_hs)}</div><div class="sub">Estimated · 3-day average</div></div>
    <div class="stat"><div class="k">Mempool</div><div class="v">${s.mempool_count != null ? num(s.mempool_count) + ' <small>txs</small>' : "—"}</div><div class="sub">${s.mempool_vsize != null ? num(s.mempool_vsize) + " vBytes" : ""} · Fastest fee ${fees.fastest != null ? fees.fastest + " sat/vB" : "—"}</div></div>`;
}
function blocksTable(items) {
  if (!items || !items.length) return emptyBox("No blocks found.");
  return `<table class="tbl"><thead><tr><th>Height</th><th>Age</th><th class="r">Tx Count</th><th class="r">Size</th></tr></thead><tbody>
    ${items.map((it) => `<tr>
      <td>${linkBlock(it.height)}</td>
      <td>${ageOf(it)}</td>
      <td class="r">${num(it.tx_count)}</td>
      <td class="r">${it.size != null ? (Number(it.size) / 1024).toFixed(1) + " kB" : "—"}</td>
    </tr>`).join("")}
  </tbody></table>`;
}
function homeTxTable(txs) {
  if (!txs || !txs.length) return emptyBox("No transactions in the latest block.");
  return txTable(txs.slice(0, 10), "No transactions in the latest block.");
}
async function refreshHome() {
  // Stats (never fatal: a failed stats fetch leaves the previous cards).
  try {
    const s = await get("/stats");
    if (s && s.btc_price_usd != null) btcPriceUsd = Number(s.btc_price_usd);
    renderStats(s);
  } catch (e) {
    const el = $("#stats");
    if (el && !el.children.length) el.innerHTML = `<div class="card" style="grid-column:1/-1">${errBox("Could not load network stats: " + e.message)}</div>`;
  }
  // Latest blocks, then latest transactions from the newest block.
  try {
    const d = await get("/blocks");
    const items = (d && d.items) || [];
    const body = $("#blocks-body");
    if (body) body.innerHTML = blocksTable(items);
    const txBody = $("#txs-body");
    if (txBody) {
      if (!items.length) { txBody.innerHTML = emptyBox("No blocks found."); }
      else {
        try {
          const bt = await get("/block/" + items[0].height + "/txs");
          txBody.innerHTML = homeTxTable((bt && bt.items) || []);
        } catch (e2) {
          txBody.innerHTML = errBox("Could not load latest transactions: " + e2.message);
        }
      }
    }
  } catch (e) {
    const body = $("#blocks-body");
    if (body) body.innerHTML = errBox("Could not load latest blocks: " + e.message);
    const txBody = $("#txs-body");
    if (txBody) txBody.innerHTML = emptyBox("Latest transactions unavailable while blocks fail to load.");
  }
}
async function homePage() {
  document.title = TITLE + " — Bitcoin Block Explorer";
  render(`
    <div class="statgrid" id="stats">${spinner()}</div>
    <div class="cols2">
      <div class="card">
        <div class="card-head">Latest Blocks <span class="spacer"></span><span class="muted" style="font-weight:400;font-size:12px">Bitcoin Mainnet</span></div>
        <div id="blocks-body">${spinner()}</div>
      </div>
      <div class="card">
        <div class="card-head">Latest Transactions <span class="spacer"></span><span class="muted" style="font-weight:400;font-size:12px">from the newest block</span></div>
        <div id="txs-body">${spinner()}</div>
      </div>
    </div>`);
  await refreshHome();
  pollTimer = setInterval(refreshHome, 15000);
}

/* ---------------- block ---------------- */
async function blockPage(n) {
  document.title = "Block " + n + " — " + TITLE;
  render(`<div class="pagehead"><h2>Block</h2></div><div class="card">${spinner()}</div>`);
  try {
    const b = await get("/block/" + encodeURIComponent(n));
    const txs = await (async () => {
      try { return await get("/block/" + encodeURIComponent(n) + "/txs"); }
      catch (e) { return { items: [], _error: e.message }; }
    })();
    const items = (txs.items || []);
    const reward = b.reward_sats != null ? btcFmt(b.reward_sats) + " BTC" : "—";
    render(`
      <div class="pagehead"><h2>Block <span class="mono">${num(b.height)}</span></h2>${copyBtn(String(b.height))}</div>
      <div class="card">
        ${detailRow("Height", `<span class="mono">${num(b.height)}</span>`)}
        ${detailRow("Hash", `<span class="mono" style="word-break:break-all">${esc(b.hash || b.id || "—")}</span> ${b.hash ? copyBtn(b.hash) : ""}`)}
        ${detailRow("Time", b.timestamp != null ? esc(utcUnix(b.timestamp)) + ` <span class="muted">(${ageOf(b)})</span>` : "—")}
        ${detailRow("Miner", b.pool ? `<strong>${esc(b.pool)}</strong>` : "— <span class='muted'>(pool unknown for this block)</span>")}
        ${detailRow("Transactions", num(b.tx_count))}
        ${detailRow("Size", b.size != null ? `${num(b.size)} bytes <span class="muted">(${b.weight != null ? num(b.weight) + " weight units" : ""})</span>` : "—")}
        ${detailRow("Block Reward", reward + (b.total_fees_sats != null ? ` <span class="muted">incl. ${btcFmt(b.total_fees_sats)} BTC fees</span>` : ""))}
        ${detailRow("Difficulty", b.difficulty != null ? Number(b.difficulty).toExponential(4) : "—")}
        ${detailRow("Nonce", num(b.nonce))}
        ${detailRow("Bits", b.bits != null ? `<span class="mono">${esc(String(b.bits))}</span>` : "—")}
        ${detailRow("Version", num(b.version))}
        ${detailRow("Merkle Root", `<span class="mono" style="word-break:break-all">${esc(b.merkle_root || "—")}</span>`)}
        ${detailRow("Previous Block", b.previousblockhash ? `<a href="#/block/${esc(b.previousblockhash)}" class="mono">${esc(shortHash(b.previousblockhash))}</a>` : "—")}
        ${detailRow("Coinbase Addresses", (b.coinbase_addresses || []).length ? b.coinbase_addresses.map((a) => linkAddr(a)).join(", ") : "—")}
      </div>
      <div class="card">
        <div class="card-head">Transactions <span class="spacer"></span><span class="muted" style="font-weight:400;font-size:12px">${num(items.length)} shown${b.tx_count != null && items.length < b.tx_count ? " of " + num(b.tx_count) : ""}</span></div>
        ${txs._error ? errBox("Could not load transactions: " + txs._error) : `<div id="block-txs">${txTable(items, "No transactions in this block.")}</div>`}
        <button class="loadmore" id="block-more" style="${items.length && txs.next_start != null ? "" : "display:none"}">Load more</button>
      </div>`);
    const moreBtn = $("#block-more");
    if (moreBtn) {
      let nextStart = txs.next_start;
      let loading = false;
      moreBtn.addEventListener("click", async () => {
        if (loading || nextStart == null) return;
        loading = true;
        moreBtn.disabled = true;
        moreBtn.textContent = "Loading…";
        try {
          const d = await get("/block/" + encodeURIComponent(n) + "/txs?start=" + nextStart);
          const newItems = (d && d.items) || [];
          const wrap = $("#block-txs");
          if (wrap && newItems.length) {
            const tbl = wrap.querySelector("tbody");
            if (tbl) tbl.insertAdjacentHTML("beforeend", newItems.map(txRow).join(""));
            else wrap.innerHTML = txTable(newItems);
          }
          nextStart = d.next_start;
          if (!newItems.length || nextStart == null) { moreBtn.style.display = "none"; }
        } catch (e) {
          moreBtn.textContent = "Load failed: " + e.message + " — retry";
        } finally {
          loading = false;
          if (moreBtn.style.display !== "none") { moreBtn.disabled = false; moreBtn.textContent = "Load more"; }
        }
      });
    }
  } catch (e) {
    render(`<div class="pagehead"><h2>Block</h2></div>
      <div class="card">${errBox("Could not load block " + n + ": " + e.message)}</div>`);
  }
}

/* ---------------- transaction ---------------- */
function ioRowIn(inp, i) {
  const val = inp.value_btc != null ? btcFloat(inp.value_btc) + " BTC" : "—";
  if (inp.is_coinbase) {
    return `<tr><td><span class="badge info">Coinbase</span></td><td class="r mono">—</td></tr>`;
  }
  return `<tr>
    <td>${inp.address ? linkAddr(inp.address) : `<span class="muted">—</span>`} <span class="muted">#${i}</span></td>
    <td class="r mono">${val}</td>
  </tr>`;
}
function ioRowOut(out, i) {
  const val = out.value_btc != null ? btcFloat(out.value_btc) + " BTC" : "—";
  return `<tr>
    <td>${out.address ? linkAddr(out.address) : `<span class="muted">${esc(out.script_type || "—")}</span>`} <span class="muted">#${i}</span></td>
    <td class="r mono">${val}</td>
  </tr>`;
}
async function txPage(txid) {
  document.title = "Tx " + shortHash(txid) + " — " + TITLE;
  render(`<div class="pagehead"><h2>Transaction</h2></div><div class="card">${spinner()}</div>`);
  const priceP = loadBtcPrice();
  try {
    const t = await get("/tx/" + encodeURIComponent(txid));
    const price = await priceP;
    const feeUsd = (price != null && t.fee != null) ? usd(t.fee / SATS_PER_BTC * price) : "—";
    const inBtc = t.total_in_btc;
    const outBtc = t.total_out_btc;
    render(`
      <div class="pagehead"><h2><span class="mono">${esc(shortHash(t.txid))}</span></h2>${copyBtn(t.txid)}</div>
      <div id="explain-card"><div class="card"><div style="padding:12px 16px" class="muted">Explaining transaction…</div></div></div>
      <div class="card">
        ${detailRow("Transaction ID", `<span class="mono" style="word-break:break-all">${esc(t.txid)}</span> ${copyBtn(t.txid)}`)}
        ${detailRow("Status", statusBadge(t.confirmed) + (t.confirmations != null ? ` <span class="muted">${num(t.confirmations)} confirmation${t.confirmations === 1 ? "" : "s"}</span>` : "") + (t.confirmed && t.block_height != null ? ` · Block ${linkBlock(t.block_height)}` : ""))}
        ${detailRow("Time", t.block_time != null ? esc(utcUnix(t.block_time)) + ` <span class="muted">(${ageOf(t)})</span>` : "— <span class='muted'>(unconfirmed)</span>")}
        ${detailRow("Fee", t.fee != null ? `<strong>${num(t.fee)} sat</strong> <span class="muted">(${t.fee_rate_sat_vb != null ? t.fee_rate_sat_vb + " sat/vB" : "—"} · ${feeUsd})</span>` : "—")}
        ${detailRow("Size", t.size != null ? `${num(t.size)} bytes <span class="muted">· ${num(t.vsize)} vBytes · ${num(t.weight)} weight units</span>` : "—")}
        ${detailRow("Version", num(t.version))}
        ${detailRow("Locktime", t.locktime != null ? num(t.locktime) : "—")}
        ${detailRow("Total Input", t.is_coinbase ? "<span class='muted'>— (coinbase)</span>" : (inBtc != null ? `<strong>${btcFloat(inBtc)} BTC</strong> ${usdOfBtc(inBtc, price) !== "—" ? `<span class="muted">(${usdOfBtc(inBtc, price)})</span>` : ""}` : "—"))}
        ${detailRow("Total Output", outBtc != null ? `<strong>${btcFloat(outBtc)} BTC</strong> ${usdOfBtc(outBtc, price) !== "—" ? `<span class="muted">(${usdOfBtc(outBtc, price)})</span>` : ""}` : "—")}
        ${(!t.confirmed && t.rbf_replacements) ? detailRow("RBF", '<span class="badge warn">Replaced by a newer transaction</span>') : ""}
        ${(!t.confirmed && t.rbf_replaces) ? detailRow("RBF", '<span class="badge warn">Replaces an earlier transaction</span>') : ""}
      </div>
      <div class="card">
        <div class="card-head">Inputs <span class="spacer"></span><span class="muted" style="font-weight:400;font-size:12px">${num((t.inputs || []).length)}</span></div>
        ${(t.inputs || []).length ? `<table class="tbl"><thead><tr><th>From</th><th class="r">Value</th></tr></thead><tbody>
          ${t.inputs.map(ioRowIn).join("")}
        </tbody></table>` : emptyBox("No inputs for this transaction.")}
      </div>
      <div class="card">
        <div class="card-head">Outputs <span class="spacer"></span><span class="muted" style="font-weight:400;font-size:12px">${num((t.outputs || []).length)}</span></div>
        ${(t.outputs || []).length ? `<table class="tbl"><thead><tr><th>To</th><th class="r">Value</th></tr></thead><tbody>
          ${t.outputs.map(ioRowOut).join("")}
        </tbody></table>` : emptyBox("No outputs for this transaction.")}
      </div>`);
    // "What happened" card (Feature 3) — loads independently so a slow
    // explain never blocks the page.
    loadExplainCard(txid);
  } catch (e) {
    render(`<div class="pagehead"><h2>Transaction</h2></div>
      <div class="card">${errBox("Could not load transaction " + shortHash(txid) + ": " + e.message)}</div>`);
  }
}

/* ---------------- explain card (Feature 3, tx pages) ---------------- */
function explainCardHtml(e) {
  const kindLabel = { transfer: "Transfer", coinbase: "Miner reward" }[e.kind] || e.kind || "Transaction";
  const flowRows = (e.flows || []).map((f) => {
    const toCell = f.to && f.to.hash ? linkAddr(f.to.hash) : '<span class="muted">—</span>';
    return `<tr><td>BTC</td><td class="r mono">${esc(String(f.amount ?? "—"))}</td><td><span class="muted">—</span></td><td>${toCell}</td></tr>`;
  }).join("");
  return `<div class="card">
    <div class="card-head">What happened <span class="spacer"></span><span class="badge info">${esc(kindLabel)}</span></div>
    <div style="padding:14px 16px">
      <div style="font-size:15px;line-height:1.5">${esc(e.sentence || "This transaction could not be explained.")}</div>
      ${flowRows ? `<table class="tbl" style="margin-top:12px"><thead><tr><th>Token</th><th class="r">Amount</th><th>From</th><th>To</th></tr></thead><tbody>${flowRows}</tbody></table>` : ""}
      ${e.note ? `<div class="muted" style="font-size:12px;margin-top:10px">${esc(e.note)}</div>` : ""}
    </div>
  </div>`;
}

function loadExplainCard(txid) {
  fetchJson(API + "/tx/" + encodeURIComponent(txid) + "/explain", null, 30000).then((e) => {
    const el = document.getElementById("explain-card");
    if (el) el.innerHTML = explainCardHtml(e);
  }).catch((err) => {
    const el = document.getElementById("explain-card");
    if (el) el.innerHTML = `<div class="card"><div style="padding:12px 16px" class="muted">Explanation unavailable: ${esc(err.message)}</div></div>`;
  });
}

/* ---------------- risk card (address pages) ---------------- */
function riskCardHtml(r) {
  const lvl = String(r.level || "low").toLowerCase();
  const badgeCls = { low: "ok", elevated: "warn", high: "fail", critical: "fail" }[lvl] || "grey";
  const reasons = (r.reasons || []).map((x) => `<li>${esc(x)}</li>`).join("");
  const cov = r.coverage || {};
  return `<div class="card risk-card risk-${esc(lvl)}">
    <div class="card-head">Risk Score <span class="spacer"></span><span class="badge ${badgeCls}">${esc(lvl)}</span></div>
    <div style="padding:14px 16px">
      <div style="display:flex;align-items:baseline;gap:10px"><span class="risk-score">${esc(String(r.score))}</span><span class="muted">/ 100 · ${esc(lvl)}</span></div>
      <ul class="risk-reasons">${reasons}</ul>
      <div class="muted" style="font-size:12px;margin-top:10px">${esc(r.family_note || "")}</div>
      <div class="muted" style="font-size:12px;margin-top:4px">${esc(cov.note || "")}</div>
      <div class="muted" style="font-size:12px;margin-top:4px">${esc(r.method || "")} Source: OFAC SDN list (${esc((r.sdn && r.sdn.date) || "")}, ${esc(String((r.sdn && r.sdn.labels_total) || ""))} addresses).</div>
    </div>
  </div>`;
}

function loadRiskCard(addr) {
  // 60s client timeout: the funder walk fans out to upstream pages.
  fetchJson(API + "/address/" + encodeURIComponent(addr) + "/risk", null, 60000).then((r) => {
    const el = document.getElementById("risk-card");
    if (el) el.innerHTML = riskCardHtml(r);
  }).catch((e) => {
    const el = document.getElementById("risk-card");
    if (el) el.innerHTML = `<div class="card"><div style="padding:12px 16px" class="muted">Risk score unavailable: ${esc(e.message)}</div></div>`;
  });
}

/* ---------------- cross-chain card (address pages) ---------------- */
/* Bridge linkage only: same-address EVM presence is not applicable on
   this chain (different address format). Linkage to an address on
   another chain is shown ONLY when an actual bridge transfer is
   observed — there is no keyless way to prove two different-format
   addresses belong to the same person. */
function crosschainCardHtml(c) {
  const note = `<div class="muted" style="font-size:12.5px;margin-top:2px">${esc(c.note || "")}</div>`;
  let presence = "";
  if (Array.isArray(c.evm_presence)) {
    const rows = c.evm_presence.map((p) => {
      const link = p.url ? `<a href="${esc(p.url)}" target="_blank" rel="noopener">View on ${esc(p.label)} →</a>` : esc(p.label);
      let bal, act;
      if (p.status === "active") {
        bal = `<b class="mono">${esc(p.balance || "0")} ${esc(p.native || "")}</b>${p.is_contract ? ' <span class="badge grey">Contract</span>' : ""}`;
        const parts = [];
        if (p.transactions_count != null) parts.push(`<span class="mono">${num(p.transactions_count)}</span> txs`);
        if (p.latest_tx && p.latest_tx.timestamp) parts.push(`latest ${esc(new Date(p.latest_tx.timestamp).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" }))}`);
        act = parts.length ? parts.join(" · ") : `<span class="muted">Active</span>`;
      } else if (p.status === "no_activity") {
        bal = `<span class="muted">0 ${esc(p.native || "")}</span>`;
        act = `<span class="muted">No activity</span>`;
      } else {
        bal = `<span class="muted">—</span>`;
        act = `<span class="muted">Unavailable${p.note ? " — " + esc(p.note) : ""}</span>`;
      }
      return `<tr><td><b>${esc(p.label)}</b></td><td class="r">${bal}</td><td class="r">${act}</td><td class="r">${link}</td></tr>`;
    }).join("");
    presence = `<div style="margin-top:12px"><b>Same address on every EVM chain</b></div>
      <table class="tbl"><thead><tr><th>Chain</th><th class="r">Native balance</th><th class="r">Activity</th><th class="r"></th></tr></thead><tbody>${rows}</tbody></table>`;
  } else {
    presence = `<div class="muted" style="margin-top:12px">Same-address presence does not apply here — this address format exists on one chain only. Cross-chain linkage below comes from observed bridge transfers only.</div>`;
  }
  const b = c.bridges || {};
  let bridges;
  if (b.error) {
    bridges = `<div class="muted" style="margin-top:12px">Bridge transfers unavailable: ${esc(b.error)}</div>`;
  } else if (!(b.transfers || []).length) {
    bridges = `<div class="muted" style="margin-top:12px">No ${esc(b.provider || "Wormhole")} bridge transfers observed for this address.</div>`;
  } else {
    const rows = (b.transfers || []).map((t) => {
      const when = t.timestamp ? esc(new Date(t.timestamp).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" })) : "—";
      const dir = t.direction === "in" ? '<span class="badge ok">In</span>' : t.direction === "out" ? '<span class="badge warn">Out</span>' : '<span class="badge grey">Involving</span>';
      const amt = t.amount_reported ? `<span class="mono">${esc(t.amount)}</span>` : `<span class="muted">amount not reported</span>`;
      const tok = t.token_address ? `<span class="mono muted">${esc(String(t.token_address).length > 14 ? String(t.token_address).slice(0, 10) + "…" : t.token_address)}</span>` : "";
      return `<tr><td style="white-space:nowrap">${when}</td><td>${dir}</td><td>${esc(t.from_chain || "?")} → ${esc(t.to_chain || "?")}</td><td class="r">${amt} ${tok}</td><td><span class="muted">${esc(t.app || "")}</span></td></tr>`;
    }).join("");
    bridges = `<div style="margin-top:12px"><b>Bridge transfers (${esc(b.provider || "Wormhole")})</b> <span class="muted" style="font-size:12px">${esc(b.note || "")}</span></div>
      <table class="tbl"><thead><tr><th>Date</th><th>Direction</th><th>Route</th><th class="r">Amount</th><th>Via</th></tr></thead><tbody>${rows}</tbody></table>`;
  }
  const wallet = c.walletscope_url
    ? `<div style="margin-top:14px"><a class="btn" href="${esc(c.walletscope_url)}" target="_blank" rel="noopener">💼 Open full portfolio in WalletScope →</a></div>`
    : `<div class="muted" style="font-size:12.5px;margin-top:14px">WalletScope covers EVM + Solana only — no portfolio view for this address.</div>`;
  return `<div class="card">
    <div class="card-head">Cross-chain identity</div>
    <div style="padding:14px 16px">
      ${note}
      ${presence}
      ${bridges}
      ${wallet}
    </div>
  </div>`;
}

function loadCrosschainCard(addr) {
  fetchJson(API + "/address/" + encodeURIComponent(addr) + "/crosschain", null, 30000).then((c) => {
    const el = document.getElementById("crosschain-card");
    if (el) el.innerHTML = crosschainCardHtml(c);
  }).catch((e) => {
    const el = document.getElementById("crosschain-card");
    if (el) el.innerHTML = `<div class="card"><div style="padding:12px 16px" class="muted">Cross-chain identity unavailable: ${esc(e.message)}</div></div>`;
  });
}

/* ---------------- address ---------------- */
async function addressPage(addr) {
  document.title = shortHash(addr) + " — " + TITLE;
  render(`<div class="pagehead"><h2>Address</h2></div><div class="card">${spinner()}</div>`);
  const priceP = loadBtcPrice();
  try {
    const a = await get("/address/" + encodeURIComponent(addr));
    const price = await priceP;
    const txs = await (async () => {
      try { return await get("/address/" + encodeURIComponent(addr) + "/txs"); }
      catch (e) { return { items: [], _error: e.message }; }
    })();
    const items = (txs.items || []);
    const utxo = await (async () => {
      try { return await get("/address/" + encodeURIComponent(addr) + "/utxo"); }
      catch (e) { return { items: [], note: e.message }; }
    })();
    const utxos = (utxo.items || []);
    render(`
      <div class="pagehead"><h2><span class="mono">${esc(shortHash(a.address))}</span></h2>${copyBtn(a.address)}</div>
      <div class="card">
        ${detailRow("Address", `<span class="mono" style="word-break:break-all">${esc(a.address)}</span> ${copyBtn(a.address)}`)}
        ${detailRow("Balance", `<strong>${btcFmt(a.balance_sats)} BTC</strong> <span class="muted">(${num(a.balance_sats)} sat)</span> ${price != null && a.balance_btc != null ? `· <span class="muted">${usdOfBtc(a.balance_btc, price)}</span>` : ""}`)}
        ${detailRow("Funded / Spent", `<span class="mono">${btcFmt(a.funded_sats)} BTC</span> / <span class="mono">${btcFmt(a.spent_sats)} BTC</span>`)}
        ${detailRow("Transactions", num(a.tx_count))}
        ${detailRow("Unconfirmed", a.unconfirmed_tx_count ? `${num(a.unconfirmed_tx_count)} txs · ${btcFmt(a.unconfirmed_balance_sats)} BTC` : "—")}
      </div>
      <div id="risk-card"><div class="card"><div style="padding:12px 16px" class="muted">Loading risk score…</div></div></div>
      <div id="crosschain-card"><div class="card"><div style="padding:12px 16px" class="muted">Loading cross-chain identity…</div></div></div>
      <div class="card">
        <div class="card-head">Transactions <span class="spacer"></span><span class="muted" style="font-weight:400;font-size:12px">${num(items.length)} shown${a.tx_count != null && items.length < a.tx_count ? " of " + num(a.tx_count) : ""}</span></div>
        ${txs._error ? errBox("Could not load transactions: " + txs._error) : `<div id="addr-txs">${txTable(items, "No transactions found for this address.")}</div>`}
        <button class="loadmore" id="addr-more" style="${items.length && txs.next_after ? "" : "display:none"}">Load more</button>
      </div>
      <div class="card">
        <div class="card-head">UTXOs <span class="spacer"></span><span class="muted" style="font-weight:400;font-size:12px">${utxo.total != null ? num(utxo.total) + " total" : ""}${utxo.truncated ? " · first 100 shown" : ""}</span></div>
        ${utxo.note ? `<div style="padding:16px"><div class="notice info" style="margin:0">${esc(utxo.note)}</div></div>`
          : utxos.length ? `<table class="tbl"><thead><tr><th>Outpoint</th><th>Status</th><th class="r">Value</th></tr></thead><tbody>
            ${utxos.map((u) => `<tr>
              <td>${linkTx(u.txid)}<span class="muted">:${num(u.vout)}</span></td>
              <td>${statusBadge(u.status && u.status.confirmed)}</td>
              <td class="r mono">${btcFmt(u.value_sats)} BTC</td>
            </tr>`).join("")}
          </tbody></table>` : emptyBox("No unspent outputs for this address.")}
      </div>`);
    loadRiskCard(addr);
    loadCrosschainCard(addr);
    const moreBtn = $("#addr-more");
    if (moreBtn) {
      let after = txs.next_after;
      let loading = false;
      moreBtn.addEventListener("click", async () => {
        if (loading || !after) return;
        loading = true;
        moreBtn.disabled = true;
        moreBtn.textContent = "Loading…";
        try {
          const d = await get("/address/" + encodeURIComponent(addr) + "/txs?after=" + encodeURIComponent(after));
          const newItems = (d && d.items) || [];
          const wrap = $("#addr-txs");
          if (wrap && newItems.length) {
            const tbl = wrap.querySelector("tbody");
            if (tbl) tbl.insertAdjacentHTML("beforeend", newItems.map(txRow).join(""));
            else wrap.innerHTML = txTable(newItems);
            after = d.next_after;
          }
          if (!newItems.length || !d.next_after) { moreBtn.style.display = "none"; }
        } catch (e) {
          moreBtn.textContent = "Load failed: " + e.message + " — retry";
        } finally {
          loading = false;
          if (moreBtn.style.display !== "none") { moreBtn.disabled = false; moreBtn.textContent = "Load more"; }
        }
      });
    }
  } catch (e) {
    render(`<div class="pagehead"><h2>Address</h2></div>
      <div class="card">${errBox("Could not load address " + shortHash(addr) + ": " + e.message)}</div>`);
  }
}

/* ---------------- mempool ---------------- */
async function mempoolPage() {
  document.title = "Mempool — " + TITLE;
  render(`<div class="pagehead"><h2>Mempool</h2></div><div class="card">${spinner()}</div>`);
  try {
    const m = await get("/mempool");
    const fees = m.fees || {};
    const bands = m.fee_histogram_bands || [];
    const recent = m.recent || [];
    render(`
      <div class="pagehead"><h2>Mempool</h2></div>
      <div class="statgrid">
        <div class="stat"><div class="k">Unconfirmed Transactions</div><div class="v">${num(m.count)}</div><div class="sub">${m.vsize != null ? num(m.vsize) + " vBytes" : ""}</div></div>
        <div class="stat"><div class="k">Total Fees Waiting</div><div class="v">${m.total_fee_sats != null ? btcFmt(m.total_fee_sats) + " BTC" : "—"}</div><div class="sub">${num(m.total_fee_sats)} sat</div></div>
        <div class="stat"><div class="k">Next Block Fee</div><div class="v">${fees.fastest != null ? fees.fastest + ' <small>sat/vB</small>' : "—"}</div><div class="sub">Fastest confirmation</div></div>
        <div class="stat"><div class="k">Economy Fee</div><div class="v">${fees.economy != null ? fees.economy + ' <small>sat/vB</small>' : "—"}</div><div class="sub">Minimum ${fees.minimum != null ? fees.minimum + " sat/vB" : ""}</div></div>
      </div>
      <div class="card">
        <div class="card-head">Recommended Fee Rates</div>
        ${detailRow("Fastest (next block)", fees.fastest != null ? fees.fastest + " sat/vB" : "—")}
        ${detailRow("~30 minutes", fees.half_hour != null ? fees.half_hour + " sat/vB" : "—")}
        ${detailRow("~1 hour", fees.hour != null ? fees.hour + " sat/vB" : "—")}
        ${detailRow("Economy", fees.economy != null ? fees.economy + " sat/vB" : "—")}
        ${detailRow("Minimum", fees.minimum != null ? fees.minimum + " sat/vB" : "—")}
      </div>
      <div class="card">
        <div class="card-head">Fee-Rate Bands <span class="spacer"></span><span class="muted" style="font-weight:400;font-size:12px">unconfirmed txs per sat/vB range</span></div>
        ${bands.length ? `<table class="tbl"><thead><tr><th>Fee Rate (sat/vB)</th><th class="r">Transactions</th><th class="r">Size (vBytes)</th></tr></thead><tbody>
          ${bands.map((b) => `<tr>
            <td class="mono">${esc(b.band)}</td>
            <td class="r">${num(b.count)}</td>
            <td class="r">${num(b.vsize)}</td>
          </tr>`).join("")}
        </tbody></table>` : emptyBox("Fee histogram unavailable.")}
      </div>
      <div class="card">
        <div class="card-head">Recent Unconfirmed Transactions</div>
        ${recent.length ? `<table class="tbl"><thead><tr><th>Transaction</th><th class="r">Fee</th><th class="r">Fee Rate</th><th class="r">Value</th></tr></thead><tbody>
          ${recent.map((t) => `<tr>
            <td>${linkTx(t.txid)}</td>
            <td class="r mono">${t.fee != null ? num(t.fee) + " sat" : "—"}</td>
            <td class="r mono">${t.fee_rate_sat_vb != null ? t.fee_rate_sat_vb + " sat/vB" : "—"}</td>
            <td class="r mono">${t.value_sats != null ? btcFmt(t.value_sats) + " BTC" : "—"}</td>
          </tr>`).join("")}
        </tbody></table>` : emptyBox("No recent unconfirmed transactions.")}
      </div>`);
  } catch (e) {
    render(`<div class="pagehead"><h2>Mempool</h2></div>
      <div class="card">${errBox("Could not load mempool: " + e.message)}</div>`);
  }
}

/* ---------------- API docs ---------------- */
async function apiDocsPage() {
  document.title = "API Docs — " + TITLE;
  render(`<div class="pagehead"><h2>API Documentation</h2></div><div class="card">${spinner()}</div>`);
  try {
    const d = await get("/docs");
    const eps = (d && d.endpoints) || [];
    const chains = (d && d.chains) || ["bitcoin"];
    render(`
      <div class="pagehead"><h2>API Documentation</h2></div>
      <div class="card"><div style="padding:14px 16px">BitScope exposes a read-only JSON API for ${esc(chains.join(", "))}. All responses are JSON; errors are <span class="mono">{"error": "..."}</span> with a non-2xx status. No API keys required.</div></div>
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
  try {
    const d = await get("/search?q=" + encodeURIComponent(q));
    if (!d || !d.type) { banner("No results for “" + q + "”"); return; }
    if (d.type === "block" && d.height != null) location.hash = "#/block/" + d.height;
    else if (d.type === "block" && d.hash) location.hash = "#/block/" + d.hash;
    else if (d.type === "transaction" && d.txid) location.hash = "#/tx/" + d.txid;
    else if (d.type === "address" && d.address) location.hash = "#/address/" + d.address;
    else banner("No results for “" + q + "”");
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
  [/^#\/block\/([^/]+)$/, (m) => blockPage(decodeURIComponent(m[1]))],
  [/^#\/tx\/([^/]+)$/, (m) => txPage(decodeURIComponent(m[1]))],
  [/^#\/address\/([^/]+)$/, (m) => addressPage(decodeURIComponent(m[1]))],
  [/^#\/mempool$/, () => mempoolPage()],
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
  document.title = TITLE + " — Block Explorer";
  const badge = document.getElementById("brand-badge");
  if (badge) badge.textContent = CFG.brandLetter || "B";
  const bname = document.getElementById("brand-name");
  if (bname) { const _bm = TITLE.match(/^(.*)(scope)$/i); bname.innerHTML = _bm ? esc(_bm[1]) + '<span class="brand-accent">' + esc(_bm[2]) + '</span>' : esc(TITLE) + '<span class="brand-accent">Scope</span>'; }
  const fbrand = document.getElementById("footer-brand");
  if (fbrand) fbrand.textContent = TITLE;
  const fav = document.querySelector('link[rel="icon"]');
  if (fav && CFG.brandLetter) {
    fav.href = "data:image/svg+xml," + encodeURIComponent("<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect width='32' height='32' rx='7' fill='%23f7931a'/><text x='16' y='22' font-size='17' text-anchor='middle' fill='white' font-family='Arial' font-weight='bold'>" + CFG.brandLetter + "</text></svg>");
  }
  // Scope switcher: jump between sibling Scope apps.
  const scopeSel = document.getElementById("scope-select");
  if (scopeSel) {
    scopeSel.innerHTML = `<option value="">${esc(TITLE)}</option>` + SIBLINGS.map((s) => `<option value="${esc(s.url)}">${esc(s.name)}</option>`).join("");
    scopeSel.onchange = () => { if (scopeSel.value) window.location.href = scopeSel.value; };
  }
  // Wallet links point at WalletScope from this explorer app.
  const nw = document.getElementById("nav-wallet");
  if (nw && WALLET_URL) nw.href = WALLET_URL + "/#/wallet";
  const fw = document.getElementById("footer-wallet");
  if (fw && WALLET_URL) fw.href = WALLET_URL + "/#/wallet";
  // Net pill: Bitcoin Mainnet (from config when present).
  const np = document.getElementById("netpill-text");
  if (np) np.textContent = CFG.netpill || "Bitcoin Mainnet";
  const pill = document.getElementById("netpill");
  if (pill) pill.title = "Data: mempool.space / Blockstream Esplora";
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
  // Best-effort chain list (used only for the net-pill tooltip); the app
  // is single-chain and works fine on the built-in default if it fails.
  try {
    const d = await get("/chains");
    if (d && Array.isArray(d.chains) && d.chains.length && pill) {
      const c = d.chains[0];
      pill.title = "Data: mempool.space / Blockstream Esplora" + (c.api ? " (" + c.api + ")" : "");
    }
  } catch (e) { /* keep default pill */ }
  route();
});
