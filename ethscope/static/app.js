"use strict";
/* EthScope frontend — hash-routed single page app. API: /api/ethscope/* */
const API = "/api/ethscope";

/* ---------------- helpers ---------------- */
const $ = (s) => document.querySelector(s);
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
async function get(path) {
  const r = await fetch(API + path);
  let j = null;
  try { j = await r.json(); } catch (e) { throw new Error("Bad response from server"); }
  if (!r.ok || (j && j.error)) throw new Error((j && j.error) || ("HTTP " + r.status));
  return j;
}
function num(n) {
  if (n == null || n === "") return "—";
  const x = Number(n);
  return Number.isFinite(x) ? x.toLocaleString("en-US") : String(n);
}
function ethFmt(s, dec = 6) {
  if (s == null || s === "") return "0";
  let [i, f] = String(s).split(".");
  i = Number(i).toLocaleString("en-US");
  if (!f) return i;
  f = f.replace(/0+$/, "").slice(0, dec);
  return f ? i + "." + f : i;
}
function utc(ts) {
  try { return new Date(ts).toUTCString().replace("GMT", "UTC"); } catch (e) { return ts; }
}
function shortH(h) {
  h = String(h || "");
  return h.length > 18 ? h.slice(0, 10) + "..." + h.slice(-8) : h;
}
function copyBtn(text) {
  return `<button class="copybtn" data-copy="${esc(text)}" title="Copy to clipboard">⧉</button>`;
}
function statusBadge(st) {
  if (st === "success") return '<span class="badge ok">Success</span>';
  if (st === "failed" || st === "error") return '<span class="badge fail">Failed</span>';
  if (st === "pending") return '<span class="badge info">Pending</span>';
  return `<span class="badge grey">${esc(st || "—")}</span>`;
}

/* ---------------- links ---------------- */
function linkAddr(a, full) {
  if (!a || !a.hash) return '<span class="muted">—</span>';
  const label = a.label && a.label !== a.hash ? esc(a.label) : esc(full ? a.hash : a.short || shortH(a.hash));
  const title = a.label && a.label !== a.hash ? ` title="${esc(a.hash)}"` : "";
  const tag = a.is_contract ? ' <span class="badge grey" title="Contract">C</span>' : "";
  return `<a class="mono" href="#/address/${a.hash}"${title}>${label}</a>${tag}`;
}
function linkTx(h, full) {
  if (!h) return '<span class="muted">—</span>';
  return `<a class="mono" href="#/tx/${h}">${esc(full ? h : shortH(h))}</a>`;
}
function linkBlock(n) {
  if (n == null) return '<span class="muted">—</span>';
  return `<a class="mono" href="#/block/${n}">${num(n)}</a>`;
}
function linkToken(addr, symbol) {
  if (!addr) return '<span class="muted">—</span>';
  return `<a class="mono" href="#/token/${addr}">${esc(symbol || shortH(addr))}</a>`;
}

