"use strict";
/* SolScope frontend — hash-routed single page app for the Solana explorer.
   No build step, no CDN. Per-app behaviour comes from /config.js
   (window.SCOPE_CONFIG). API: /api/solscope/* — one chain (solana).
   Same light Etherscan-like theme/classes as the canonical EVM Scope app
   (styles.css is shared): .card, .table/.tbl, .badge, .muted, .topbar,
   .wrap, .error-banner, .statgrid, .detail, .copybtn, .loadmore. */
const CFG = window.SCOPE_CONFIG || {};
const SCOPE = CFG.scope || "solscope";
const TITLE = CFG.title || "SolScope";
const API = CFG.apiPrefix || "/api/solscope";
const WALLET_URL = CFG.walletUrl || "https://walletscope.vercel.app";
const SIBLINGS = Array.isArray(CFG.siblings) ? CFG.siblings : [];
const LAMPORTS_PER_SOL = 1e9;

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
/* USD price that can be tiny (token prices): show up to 6 decimals below $1. */
function usdPrice(n) {
  if (n == null || n === "") return "—";
  const x = Number(n);
  if (!Number.isFinite(x)) return "—";
  if (x > 0 && x < 1) return "$" + x.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 6 });
  return usd(x);
}
/* SOL amount (float SOL): trim trailing zeros, up to 9 decimals. */
function solFmt(v, dec) {
  if (v == null || v === "") return "—";
  const x = Number(v);
  if (!Number.isFinite(x)) return "—";
  const d = dec == null ? 9 : dec;
  return x.toLocaleString("en-US", { minimumFractionDigits: 0, maximumFractionDigits: d });
}
function lamportsSol(l) {
  if (l == null || l === "") return "—";
  const x = Number(l);
  if (!Number.isFinite(x)) return "—";
  return solFmt(x / LAMPORTS_PER_SOL);
}
/* Unix seconds -> UTC string. */
function utcUnix(sec) {
  if (sec == null || sec === "") return "—";
  try { return new Date(Number(sec) * 1000).toUTCString().replace("GMT", "UTC"); } catch (e) { return String(sec); }
}
/* Relative age from unix seconds ("5 min ago"). Backend also supplies an
   `age` string on most objects; prefer it and use this as fallback. */
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
  return esc(ageFromUnix(obj && obj.block_time));
}
/* Short base58 display: first 4 + "…" + last 4. */
function short58(s) {
  s = String(s || "");
  return s.length > 12 ? s.slice(0, 4) + "…" + s.slice(-4) : s;
}
function copyBtn(text) {
  return `<button class="copybtn" data-copy="${esc(text)}" title="Copy to clipboard">⧉</button>`;
}
function statusBadge(st) {
  if (st === "success") return '<span class="badge ok">Success</span>';
  if (st === "failed" || st === "error") return '<span class="badge fail">Failed</span>';
  return `<span class="badge grey">${esc(st || "—")}</span>`;
}
function pctChange(v) {
  if (v == null || v === "") return "";
  const x = Number(v);
  if (!Number.isFinite(x)) return "";
  const color = x >= 0 ? "var(--green)" : "var(--red)";
  return ` <small style="color:${color}">(${x >= 0 ? "+" : ""}${x.toFixed(2)}%)</small>`;
}

/* ---------------- links ---------------- */
function linkSlot(n) { return `<a href="#/slot/${esc(n)}" class="mono">${num(n)}</a>`; }
function linkTx(sig) { return `<a href="#/tx/${esc(sig)}" class="mono">${esc(short58(sig))}</a>`; }
function linkAddr(a) { return `<a href="#/address/${esc(a)}" class="mono">${esc(short58(a))}</a>`; }
function linkToken(m) { return `<a href="#/token/${esc(m)}" class="mono">${esc(short58(m))}</a>`; }
function linkProgram(a) { return `<a href="#/program/${esc(a)}" class="mono">${esc(short58(a))}</a>`; }
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