/* ---------------- rows ---------------- */
function txRow(t, lite) {
  const cells = [
    `<td>${linkTx(t.hash)}</td>`,
    `<td>${statusBadge(t.status)}</td>`,
    `<td>${t.block != null ? linkBlock(t.block) : '<span class="muted">pending</span>'}</td>`,
    `<td class="muted" style="white-space:nowrap">${esc(t.age || "")}</td>`,
    `<td>${linkAddr(t.from)}</td>`,
    `<td>${linkAddr(t.to)}</td>`,
  ];
  if (!lite) cells.push(`<td class="r mono">${ethFmt(t.value_eth)}&nbsp;ETH</td>`);
  else cells.push(`<td class="r mono">${ethFmt(t.value_eth)}</td>`);
  if (!lite) cells.push(`<td class="r mono">${ethFmt(t.fee_eth, 8)}</td>`);
  return `<tr>${cells.join("")}</tr>`;
}
function txHead(lite) {
  return `<tr><th>Txn Hash</th><th>Status</th><th>Block</th><th>Age</th><th>From</th><th>To</th><th class="r">Value${lite ? "" : " (ETH)"}</th>${lite ? "" : '<th class="r">Fee (ETH)</th>'}</tr>`;
}
function blockRow(b) {
  return `<tr>
    <td>${linkBlock(b.height)}</td>
    <td class="muted" style="white-space:nowrap">${esc(b.age || "")}</td>
    <td>${linkAddr(b.miner)}</td>
    <td class="r">${num(b.tx_count)}</td>
    <td class="r">${b.gas_used_pct}%</td>
    <td class="r mono">${esc(b.base_fee_gwei)}&nbsp;Gwei</td>
    <td class="r mono">${ethFmt(b.burnt_fees_eth, 8)}</td>
  </tr>`;
}
function ttRow(tt) {
  return `<tr>
    <td>${linkTx(tt.tx_hash)}</td>
    <td class="muted" style="white-space:nowrap">${esc(tt.age || "")}</td>
    <td>${linkAddr(tt.from)}</td>
    <td>${linkAddr(tt.to)}</td>
    <td class="r mono">${esc(tt.amount)}&nbsp;${esc(tt.token.symbol || "")}</td>
    <td>${linkToken(tt.token.address, tt.token.symbol)}</td>
  </tr>`;
}
function internalRow(i) {
  return `<tr>
    <td>${linkTx(i.tx_hash)}</td>
    <td class="muted" style="white-space:nowrap">${esc(i.age || "")}</td>
    <td>${linkAddr(i.from)}</td>
    <td>${linkAddr(i.to)}</td>
    <td class="r mono">${ethFmt(i.value_eth)}</td>
    <td><span class="badge ${i.success ? "ok" : "fail"}">${i.success ? "Success" : "Failed"}</span></td>
  </tr>`;
}

/* ---------------- pager ---------------- */
function mountPager(tbodySel, btnSel, apiBase, rowFn, emptyMsg) {
  const tbody = $(tbodySel), btn = $(btnSel);
  let npp = null, loading = false, first = true;
  async function load() {
    if (loading) return;
    loading = true;
    btn.textContent = "Loading…";
    try {
      const q = npp ? "?" + new URLSearchParams(npp).toString() : "";
      const d = await get(apiBase + q);
      if (first && !d.items.length) {
        tbody.innerHTML = `<tr><td colspan="9"><div class="empty">${esc(emptyMsg || "No records found")}</div></td></tr>`;
      } else {
        if (first) tbody.innerHTML = "";
        tbody.insertAdjacentHTML("beforeend", d.items.map(rowFn).join(""));
      }
      first = false;
      npp = d.next_page_params;
      btn.style.display = npp ? "" : "none";
      btn.textContent = "Load more";
    } catch (e) {
      btn.textContent = "Retry";
      if (first) tbody.innerHTML = `<tr><td colspan="9"><div class="empty">Error: ${esc(e.message)}</div></td></tr>`;
    }
    loading = false;
  }
  btn.onclick = load;
  load();
}

/* ---------------- banner / view ---------------- */
function banner(msg) {
  const b = $("#error-banner");
  if (!msg) { b.classList.add("hidden"); b.textContent = ""; return; }
  b.textContent = msg;
  b.classList.remove("hidden");
}
function render(html) { $("#view").innerHTML = html; banner(null); }
function spinner() { return '<div class="spinner">Loading…</div>'; }
function detailRow(k, v) {
  return `<div class="detail"><div class="dk">${esc(k)}</div><div class="dv">${v}</div></div>`;
}

/* ---------------- search ---------------- */
function classify(q) {
  q = String(q || "").trim();
  if (/^0x[0-9a-fA-F]{64}$/.test(q)) return { kind: "tx", v: q };
  if (/^0x[0-9a-fA-F]{40}$/.test(q)) return { kind: "address", v: q };
  if (/^\d+$/.test(q)) return { kind: "block", v: q };
  if (q.length >= 2) return { kind: "query", v: q };
  return null;
}
async function doSearch(q) {
  const c = classify(q);
  if (!c) return;
  if (c.kind === "tx") location.hash = "#/tx/" + c.v;
  else if (c.kind === "address") location.hash = "#/address/" + c.v;
  else if (c.kind === "block") location.hash = "#/block/" + c.v;
  else {
    try {
      const d = await get("/search?q=" + encodeURIComponent(c.v));
      if (d.items && d.items.length) {
        const it = d.items[0];
        if (it.type === "address") location.hash = "#/address/" + it.hash;
        else if (it.type === "token") location.hash = "#/token/" + it.hash;
        else if (it.type === "transaction") location.hash = "#/tx/" + it.hash;
        else if (it.type === "block") location.hash = "#/block/" + (it.block_number || it.hash);
        else banner("No results for “" + c.v + "”");
      } else banner("No results for “" + c.v + "”");
    } catch (e) { banner(e.message); }
  }
}
function wireSearch(form, input, suggestBox) {
  let timer = null;
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    suggestBox.classList.add("hidden");
    doSearch(input.value);
  });
  input.addEventListener("input", () => {
    clearTimeout(timer);
    const q = input.value.trim();
    if (q.length < 3) { suggestBox.classList.add("hidden"); return; }
    timer = setTimeout(async () => {
      try {
        const d = await get("/search?q=" + encodeURIComponent(q));
        if (!d.items || !d.items.length) { suggestBox.classList.add("hidden"); return; }
        suggestBox.innerHTML = d.items.map((it) => {
          const route = it.type === "address" ? "#/address/" + it.hash
            : it.type === "token" ? "#/token/" + it.hash
            : it.type === "transaction" ? "#/tx/" + it.hash
            : it.type === "block" ? "#/block/" + (it.block_number || it.hash) : null;
          if (!route) return "";
          return `<a href="${route}"><span class="stype ${it.type}">${esc(it.type)}</span><span class="mono">${esc(it.name)}</span></a>`;
        }).join("");
        suggestBox.classList.remove("hidden");
      } catch (e) { /* silent */ }
    }, 300);
  });
  document.addEventListener("click", (e) => {
    if (!form.contains(e.target)) suggestBox.classList.add("hidden");
  });
}

/* ---------------- home ---------------- */
let pollTimer = null;
function clearPoll() { if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } }

function renderStats(s) {
  const el = $("#stats");
  if (!el) return;
  const chg = s.eth_price_change_24h_pct;
  const chgHtml = chg == null ? "" : `<small style="color:${chg >= 0 ? "var(--green)" : "var(--red)"}">(${chg >= 0 ? "+" : ""}${Number(chg).toFixed(2)}%)</small>`;
  const gp = s.gas_price_gwei || {};
  el.innerHTML = `
    <div class="stat"><div class="k">Ether Price</div><div class="v">$${num(s.eth_price_usd)} ${chgHtml}</div></div>
    <div class="stat"><div class="k">Latest Block</div><div class="v">${linkBlock(s.latest_block)}</div></div>
    <div class="stat"><div class="k">Avg Block Time</div><div class="v">${esc(String(s.avg_block_time_s))}<small> sec</small></div></div>
    <div class="stat"><div class="k">Total Transactions</div><div class="v">${num(s.total_transactions)}</div></div>
    <div class="stat"><div class="k">Gas Price</div><div class="v">${esc(String(gp.average ?? "—"))}<small> Gwei</small></div><div class="sub">Slow ${esc(String(gp.slow ?? "—"))} · Fast ${esc(String(gp.fast ?? "—"))}</div></div>
    <div class="stat"><div class="k">Network Utilization</div><div class="v">${esc(String(s.network_utilization_pct))}<small>%</small></div></div>`;
}