/* ---------------- SOL price cache ---------------- */
/* /stats is the only source of SOL price; address/token pages reuse it so
   USD values do not need a second stats round-trip per page load. */
let solPriceUsd = null;
let solPriceTried = false;
async function loadSolPrice() {
  if (solPriceTried) return solPriceUsd;
  solPriceTried = true;
  try {
    const s = await get("/stats");
    if (s && s.sol_price_usd != null) solPriceUsd = Number(s.sol_price_usd);
  } catch (e) { /* price stays unknown; USD values render as "—" */ }
  return solPriceUsd;
}

/* ---------------- signatures table (shared) ---------------- */
function sigRow(s) {
  const err = s.err ? '<span class="badge fail">Failed</span>' : '<span class="badge ok">Success</span>';
  return `<tr>
    <td>${linkTx(s.signature)}${s.memo ? ` <span class="muted" title="Memo">${esc(String(s.memo).slice(0, 60))}</span>` : ""}</td>
    <td>${s.slot != null ? linkSlot(s.slot) : "—"}</td>
    <td>${ageOf(s)}</td>
    <td>${err}</td>
  </tr>`;
}
function sigTable(sigs, emptyMsg) {
  if (!sigs || !sigs.length) return emptyBox(emptyMsg || "No signatures found.");
  return `<table class="tbl"><thead><tr><th>Signature</th><th>Slot</th><th>Age</th><th>Status</th></tr></thead><tbody>
    ${sigs.map(sigRow).join("")}
  </tbody></table>`;
}

/* ---------------- polling ---------------- */
let pollTimer = null;
function clearPoll() { if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } }

/* ---------------- home ---------------- */
function renderStats(s) {
  const el = $("#stats");
  if (!el) return;
  const price = s.sol_price_usd != null ? usd(s.sol_price_usd) : "—";
  const chg = pctChange(s.sol_change_24h_pct);
  const supply = s.circulating_supply != null
    ? num(s.circulating_supply) + ' <small>SOL</small>'
    : "—";
  const total = s.total_supply != null ? "of " + num(s.total_supply) + " total" : "";
  el.innerHTML = `
    <div class="stat"><div class="k">SOL Price</div><div class="v">${price}${chg}</div><div class="sub">USD · 24h change</div></div>
    <div class="stat"><div class="k">Current Slot</div><div class="v">${s.slot != null ? linkSlot(s.slot) : "—"}</div><div class="sub">Block height ${num(s.block_height)} · Epoch ${num(s.epoch)}</div></div>
    <div class="stat"><div class="k">Transactions / Sec</div><div class="v">${s.tps != null ? Number(s.tps).toLocaleString("en-US", { maximumFractionDigits: 1 }) : "—"}</div><div class="sub">${num(s.transaction_count)} total transactions</div></div>
    <div class="stat"><div class="k">Circulating Supply</div><div class="v">${supply}</div><div class="sub">${esc(total)}</div></div>`;
}
function slotsTable(items) {
  if (!items || !items.length) return emptyBox("No slots found.");
  return `<table class="tbl"><thead><tr><th>Slot</th><th>Block Height</th><th>Age</th><th class="r">Tx Count</th></tr></thead><tbody>
    ${items.map((it) => `<tr>
      <td>${linkSlot(it.slot)}</td>
      <td>${num(it.block_height)}</td>
      <td>${ageOf(it)}</td>
      <td class="r">${num(it.tx_count)}</td>
    </tr>`).join("")}
  </tbody></table>`;
}
function homeTxTable(sigs, slot) {
  if (!sigs || !sigs.length) return emptyBox("No transactions in the latest slot.");
  return `<table class="tbl"><thead><tr><th>Signature</th><th>Slot</th><th>Age</th></tr></thead><tbody>
    ${sigs.slice(0, 10).map((sig) => `<tr>
      <td>${linkTx(sig)}</td>
      <td>${linkSlot(slot)}</td>
      <td>${esc(ageFromUnix(slot && slot.block_time))}</td>
    </tr>`).join("")}
  </tbody></table>`;
}
async function refreshHome() {
  // Stats (never fatal: a failed stats fetch leaves the previous cards).
  try {
    const s = await get("/stats");
    if (s && s.sol_price_usd != null) solPriceUsd = Number(s.sol_price_usd);
    renderStats(s);
  } catch (e) {
    const el = $("#stats");
    if (el && !el.children.length) el.innerHTML = `<div class="card" style="grid-column:1/-1">${errBox("Could not load network stats: " + e.message)}</div>`;
  }
  // Latest slots, then latest transactions from the newest slot.
  try {
    const d = await get("/slots?limit=10");
    const items = (d && d.items) || [];
    const body = $("#slots-body");
    if (body) body.innerHTML = slotsTable(items);
    const txBody = $("#txs-body");
    if (txBody) {
      if (!items.length) { txBody.innerHTML = emptyBox("No slots found."); }
      else {
        try {
          const sl = await get("/slot/" + items[0].slot);
          const sigs = (sl && sl.signatures) || [];
          txBody.innerHTML = homeTxTable(sigs, sl);
        } catch (e2) {
          txBody.innerHTML = errBox("Could not load latest transactions: " + e2.message);
        }
      }
    }
  } catch (e) {
    const body = $("#slots-body");
    if (body) body.innerHTML = errBox("Could not load latest slots: " + e.message);
    const txBody = $("#txs-body");
    if (txBody) txBody.innerHTML = emptyBox("Latest transactions unavailable while slots fail to load.");
  }
}
async function homePage() {
  document.title = TITLE + " — Solana Block Explorer";
  render(`
    <div class="statgrid" id="stats">${spinner()}</div>
    <div class="cols2">
      <div class="card">
        <div class="card-head">Latest Slots <span class="spacer"></span><span class="muted" style="font-weight:400;font-size:12px">Solana Mainnet</span></div>
        <div id="slots-body">${spinner()}</div>
      </div>
      <div class="card">
        <div class="card-head">Latest Transactions <span class="spacer"></span><span class="muted" style="font-weight:400;font-size:12px">from the newest slot</span></div>
        <div id="txs-body">${spinner()}</div>
      </div>
    </div>`);
  await refreshHome();
  pollTimer = setInterval(refreshHome, 10000);
}

/* ---------------- slot ---------------- */
async function slotPage(n) {
  document.title = "Slot " + n + " — " + TITLE;
  render(`<div class="pagehead"><h2>Slot <span class="mono">${num(n)}</span></h2></div><div class="card">${spinner()}</div>`);
  try {
    const s = await get("/slot/" + encodeURIComponent(n));
    const sigs = s.signatures || [];
    render(`
      <div class="pagehead"><h2>Slot <span class="mono">${num(s.slot)}</span></h2>${copyBtn(String(s.slot))}</div>
      <div class="card">
        ${detailRow("Slot", `<span class="mono">${num(s.slot)}</span>`)}
        ${detailRow("Block Height", num(s.block_height))}
        ${detailRow("Blockhash", `<span class="mono">${esc(s.blockhash || "—")}</span> ${s.blockhash ? copyBtn(s.blockhash) : ""}`)}
        ${detailRow("Parent Slot", s.parent_slot != null ? linkSlot(s.parent_slot) : "—")}
        ${detailRow("Previous Blockhash", `<span class="mono">${esc(s.previous_blockhash || "—")}</span>`)}
        ${detailRow("Time", s.block_time != null ? esc(utcUnix(s.block_time)) + ` <span class="muted">(${ageOf(s)})</span>` : "—")}
        ${detailRow("Transactions", num(s.tx_count))}
      </div>
      <div class="card">
        <div class="card-head">Signatures <span class="spacer"></span><span class="muted" style="font-weight:400;font-size:12px">${num(sigs.length)} shown${s.tx_count != null && sigs.length < s.tx_count ? " of " + num(s.tx_count) : ""}</span></div>
        ${sigTable(sigs.map((sig) => ({ signature: sig, slot: s.slot, block_time: s.block_time, err: null })), "No signatures in this slot.")}
      </div>`);
  } catch (e) {
    render(`<div class="pagehead"><h2>Slot <span class="mono">${num(n)}</span></h2></div>
      <div class="card">${errBox("Could not load slot " + n + ": " + e.message)}</div>`);
  }
}