async function home() {
  clearPoll();
  document.title = "EthScope — Ethereum Block Explorer";
  render(`
    <div class="hero">
      <h1>Ethereum Mainnet Explorer</h1>
      <p>Blocks, transactions, addresses and tokens — powered by the Blockscout API.</p>
      <form id="hero-search" class="search" autocomplete="off">
        <input id="hero-input" type="text" placeholder="Search by Address / Txn Hash / Block / Token / ENS">
        <button type="submit" title="Search"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/></svg></button>
        <div id="hero-suggest" class="suggest hidden"></div>
      </form>
    </div>
    <div class="statgrid" id="stats"></div>
    <div class="cols2">
      <div class="card">
        <div class="card-head">Latest Blocks <span class="spacer"></span><span class="badge info">live</span></div>
        <table class="tbl"><thead><tr><th>Block</th><th>Age</th><th>Miner</th><th class="r">Txns</th><th class="r">Gas Used</th></tr></thead><tbody id="lb"><tr><td colspan="5"><div class="spinner">Loading…</div></td></tr></tbody></table>
      </div>
      <div class="card">
        <div class="card-head">Latest Transactions <span class="spacer"></span><span class="badge info">live</span></div>
        <table class="tbl"><thead><tr><th>Txn Hash</th><th>From</th><th>To</th><th class="r">Value</th></tr></thead><tbody id="lt"><tr><td colspan="4"><div class="spinner">Loading…</div></td></tr></tbody></table>
      </div>
    </div>`);
  wireSearch($("#hero-search"), $("#hero-input"), $("#hero-suggest"));
  async function refresh() {
    try {
      const [s, bl, tx] = await Promise.all([get("/stats"), get("/blocks"), get("/txs")]);
      renderStats(s);
      const lb = $("#lb"), lt = $("#lt");
      if (lb) lb.innerHTML = bl.items.slice(0, 10).map((b) =>
        `<tr><td>${linkBlock(b.height)}</td><td class="muted" style="white-space:nowrap">${esc(b.age || "")}</td><td>${linkAddr(b.miner)}</td><td class="r">${num(b.tx_count)}</td><td class="r">${b.gas_used_pct}%</td></tr>`).join("");
      if (lt) lt.innerHTML = tx.items.slice(0, 10).map((t) =>
        `<tr><td>${linkTx(t.hash)}</td><td>${linkAddr(t.from)}</td><td>${linkAddr(t.to)}</td><td class="r mono">${ethFmt(t.value_eth)}</td></tr>`).join("");
    } catch (e) { banner("Live update failed: " + e.message); }
  }
  await refresh();
  pollTimer = setInterval(refresh, 10000);
}

/* ---------------- block page ---------------- */
async function blockPage(n) {
  clearPoll();
  render(spinner());
  let b;
  try { b = await get("/block/" + encodeURIComponent(n)); }
  catch (e) { render(`<div class="pagehead"><h2>Block</h2></div><div class="card"><div class="empty">Block not found: ${esc(n)}<br><span class="muted">${esc(e.message)}</span></div></div>`); return; }
  document.title = `Block ${b.height} — EthScope`;
  const gasPct = b.gas_limit ? (Number(b.gas_used) / Number(b.gas_limit) * 100) : 0;
  render(`
    <div class="pagehead"><h2>Block <span class="mono">#${num(b.height)}</span></h2></div>
    <div class="card">
      ${detailRow("Block Height", `<span class="mono">${num(b.height)}</span>`)}
      ${detailRow("Timestamp", `${esc(b.age || "")} <span class="muted">(${esc(utc(b.timestamp))})</span>`)}
      ${detailRow("Block Hash", `<span class="mono">${esc(b.hash)}</span> ${copyBtn(b.hash)}`)}
      ${detailRow("Parent Hash", `<a class="mono" href="#/block/${esc(b.parent_hash)}">${esc(shortH(b.parent_hash))}</a>`)}
      ${detailRow("Miner", `${linkAddr(b.miner, true)} ${copyBtn(b.miner ? b.miner.hash : "")}`)}
      ${detailRow("Transactions", `<b>${num(b.tx_count)}</b> transactions in this block`)}
      ${detailRow("Gas Used / Limit", `<span class="mono">${num(b.gas_used)} / ${num(b.gas_limit)}</span> (${gasPct.toFixed(2)}%)<div class="gasbar"><i style="width:${Math.min(100, gasPct)}%"></i></div>`)}
      ${detailRow("Base Fee", `<span class="mono">${esc(b.base_fee_gwei)} Gwei</span>`)}
      ${detailRow("Burnt Fees", `<span class="mono">${ethFmt(b.burnt_fees_eth, 8)} ETH</span>`)}
      ${detailRow("Block Rewards", (b.rewards || []).map((r) => `${ethFmt(r.value_eth, 8)} ETH → ${linkAddr({ hash: r.recipient, label: r.recipient, short: shortH(r.recipient) })}`).join("<br>") || '<span class="muted">—</span>')}
      ${detailRow("Size", `<span class="mono">${num(b.size)} bytes</span>`)}
      ${detailRow("Withdrawals", num(b.withdrawals_count))}
      ${detailRow("Blob Transactions", num(b.blob_tx_count))}
      ${detailRow("Nonce", `<span class="mono">${esc(b.nonce || "—")}</span>`)}
    </div>
    <div class="card">
      <div class="card-head">Transactions <span class="spacer"></span><span class="badge grey">${num(b.tx_count)}</span></div>
      <table class="tbl"><thead>${txHead(false)}</thead><tbody id="btx"></tbody></table>
      <button class="loadmore" id="btx-more">Load more</button>
    </div>`);
  mountPager("#btx", "#btx-more", `/block/${b.height}/txs`, (t) => txRow(t, false), "No transactions in this block");
}

/* ---------------- tx page ---------------- */
async function txPage(h, retried) {
  clearPoll();
  render(spinner());
  let t;
  try { t = await get("/tx/" + h); }
  catch (e) {
    // a 64-hex string may be a block hash — fall back to the block page
    if (!retried) {
      try { await get("/block/" + h); location.replace("#/block/" + h); return; }
      catch (e2) { /* fall through */ }
    }
    render(`<div class="pagehead"><h2>Transaction</h2></div><div class="card"><div class="empty">Transaction not found<br><span class="mono muted">${esc(h)}</span><br><span class="muted">${esc(e.message)}</span></div></div>`);
    return;
  }
  document.title = `Tx ${shortH(t.hash)} — EthScope`;
  const inputShown = t.input && t.input.length > 2000 ? t.input.slice(0, 2000) + "…" : (t.input || "0x");
  render(`
    <div class="pagehead"><h2>Transaction Details</h2> ${statusBadge(t.status)}</div>
    <div class="card">
      ${detailRow("Transaction Hash", `<span class="mono">${esc(t.hash)}</span> ${copyBtn(t.hash)}`)}
      ${detailRow("Status", statusBadge(t.status))}
      ${detailRow("Block", t.block != null ? `${linkBlock(t.block)} <span class="muted">(${num(t.confirmations)} confirmations)</span>` : '<span class="badge info">Pending</span>')}
      ${detailRow("Timestamp", `${esc(t.age || "")} <span class="muted">(${esc(utc(t.timestamp))})</span>`)}
      ${detailRow("From", `${linkAddr(t.from, true)} ${copyBtn(t.from ? t.from.hash : "")}`)}
      ${detailRow("To", t.to ? `${linkAddr(t.to, true)} ${copyBtn(t.to.hash)}` : (t.created_contract ? `Contract created: <a class="mono" href="#/address/${esc(t.created_contract)}">${esc(shortH(t.created_contract))}</a>` : '<span class="muted">—</span>'))}
      ${detailRow("Value", `<b class="mono">${ethFmt(t.value_eth)} ETH</b>`)}
      ${detailRow("Transaction Fee", `<span class="mono">${ethFmt(t.fee_eth, 8)} ETH</span>`)}
      ${detailRow("Gas Price", `<span class="mono">${esc(t.gas_price_gwei)} Gwei</span>`)}
      ${detailRow("Gas Limit / Used", `<span class="mono">${num(t.gas_limit)} / ${num(t.gas_used)}</span>`)}
      ${t.max_fee_per_gas_gwei && t.max_fee_per_gas_gwei !== "0.00" ? detailRow("Max Fee / Priority", `<span class="mono">${esc(t.max_fee_per_gas_gwei)} / ${esc(t.max_priority_fee_gwei)} Gwei</span>`) : ""}
      ${detailRow("Burnt Fee", `<span class="mono">${ethFmt(t.burnt_fee_eth, 8)} ETH</span>`)}
      ${detailRow("Nonce / Position", `<span class="mono">${num(t.nonce)} / ${num(t.position)}</span>`)}
      ${detailRow("Type", `<span class="mono">${t.type == 2 ? "2 (EIP-1559)" : esc(String(t.type ?? "—"))}</span>`)}
      ${t.revert_reason ? detailRow("Revert Reason", `<span class="mono">${esc(t.revert_reason)}</span>`) : ""}
      ${t.decoded_method ? detailRow("Method", `<span class="mono">${esc(t.decoded_method)}</span>`) : ""}
      ${detailRow("Input Data", `<pre class="hex">${esc(inputShown)}</pre>${t.input_truncated ? '<div class="muted" style="font-size:12px">Input truncated to first 2000 chars.</div>' : ""}${t.input_utf8 ? `<div class="utf8box"><div class="k">Decoded as UTF-8</div><div class="mono">${esc(t.input_utf8)}</div></div>` : ""}`)}
    </div>
    <div class="card" id="tt-card">
      <div class="card-head">Token Transfers <span class="spacer"></span><span class="badge grey" id="tt-count">…</span></div>
      <table class="tbl"><thead><tr><th>Txn Hash</th><th>Age</th><th>From</th><th>To</th><th class="r">Amount</th><th>Token</th></tr></thead><tbody id="ttx"></tbody></table>
    </div>
    <div class="card">
      <div class="card-head">Logs <span class="spacer"></span><span class="badge grey" id="log-count">…</span></div>
      <div id="logs"></div>
    </div>
    <div class="card">
      <div class="card-head">Internal Transactions <span class="spacer"></span><span class="badge grey" id="in-count">…</span></div>
      <table class="tbl"><thead><tr><th>Parent Txn</th><th>Age</th><th>From</th><th>To</th><th class="r">Value</th><th>Status</th></tr></thead><tbody id="inx"></tbody></table>
    </div>`);

  // token transfers (embedded in detail + overflow note)
  const ttBody = $("#ttx");
  if (t.token_transfers && t.token_transfers.length) {
    ttBody.innerHTML = t.token_transfers.map(ttRow).join("");
    $("#tt-count").textContent = t.token_transfers.length + (t.token_transfers_overflow ? "+" : "");
    if (t.token_transfers_overflow) ttBody.insertAdjacentHTML("afterend", '<div class="empty">More transfers exist — showing first page.</div>');
  } else {
    ttBody.innerHTML = '<tr><td colspan="6"><div class="empty">No token transfers</div></td></tr>';
    $("#tt-count").textContent = "0";
  }
  // logs + internal in parallel
  try {
    const [logs, ix] = await Promise.all([get(`/tx/${h}/logs`), get(`/tx/${h}/internal`)]);
    $("#log-count").textContent = logs.items.length;
    $("#logs").innerHTML = logs.items.length ? logs.items.slice(0, 25).map((l) =>
      `<div class="detail"><div class="dk mono">${esc(shortH(l.address ? l.address.hash : ""))}<br><span class="muted">log #${l.log_index}</span></div>
       <div class="dv"><div class="logtopics">${(l.topics || []).map((tp, i) => `<span class="mono" style="font-size:11.5px">${i === 0 ? "◈" : "◦"} ${esc(tp)}</span>`).join("")}</div>
       ${l.data && l.data !== "0x" ? `<pre class="hex" style="max-height:120px;margin-top:8px">${esc(l.data)}</pre>` : ""}</div></div>`
    ).join("") : '<div class="empty">No logs emitted</div>';
    $("#in-count").textContent = ix.items.length;
    $("#inx").innerHTML = ix.items.length ? ix.items.map(internalRow).join("") : '<tr><td colspan="6"><div class="empty">No internal transactions</div></td></tr>';
  } catch (e) { banner("Failed to load logs/internal txs: " + e.message); }
}