/* ---------------- transaction ---------------- */
function instructionHtml(ix, i) {
  const info = ix.info && typeof ix.info === "object" ? ix.info : null;
  const infoJson = info ? JSON.stringify(info) : (ix.info != null ? String(ix.info) : "");
  const pretty = infoJson.length > 400 ? infoJson.slice(0, 400) + "…" : infoJson;
  return `<div class="detail"><div class="dk">Instruction ${i + 1}</div><div class="dv">
    <strong>${esc(ix.program || "Unknown program")}</strong>
    ${ix.type ? ` <span class="badge info">${esc(ix.type)}</span>` : ""}
    ${ix.program_id ? `<br><span class="muted">Program ID:</span> <a href="#/program/${esc(ix.program_id)}" class="mono">${esc(short58(ix.program_id))}</a> ${copyBtn(ix.program_id)}` : ""}
    ${pretty ? `<pre class="hex" style="max-height:160px;margin:8px 0 0">${esc(pretty)}</pre>` : ""}
  </div></div>`;
}
async function txPage(sig) {
  document.title = "Tx " + short58(sig) + " — " + TITLE;
  render(`<div class="pagehead"><h2>Transaction</h2></div><div class="card">${spinner()}</div>`);
  try {
    const t = await get("/tx/" + encodeURIComponent(sig));
    const accounts = t.accounts || [];
    const ixs = t.instructions || [];
    const logs = t.logs || [];
    render(`
      <div class="pagehead"><h2><span class="mono">${esc(short58(t.signature))}</span></h2>${copyBtn(t.signature)}</div>
      <div class="card">
        ${detailRow("Signature", `<span class="mono" style="word-break:break-all">${esc(t.signature)}</span> ${copyBtn(t.signature)}`)}
        ${detailRow("Status", statusBadge(t.status) + (t.err ? ` <span class="mono muted">${esc(typeof t.err === "string" ? t.err : JSON.stringify(t.err))}</span>` : ""))}
        ${detailRow("Slot", t.slot != null ? linkSlot(t.slot) : "—")}
        ${detailRow("Time", t.block_time != null ? esc(utcUnix(t.block_time)) + ` <span class="muted">(${ageOf(t)})</span>` : "—")}
        ${detailRow("Fee", t.fee_sol != null ? `${solFmt(t.fee_sol)} SOL <span class="muted">(${num(t.fee_lamports)} lamports)</span>` : (t.fee_lamports != null ? `${lamportsSol(t.fee_lamports)} SOL <span class="muted">(${num(t.fee_lamports)} lamports)</span>` : "—"))}
        ${detailRow("Compute Units Consumed", num(t.compute_units_consumed))}
      </div>
      <div class="card">
        <div class="card-head">Instructions <span class="spacer"></span><span class="muted" style="font-weight:400;font-size:12px">${num(ixs.length)}</span></div>
        ${ixs.length ? ixs.map(instructionHtml).join("") : emptyBox("No instructions decoded for this transaction.")}
      </div>
      <div class="card">
        <div class="card-head">Account Balance Changes</div>
        ${accounts.length ? `<table class="tbl"><thead><tr><th>Account</th><th>Flags</th><th class="r">Pre (SOL)</th><th class="r">Post (SOL)</th><th class="r">Change (SOL)</th></tr></thead><tbody>
          ${accounts.map((a) => {
            const ch = a.change_sol != null ? Number(a.change_sol) : null;
            const chHtml = ch == null ? "—" : `<span style="color:${ch < 0 ? "var(--red)" : ch > 0 ? "var(--green)" : "inherit"}">${ch > 0 ? "+" : ""}${solFmt(ch)}</span>`;
            const flags = [a.signer ? "Signer" : "", a.writable ? "Writable" : ""].filter(Boolean).join(", ") || "—";
            return `<tr>
              <td>${linkAddr(a.address)} ${copyBtn(a.address)}</td>
              <td class="muted">${esc(flags)}</td>
              <td class="r mono">${a.pre_balance_sol != null ? solFmt(a.pre_balance_sol) : "—"}</td>
              <td class="r mono">${a.post_balance_sol != null ? solFmt(a.post_balance_sol) : "—"}</td>
              <td class="r mono">${chHtml}</td>
            </tr>`;
          }).join("")}
        </tbody></table>` : emptyBox("No account balance changes reported for this transaction.")}
      </div>
      <div class="card">
        <div class="card-head">Logs</div>
        ${logs.length ? `<div style="padding:16px"><pre class="hex" style="margin:0">${esc(logs.join("\n"))}</pre></div>` : emptyBox("No program logs for this transaction.")}
      </div>`);
  } catch (e) {
    render(`<div class="pagehead"><h2>Transaction</h2></div>
      <div class="card">${errBox("Could not load transaction " + short58(sig) + ": " + e.message)}</div>`);
  }
}