/* ---------------- address page ---------------- */
let addrTab = "txs";
async function addressPage(h) {
  clearPoll();
  render(spinner());
  let a;
  try { a = await get("/address/" + h); }
  catch (e) { render(`<div class="pagehead"><h2>Address</h2></div><div class="card"><div class="empty">Address not found<br><span class="mono muted">${esc(h)}</span></div></div>`); return; }
  document.title = `${a.ens || shortH(a.hash)} — EthScope`;
  const badges = [
    a.is_contract ? '<span class="badge info">Contract</span>' : '<span class="badge grey">EOA</span>',
    a.is_verified ? '<span class="badge ok">Verified</span>' : "",
    a.ens ? `<span class="badge info">${esc(a.ens)}</span>` : "",
    a.is_scam ? '<span class="badge fail">Scam</span>' : "",
  ].join(" ");
  const tokenBanner = a.token
    ? `<div class="card" style="border-color:#d9c8f5;background:#faf7ff"><div style="padding:12px 16px">🪙 This address is the <b>${esc(a.token.name)} (${esc(a.token.symbol)})</b> token contract. <a href="#/token/${a.hash}">View token tracker →</a></div></div>`
    : "";
  render(`
    <div class="pagehead"><h2>Address</h2></div>
    <div class="card">
      <div style="padding:14px 16px;border-bottom:1px solid var(--line)">
        <div class="addr-title"><span class="mono" style="font-size:15px">${esc(a.hash)}</span> ${copyBtn(a.hash)}</div>
        <div style="margin-top:8px">${badges}</div>
      </div>
      ${detailRow("ETH Balance", `<b class="mono">${ethFmt(a.balance_eth)} ETH</b>`)}
      ${detailRow("Transactions", num(a.transactions_count))}
      ${a.name ? detailRow("Name", esc(a.name)) : ""}
      ${a.creator ? detailRow("Creator", `<a class="mono" href="#/address/${esc(a.creator)}">${esc(shortH(a.creator))}</a>`) : ""}
      ${a.creation_tx ? detailRow("Creation Txn", linkTx(a.creation_tx)) : ""}
    </div>
    ${tokenBanner}
    <div class="card">
      <div class="tabs">
        <button class="tab active" data-tab="txs">Transactions</button>
        <button class="tab" data-tab="token-transfers">Token Transfers</button>
        <button class="tab" data-tab="internal">Internal Txns</button>
        <button class="tab" data-tab="tokens">Tokens</button>
      </div>
      <div id="tab-body"></div>
    </div>`);

  document.querySelectorAll(".tab").forEach((t) => {
    t.onclick = () => {
      document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
      t.classList.add("active");
      addrTab = t.dataset.tab;
      renderAddrTab(h);
    };
  });
  renderAddrTab(h);
}

function renderAddrTab(h) {
  const body = $("#tab-body");
  if (!body) return;
  if (addrTab === "txs") {
    body.innerHTML = `<table class="tbl"><thead>${txHead(false)}</thead><tbody id="atx"></tbody></table><button class="loadmore" id="atx-more">Load more</button>`;
    mountPager("#atx", "#atx-more", `/address/${h}/txs`, (t) => txRow(t, false), "No transactions");
  } else if (addrTab === "token-transfers") {
    body.innerHTML = `<table class="tbl"><thead><tr><th>Txn Hash</th><th>Age</th><th>From</th><th>To</th><th class="r">Amount</th><th>Token</th></tr></thead><tbody id="att"></tbody></table><button class="loadmore" id="att-more">Load more</button>`;
    mountPager("#att", "#att-more", `/address/${h}/token-transfers`, ttRow, "No token transfers");
  } else if (addrTab === "internal") {
    body.innerHTML = `<table class="tbl"><thead><tr><th>Parent Txn</th><th>Age</th><th>From</th><th>To</th><th class="r">Value</th><th>Status</th></tr></thead><tbody id="ain"></tbody></table><button class="loadmore" id="ain-more">Load more</button>`;
    mountPager("#ain", "#ain-more", `/address/${h}/internal`, internalRow, "No internal transactions");
  } else if (addrTab === "tokens") {
    body.innerHTML = `<table class="tbl"><thead><tr><th>Token</th><th class="r">Balance</th><th>Type</th></tr></thead><tbody id="atok"><tr><td colspan="3"><div class="spinner">Loading…</div></td></tr></tbody></table>`;
    get(`/address/${h}/tokens`).then((d) => {
      const el = $("#atok");
      if (!el) return;
      el.innerHTML = d.items.length ? d.items.map((t) =>
        `<tr><td>${linkToken(t.address, t.symbol)} <span class="muted">${esc(t.name || "")}</span></td><td class="r mono">${esc(t.balance)}</td><td><span class="badge grey">${esc(t.type || "—")}</span></td></tr>`
      ).join("") : '<tr><td colspan="3"><div class="empty">No token balances</div></td></tr>';
    }).catch((e) => { const el = $("#atok"); if (el) el.innerHTML = `<tr><td colspan="3"><div class="empty">Error: ${esc(e.message)}</div></td></tr>`; });
  }
}