/* ---------------- address ---------------- */
function typeBadge(type) {
  if (type === "program") return '<span class="badge info">Program</span>';
  if (type === "mint") return '<span class="badge warn">Token Mint</span>';
  return '<span class="badge grey">Account</span>';
}
async function addressPage(addr) {
  document.title = short58(addr) + " — " + TITLE;
  render(`<div class="pagehead"><h2>Address</h2></div><div class="card">${spinner()}</div>`);
  const priceP = loadSolPrice();
  try {
    const a = await get("/address/" + encodeURIComponent(addr));
    const price = await priceP;
    const usdBal = (price != null && a.sol_balance != null) ? usd(Number(a.sol_balance) * price) : "—";
    const holdings = (a.token_holdings && a.token_holdings.items) || [];
    const sigs = a.signatures || [];
    const typeLink = a.type === "program"
      ? ` <a href="#/program/${esc(a.address)}" class="btn" style="margin-left:8px">Open as Program →</a>`
      : a.type === "mint"
        ? ` <a href="#/token/${esc(a.address)}" class="btn" style="margin-left:8px">Open as Token →</a>`
        : "";
    render(`
      <div class="pagehead"><h2><span class="mono">${esc(short58(a.address))}</span></h2>${copyBtn(a.address)} <a class="btn" href="${walletHref(a.address)}" target="_blank" rel="noopener">💼 Wallet Dashboard</a></div>
      <div class="card">
        ${detailRow("Address", `<span class="mono" style="word-break:break-all">${esc(a.address)}</span> ${copyBtn(a.address)}`)}
        ${detailRow("Type", typeBadge(a.type) + typeLink)}
        ${detailRow("SOL Balance", `<strong>${solFmt(a.sol_balance)} SOL</strong> <span class="muted">(${num(a.sol_balance_lamports)} lamports)</span> ${price != null ? `· <span class="muted">${usdBal}</span>` : '<span class="muted">(SOL price unavailable)</span>'}`)}
        ${detailRow("Owner", a.owner ? `${linkAddr(a.owner)} <span class="mono muted">${esc(a.owner)}</span>` : "—")}
        ${detailRow("Executable", a.executable ? "Yes" : "No")}
      </div>
      <div class="card">
        <div class="card-head">SPL Token Holdings <span class="spacer"></span><span class="muted" style="font-weight:400;font-size:12px">${a.token_holdings ? num(a.token_holdings.tokens_count) + " tokens · " + num(a.token_holdings.tokens_priced_count) + " priced" + (a.token_holdings.nft_count ? " · " + num(a.token_holdings.nft_count) + " NFTs" : "") : ""}</span></div>
        ${holdings.length ? `<table class="tbl"><thead><tr><th>Token</th><th class="r">Balance</th><th class="r">Price</th><th class="r">Value</th></tr></thead><tbody>
          ${holdings.map((h) => `<tr>
            <td>${linkToken(h.address)} ${h.symbol ? `<strong>${esc(h.symbol)}</strong>` : ""} ${h.name ? `<span class="muted">${esc(h.name)}</span>` : ""} <span class="badge grey">${esc(h.type || "SPL")}</span></td>
            <td class="r mono">${num(h.balance)}${h.decimals != null ? ` <span class="muted">(${num(h.decimals)} dp)</span>` : ""}</td>
            <td class="r">${usdPrice(h.usd_price)}${pctChange(h.usd_change_24h_pct)}</td>
            <td class="r">${usd(h.usd_value)}</td>
          </tr>`).join("")}
        </tbody></table>
        <div class="total-row"><span>Total token value</span><span>${a.token_holdings ? usd(a.token_holdings.total_usd) : "—"}</span></div>`
        : emptyBox("No SPL token holdings found for this address.")}
      </div>
      <div class="card">
        <div class="card-head">Recent Signatures</div>
        <div id="addr-sigs">${sigTable(sigs, "No signatures found for this address.")}</div>
        <button class="loadmore" id="addr-more" style="${sigs.length ? "" : "display:none"}">Load more</button>
      </div>`);
    // Load-more wiring (cursor = last signature shown).
    const moreBtn = $("#addr-more");
    if (moreBtn) {
      let before = sigs.length ? sigs[sigs.length - 1].signature : null;
      let loading = false;
      moreBtn.addEventListener("click", async () => {
        if (loading || !before) return;
        loading = true;
        moreBtn.disabled = true;
        moreBtn.textContent = "Loading…";
        try {
          const d = await get("/address/" + encodeURIComponent(addr) + "/signatures?before=" + encodeURIComponent(before));
          const items = (d && d.items) || [];
          const wrap = $("#addr-sigs");
          if (wrap && items.length) {
            const tbl = wrap.querySelector("tbody");
            if (tbl) tbl.insertAdjacentHTML("beforeend", items.map(sigRow).join(""));
            else wrap.innerHTML = sigTable(items);
            before = d.before || items[items.length - 1].signature;
          }
          if (!items.length || !d.before) { moreBtn.style.display = "none"; }
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
      <div class="card">${errBox("Could not load address " + short58(addr) + ": " + e.message)}</div>`);
  }
}

/* ---------------- token ---------------- */
async function tokenPage(mint) {
  document.title = short58(mint) + " — Token — " + TITLE;
  render(`<div class="pagehead"><h2>Token</h2></div><div class="card">${spinner()}</div>`);
  try {
    const t = await get("/token/" + encodeURIComponent(mint));
    const holders = t.holders || null;
    const transfers = await (async () => {
      try { return await get("/token/" + encodeURIComponent(mint) + "/transfers"); }
      catch (e) { return { items: [], before: null, _error: e.message }; }
    })();
    const tItems = (transfers.items || []);
    render(`
      <div class="pagehead"><h2>${t.symbol ? esc(t.symbol) + " " : ""}<span class="mono">${esc(short58(t.address))}</span></h2>${copyBtn(t.address)}</div>
      <div class="card">
        ${detailRow("Mint Address", `<span class="mono" style="word-break:break-all">${esc(t.address)}</span> ${copyBtn(t.address)}`)}
        ${detailRow("Name", esc(t.name || "—"))}
        ${detailRow("Symbol", esc(t.symbol || "—"))}
        ${detailRow("Decimals", num(t.decimals))}
        ${detailRow("Supply", t.supply != null ? `<strong>${num(t.supply)}</strong> <span class="muted">(raw: ${esc(t.supply_raw || "—")})</span>` : "—")}
        ${detailRow("Mint Authority", t.mint_authority ? `${linkAddr(t.mint_authority)} <span class="mono muted">${esc(t.mint_authority)}</span>` : "None (fixed supply)")}
        ${detailRow("Freeze Authority", t.freeze_authority ? `${linkAddr(t.freeze_authority)} <span class="mono muted">${esc(t.freeze_authority)}</span>` : "None")}
        ${detailRow("Initialized", t.is_initialized ? "Yes" : "No")}
        ${detailRow("Price", t.usd_price != null ? `${usdPrice(t.usd_price)}${pctChange(t.usd_change_24h_pct)}` : "— <span class='muted'>(unpriced)</span>")}
      </div>
      <div class="card">
        <div class="card-head">Top Holders</div>
        ${holders && holders.length ? `<table class="tbl"><thead><tr><th>Holder</th><th class="r">Amount</th><th class="r">Share</th></tr></thead><tbody>
          ${holders.map((h) => `<tr>
            <td>${linkAddr(h.address)} ${copyBtn(h.address)}</td>
            <td class="r mono">${num(h.ui_amount != null ? h.ui_amount : h.amount)}</td>
            <td class="r">${h.share_pct != null ? Number(h.share_pct).toFixed(2) + "%" : "—"}</td>
          </tr>`).join("")}
        </tbody></table>`
        : (t.holders_note ? `<div style="padding:16px"><div class="notice info" style="margin:0">${esc(t.holders_note)}</div></div>` : emptyBox("Holder data unavailable for this mint."))}
      </div>
      <div class="card">
        <div class="card-head">Recent Transfers</div>
        ${transfers._error ? errBox("Could not load transfers: " + transfers._error) : `<div id="token-transfers">${sigTable(tItems, "No transfers found for this token.")}</div>`}
        <button class="loadmore" id="token-more" style="${tItems.length ? "" : "display:none"}">Load more</button>
      </div>`);
    const moreBtn = $("#token-more");
    if (moreBtn) {
      let before = tItems.length ? tItems[tItems.length - 1].signature : null;
      let loading = false;
      moreBtn.addEventListener("click", async () => {
        if (loading || !before) return;
        loading = true;
        moreBtn.disabled = true;
        moreBtn.textContent = "Loading…";
        try {
          const d = await get("/token/" + encodeURIComponent(mint) + "/transfers?before=" + encodeURIComponent(before));
          const items = (d && d.items) || [];
          const wrap = $("#token-transfers");
          if (wrap && items.length) {
            const tbl = wrap.querySelector("tbody");
            if (tbl) tbl.insertAdjacentHTML("beforeend", items.map(sigRow).join(""));
            else wrap.innerHTML = sigTable(items);
            before = d.before || items[items.length - 1].signature;
          }
          if (!items.length || !d.before) { moreBtn.style.display = "none"; }
        } catch (e) {
          moreBtn.textContent = "Load failed: " + e.message + " — retry";
        } finally {
          loading = false;
          if (moreBtn.style.display !== "none") { moreBtn.disabled = false; moreBtn.textContent = "Load more"; }
        }
      });
    }
  } catch (e) {
    render(`<div class="pagehead"><h2>Token</h2></div>
      <div class="card">${errBox("Could not load token " + short58(mint) + ": " + e.message)}</div>`);
  }
}

/* ---------------- program ---------------- */
async function programPage(addr) {
  document.title = short58(addr) + " — Program — " + TITLE;
  render(`<div class="pagehead"><h2>Program</h2></div><div class="card">${spinner()}</div>`);
  try {
    const p = await get("/program/" + encodeURIComponent(addr));
    const sigs = p.signatures || [];
    render(`
      <div class="pagehead"><h2><span class="mono">${esc(short58(p.address))}</span></h2>${copyBtn(p.address)} <span class="badge info">Program</span></div>
      <div class="card">
        ${detailRow("Program Address", `<span class="mono" style="word-break:break-all">${esc(p.address)}</span> ${copyBtn(p.address)}`)}
        ${detailRow("Executable", p.executable ? "Yes" : "No")}
        ${detailRow("Owner", p.owner ? `${linkAddr(p.owner)} <span class="mono muted">${esc(p.owner)}</span>` : "—")}
        ${detailRow("Lamports", num(p.lamports) + (p.lamports != null ? ` <span class="muted">(${lamportsSol(p.lamports)} SOL)</span>` : ""))}
        ${detailRow("Data Length", p.data_length != null ? num(p.data_length) + " bytes" : "—")}
        ${detailRow("Also see", `<a href="#/address/${esc(p.address)}">Address view</a>`)}
      </div>
      <div class="card">
        <div class="card-head">Recent Invoking Signatures</div>
        ${sigTable(sigs, "No recent invoking signatures found for this program.")}
      </div>`);
  } catch (e) {
    render(`<div class="pagehead"><h2>Program</h2></div>
      <div class="card">${errBox("Could not load program " + short58(addr) + ": " + e.message)}</div>`);
  }
}

/* ---------------- API docs ---------------- */
async function apiDocsPage() {
  document.title = "API Docs — " + TITLE;
  render(`<div class="pagehead"><h2>API Documentation</h2></div><div class="card">${spinner()}</div>`);
  try {
    const d = await get("/docs");
    const eps = (d && d.endpoints) || [];
    const chains = (d && d.chains) || ["solana"];
    render(`
      <div class="pagehead"><h2>API Documentation</h2></div>
      <div class="card"><div style="padding:14px 16px">SolScope exposes a read-only JSON API for ${esc(chains.join(", "))}. All responses are JSON; errors are <span class="mono">{"error": "..."}</span> with a non-2xx status. No API keys required.</div></div>
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
    if (d.type === "slot" && d.slot != null) location.hash = "#/slot/" + d.slot;
    else if (d.type === "transaction" && d.signature) location.hash = "#/tx/" + d.signature;
    else if (d.type === "account" && d.address) location.hash = "#/address/" + d.address;
    else if (d.type === "token" && d.address) location.hash = "#/token/" + d.address;
    else if (d.type === "program" && d.address) location.hash = "#/program/" + d.address;
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
  [/^#\/slot\/(\d+)$/, (m) => slotPage(m[1])],
  [/^#\/tx\/([^/]+)$/, (m) => txPage(decodeURIComponent(m[1]))],
  [/^#\/address\/([^/]+)$/, (m) => addressPage(decodeURIComponent(m[1]))],
  [/^#\/token\/([^/]+)$/, (m) => tokenPage(decodeURIComponent(m[1]))],
  [/^#\/program\/([^/]+)$/, (m) => programPage(decodeURIComponent(m[1]))],
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
  if (badge) badge.textContent = CFG.brandLetter || "S";
  const bname = document.getElementById("brand-name");
  if (bname) bname.innerHTML = esc(TITLE.replace(/Scope$/, "")) + '<span class="brand-accent">Scope</span>';
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
  // Wallet links point at WalletScope from this explorer app.
  const nw = document.getElementById("nav-wallet");
  if (nw && WALLET_URL) nw.href = WALLET_URL + "/#/wallet";
  const fw = document.getElementById("footer-wallet");
  if (fw && WALLET_URL) fw.href = WALLET_URL + "/#/wallet";
  // Net pill: Solana Mainnet (from config when present).
  const np = document.getElementById("netpill-text");
  if (np) np.textContent = CFG.netpill || "Solana Mainnet";
  const pill = document.getElementById("netpill");
  if (pill) pill.title = "Data: Solana public RPC";
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
      pill.title = "Data: Solana public RPC" + (c.rpc ? " (" + c.rpc + ")" : "");
    }
  } catch (e) { /* keep default pill */ }
  route();
});