/* ---------------- token page ---------------- */
async function tokenPage(h) {
  clearPoll();
  render(spinner());
  let t;
  try { t = await get("/token/" + h); }
  catch (e) { render(`<div class="pagehead"><h2>Token</h2></div><div class="card"><div class="empty">Token not found<br><span class="mono muted">${esc(h)}</span></div></div>`); return; }
  document.title = `${t.symbol || "Token"} — EthScope`;
  const price = t.exchange_rate_usd ? `$${num(t.exchange_rate_usd)}` : "—";
  render(`
    <div class="pagehead"><h2>Token <span class="badge info">${esc(t.type || "ERC-20")}</span></h2></div>
    <div class="card">
      <div style="padding:14px 16px;border-bottom:1px solid var(--line)">
        <div class="addr-title">
          ${t.icon_url ? `<img src="${esc(t.icon_url)}" alt="" width="28" height="28" style="border-radius:50%">` : ""}
          <b style="font-size:17px">${esc(t.name || "—")} (${esc(t.symbol || "—")})</b>
        </div>
        <div class="mono" style="margin-top:6px"><a href="#/address/${esc(t.address)}">${esc(t.address)}</a> ${copyBtn(t.address)}</div>
      </div>
      ${detailRow("Total Supply", `<span class="mono">${num(t.total_supply)} ${esc(t.symbol || "")}</span>`)}
      ${detailRow("Decimals", `<span class="mono">${esc(String(t.decimals ?? "—"))}</span>`)}
      ${detailRow("Holders", num(t.holders))}
      ${detailRow("Price", `<span class="mono">${price}</span>`)}
      ${t.circulating_supply ? detailRow("Circulating Supply", `<span class="mono">${num(t.circulating_supply)} ${esc(t.symbol || "")}</span>`) : ""}
      ${t.volume_24h_usd ? detailRow("Volume (24h)", `<span class="mono">$${num(t.volume_24h_usd)}</span>`) : ""}
    </div>
    <div class="card">
      <div class="card-head">Transfers</div>
      <table class="tbl"><thead><tr><th>Txn Hash</th><th>Age</th><th>From</th><th>To</th><th class="r">Amount</th><th>Token</th></tr></thead><tbody id="ttk"></tbody></table>
      <button class="loadmore" id="ttk-more">Load more</button>
    </div>`);
  mountPager("#ttk", "#ttk-more", `/token/${h}/transfers`, ttRow, "No transfers");
}

/* ---------------- router ---------------- */
const routes = [
  [/^#\/?$/, () => home()],
  [/^#\/block\/([^/]+)$/, (m) => blockPage(m[1])],
  [/^#\/tx\/(0x[0-9a-fA-F]{64})$/, (m) => txPage(m[1])],
  [/^#\/address\/(0x[0-9a-fA-F]{40})$/, (m) => addressPage(m[1])],
  [/^#\/token\/(0x[0-9a-fA-F]{40})$/, (m) => tokenPage(m[1])],
];
function route() {
  banner(null);
  const h = location.hash || "#/";
  for (const [re, fn] of routes) {
    const m = h.match(re);
    if (m) { fn(m); return; }
  }
  document.title = "Not found — EthScope";
  render(`<div class="card" style="margin-top:22px"><div class="empty">Page not found: <span class="mono">${esc(h)}</span><br><a href="#/">Back to home</a></div></div>`);
}

document.addEventListener("DOMContentLoaded", () => {
  wireSearch($("#search-form"), $("#search-input"), $("#search-suggest"));
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
  route();
});
