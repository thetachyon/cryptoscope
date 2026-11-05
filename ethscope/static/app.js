"use strict";
/* EthScope frontend — hash-routed single page app.
   API: /api/ethscope/{chain}/* (chain-prefixed; legacy mainnet aliases exist
   server-side but the frontend always uses the chain-prefixed form).
   Chains: ethereum, sepolia, optimism, gnosis. No build step, no CDN. */
const API = "/api/ethscope";

/* ---------------- chain state ---------------- */
const FALLBACK_CHAINS = [
  { id: "ethereum", label: "Ethereum", native: "ETH" },
  { id: "sepolia", label: "Sepolia", native: "ETH" },
  { id: "optimism", label: "Optimism", native: "ETH" },
  { id: "gnosis", label: "Gnosis", native: "xDAI" },
];
let CHAINS = FALLBACK_CHAINS.slice();
let CHAIN = localStorage.getItem("ethscope-chain") || "ethereum";
let chainLoadFailed = false;
if (!FALLBACK_CHAINS.some((c) => c.id === CHAIN)) CHAIN = "ethereum";
function chainInfo() { return CHAINS.find((c) => c.id === CHAIN) || FALLBACK_CHAINS[0]; }
function chainLabel() { return chainInfo().label; }
function nativeSym() { return chainInfo().native || "ETH"; }
function updateNetpill() {
  const t = document.getElementById("netpill-text");
  const p = document.getElementById("netpill");
  if (!t || !p) return;
  t.textContent = chainLabel();
  p.classList.toggle("warn", chainLoadFailed);
  p.title = chainLoadFailed
    ? "Chain list could not be loaded from the server — using the built-in list"
    : "Data: Blockscout v2 API, " + chainLabel();
}
async function loadChains() {
  try {
    const d = await getGlobal("/chains");
    if (d && Array.isArray(d.chains) && d.chains.length) { CHAINS = d.chains; chainLoadFailed = false; }
    else throw new Error("empty chain list");
  } catch (e) {
    CHAINS = FALLBACK_CHAINS.slice();
    chainLoadFailed = true;
  }
  if (!CHAINS.some((c) => c.id === CHAIN)) {
    CHAIN = "ethereum";
    localStorage.setItem("ethscope-chain", CHAIN);
  }
  const sel = document.getElementById("chain-select");
  if (sel) {
    sel.innerHTML = CHAINS.map((c) =>
      `<option value="${esc(c.id)}"${c.id === CHAIN ? " selected" : ""}>${esc(c.label)}</option>`).join("");
    sel.onchange = () => {
      CHAIN = sel.value;
      localStorage.setItem("ethscope-chain", CHAIN);
      updateNetpill();
      route(); // re-render the current page on the new chain
    };
  }
  updateNetpill();
}

/* ---------------- labels (mainnet-only, curated) ---------------- */
/* labels.json is curated for Ethereum mainnet only, so badges are shown
   only when the selected chain is ethereum — the same address on another
   chain is a different contract/account. */
let LABELS = null;
async function loadLabels() {
  if (LABELS) return LABELS;
  try {
    const r = await fetch("/labels.json");
    const j = await r.json();
    LABELS = new Map((j.labels || []).map((l) => [String(l.address).toLowerCase(), l.label]));
  } catch (e) { LABELS = new Map(); }
  return LABELS;
}
function labelFor(hash) {
  if (!hash || CHAIN !== "ethereum" || !LABELS) return null;
  return LABELS.get(String(hash).toLowerCase()) || null;
}
function labelPill(hash) {
  const lab = labelFor(hash);
  return lab ? ` <span class="label-pill" title="Curated label (labels.json)">${esc(lab)}</span>` : "";
}

/* ---------------- helpers ---------------- */
const $ = (s) => document.querySelector(s);
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function ipfsHttp(u) {
  if (!u) return u;
  if (String(u).startsWith("ipfs://")) return "https://dweb.link/ipfs/" + String(u).slice(7);
  return u;
}
async function fetchJson(url, opts) {
  // 20s client-side guard so a stalled upstream can never hang the UI.
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 20000);
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
/* Chain-prefixed API call — every data fetch goes through here. */
async function get(path) { return fetchJson(API + "/" + CHAIN + path); }
/* Global (non-chain) endpoints: /chains, /docs. */
async function getGlobal(path) { return fetchJson(API + path); }
async function postJson(path, body) {
  return fetchJson(API + "/" + CHAIN + path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}
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
function csvCell(v) {
  const s = String(v ?? "");
  return /[",\n\r]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
}

/* ---------------- Keccak-256 (Ethereum hash, pure JS) ---------------- */
/* 64-bit lanes as [hi, lo] 32-bit pairs; verified against the backend's
   Python keccak (empty -> c5d24601..., "hello" -> 1c8aff95...). */
const KECCAK_RC = [
  [0x00000000, 0x00000001], [0x00000000, 0x00008082], [0x80000000, 0x0000808A],
  [0x80000000, 0x80008000], [0x00000000, 0x0000808B], [0x00000000, 0x80000001],
  [0x80000000, 0x80008081], [0x80000000, 0x00008009], [0x00000000, 0x0000008A],
  [0x00000000, 0x00000088], [0x00000000, 0x80008009], [0x00000000, 0x8000000A],
  [0x00000000, 0x8000808B], [0x80000000, 0x0000008B], [0x80000000, 0x00008089],
  [0x80000000, 0x00008003], [0x80000000, 0x00008002], [0x80000000, 0x00000080],
  [0x00000000, 0x0000800A], [0x80000000, 0x8000000A], [0x80000000, 0x80008081],
  [0x80000000, 0x00008080], [0x00000000, 0x80000001], [0x80000000, 0x80008008],
];
const KECCAK_R = [
  [0, 36, 3, 41, 18],
  [1, 44, 10, 45, 2],
  [62, 6, 43, 15, 61],
  [28, 55, 25, 21, 56],
  [27, 20, 39, 8, 14],
];
function _rol64(hi, lo, n) {
  n %= 64;
  if (n === 0) return [hi, lo];
  if (n < 32) return [((hi << n) | (lo >>> (32 - n))) >>> 0, ((lo << n) | (hi >>> (32 - n))) >>> 0];
  const m = n - 32;
  if (m === 0) return [lo, hi];
  return [((lo << m) | (hi >>> (32 - m))) >>> 0, ((hi << m) | (lo >>> (32 - m))) >>> 0];
}
function _keccakF(s) {
  for (let round = 0; round < 24; round++) {
    const C = [];
    for (let x = 0; x < 5; x++) {
      let hi = 0, lo = 0;
      for (let y = 0; y < 5; y++) { hi ^= s[x + 5 * y][0]; lo ^= s[x + 5 * y][1]; }
      C[x] = [hi, lo];
    }
    const D = [];
    for (let x = 0; x < 5; x++) {
      const r = _rol64(C[(x + 1) % 5][0], C[(x + 1) % 5][1], 1);
      D[x] = [C[(x + 4) % 5][0] ^ r[0], C[(x + 4) % 5][1] ^ r[1]];
    }
    for (let x = 0; x < 5; x++) for (let y = 0; y < 5; y++) {
      s[x + 5 * y][0] ^= D[x][0];
      s[x + 5 * y][1] ^= D[x][1];
    }
    const B = new Array(25);
    for (let x = 0; x < 5; x++) for (let y = 0; y < 5; y++)
      B[y + 5 * ((2 * x + 3 * y) % 5)] = _rol64(s[x + 5 * y][0], s[x + 5 * y][1], KECCAK_R[x][y]);
    for (let x = 0; x < 5; x++) for (let y = 0; y < 5; y++) {
      const i = x + 5 * y;
      s[i][0] = B[i][0] ^ ((~B[(x + 1) % 5 + 5 * y][0]) & B[(x + 2) % 5 + 5 * y][0]);
      s[i][1] = B[i][1] ^ ((~B[(x + 1) % 5 + 5 * y][1]) & B[(x + 2) % 5 + 5 * y][1]);
    }
    s[0][0] ^= KECCAK_RC[round][0];
    s[0][1] ^= KECCAK_RC[round][1];
  }
}
function keccak256(input) {
  const data = typeof input === "string" ? new TextEncoder().encode(input) : input;
  const rate = 136;
  const total = Math.floor((data.length + 2 + rate - 1) / rate) * rate;
  const padded = new Uint8Array(total);
  padded.set(data, 0);
  padded[data.length] = 0x01;
  padded[total - 1] |= 0x80;
  const state = Array.from({ length: 25 }, () => [0, 0]);
  for (let off = 0; off < total; off += rate) {
    for (let i = 0; i < rate / 8; i++) {
      const o = off + i * 8;
      const lo = (padded[o] | (padded[o + 1] << 8) | (padded[o + 2] << 16) | (padded[o + 3] << 24)) >>> 0;
      const hi = (padded[o + 4] | (padded[o + 5] << 8) | (padded[o + 6] << 16) | (padded[o + 7] << 24)) >>> 0;
      state[i][0] ^= hi;
      state[i][1] ^= lo;
    }
    _keccakF(state);
  }
  let hex = "";
  for (let i = 0; i < 4; i++) {
    const hi = state[i][0] >>> 0, lo = state[i][1] >>> 0;
    for (const w of [lo, hi]) for (let b = 0; b < 4; b++) hex += ((w >>> (b * 8)) & 0xff).toString(16).padStart(2, "0");
  }
  return hex;
}

/* ---------------- minimal ABI encoder (write contract) ---------------- */
function _toBigInt(v) {
  if (typeof v === "bigint") return v;
  if (typeof v === "number") {
    if (!Number.isInteger(v)) throw new Error("Not an integer: " + v);
    return BigInt(v);
  }
  const s = String(v).trim();
  try { return BigInt(s); } catch (e) { throw new Error("Not an integer: " + v); }
}
function _hexToBytes(hex) {
  let h = String(hex).replace(/^0x/i, "");
  if (!/^[0-9a-fA-F]*$/.test(h)) throw new Error("Not hex bytes: " + hex);
  if (h.length % 2) h = "0" + h;
  const out = new Uint8Array(h.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(h.substr(i * 2, 2), 16);
  return out;
}
function _bytesToHex(bytes) {
  return [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
}
function _concatBytes(...arrs) {
  const len = arrs.reduce((a, b) => a + b.length, 0);
  const out = new Uint8Array(len);
  let o = 0;
  for (const a of arrs) { out.set(a, o); o += a.length; }
  return out;
}
function _wordFromBigInt(v) {
  v = ((v % (1n << 256n)) + (1n << 256n)) % (1n << 256n);
  const out = new Uint8Array(32);
  for (let i = 31; i >= 0; i--) { out[i] = Number(v & 0xffn); v >>= 8n; }
  return out;
}
function parseAbiType(t) {
  const m = /^([a-zA-Z]+)([0-9]*)((?:\[\d*\])*)$/.exec(t || "");
  if (!m) throw new Error("Unsupported ABI type: " + t);
  const base = m[1] + m[2];
  const dims = [...(m[3].match(/\[(\d*)\]/g) || [])].map((s) => {
    const n = s.slice(1, -1);
    return n === "" ? null : parseInt(n, 10);
  });
  let ok = base === "address" || base === "bool" || base === "string" || base === "bytes";
  if (!ok && /^(uint|int)(8|16|24|32|40|48|56|64|72|80|88|96|104|112|120|128|136|144|152|160|168|176|184|192|200|208|216|224|232|240|248|256)$/.test(base)) ok = true;
  if (!ok && /^bytes([1-9]|[12][0-9]|3[0-2])$/.test(base)) ok = true;
  if (!ok) throw new Error("Unsupported ABI type: " + t);
  return { base, dims };
}
function _isDynamic(base, dims) {
  if (dims.length) {
    if (dims[dims.length - 1] === null) return true;
    return _isDynamic(base, dims.slice(0, -1));
  }
  return base === "string" || base === "bytes";
}
function _encodeStatic(base, dims, v) {
  if (dims.length) {
    const n = dims[dims.length - 1];
    if (!Array.isArray(v) || v.length !== n) throw new Error(`Expected array of length ${n} for ${base}[${n}]`);
    return _concatBytes(...v.map((item) => _encodeStatic(base, dims.slice(0, -1), item)));
  }
  if (base === "address") {
    if (!/^0x[0-9a-fA-F]{40}$/.test(String(v))) throw new Error("Not an address: " + v);
    const out = new Uint8Array(32);
    out.set(_hexToBytes(v), 12);
    return out;
  }
  if (base.startsWith("uint")) {
    const bits = parseInt(base.slice(4), 10);
    const n = _toBigInt(v);
    if (n < 0n || n >= (1n << BigInt(bits))) throw new Error(`Integer ${n} out of range for ${base}`);
    return _wordFromBigInt(n);
  }
  if (base.startsWith("int")) {
    const bits = parseInt(base.slice(3), 10);
    const n = _toBigInt(v);
    if (n < -(1n << (BigInt(bits) - 1n)) || n >= (1n << (BigInt(bits) - 1n))) throw new Error(`Integer ${n} out of range for ${base}`);
    return _wordFromBigInt(n);
  }
  if (base === "bool") {
    if (typeof v !== "boolean") throw new Error("Not a boolean: " + v);
    const out = new Uint8Array(32);
    out[31] = v ? 1 : 0;
    return out;
  }
  if (base.startsWith("bytes") && base !== "bytes") {
    const n = parseInt(base.slice(5), 10);
    const raw = _hexToBytes(String(v));
    if (raw.length !== n) throw new Error(`Expected ${n} bytes for ${base}, got ${raw.length}`);
    const out = new Uint8Array(32);
    out.set(raw, 0);
    return out;
  }
  throw new Error("Cannot statically encode " + base);
}
function _encodeDynamic(base, dims, v) {
  if (dims.length && dims[dims.length - 1] === null) {
    if (!Array.isArray(v)) throw new Error("Expected array for " + base + "[]");
    const elem = base + dims.slice(0, -1).map((d) => `[${d}]`).join("");
    const body = _encodeArgs(Array(v.length).fill(elem), v);
    return _concatBytes(_wordFromBigInt(BigInt(v.length)), body);
  }
  if (base === "string") {
    const raw = new TextEncoder().encode(String(v));
    return _concatBytes(_wordFromBigInt(BigInt(raw.length)), raw, new Uint8Array((32 - (raw.length % 32)) % 32));
  }
  if (base === "bytes") {
    const raw = _hexToBytes(String(v));
    return _concatBytes(_wordFromBigInt(BigInt(raw.length)), raw, new Uint8Array((32 - (raw.length % 32)) % 32));
  }
  throw new Error("Cannot dynamically encode " + base);
}
function _encodeArgs(types, values) {
  if (types.length !== values.length) throw new Error(`Expected ${types.length} argument(s), got ${values.length}`);
  const heads = [], tails = [];
  let offset = 32 * types.length;
  types.forEach((t, i) => {
    const { base, dims } = parseAbiType(t);
    if (_isDynamic(base, dims)) {
      heads.push(_wordFromBigInt(BigInt(offset)));
      const tail = _encodeDynamic(base, dims, values[i]);
      tails.push(tail);
      offset += tail.length;
    } else {
      heads.push(_encodeStatic(base, dims, values[i]));
    }
  });
  return _concatBytes(...heads, ...tails);
}
/* Parse one form-field string into a JS value for an ABI type. */
function parseArg(type, raw) {
  const { base, dims } = parseAbiType(type);
  const s = String(raw ?? "").trim();
  if (dims.length) {
    if (!s) return [];
    let arr;
    try { arr = JSON.parse(s); } catch (e) { throw new Error(`Expected a JSON array for ${type}`); }
    if (!Array.isArray(arr)) throw new Error(`Expected a JSON array for ${type}`);
    const elem = base + dims.slice(0, -1).map((d) => `[${d == null ? "" : d}]`).join("");
    return arr.map((item) => parseArg(elem, typeof item === "string" ? item : JSON.stringify(item)));
  }
  if (base === "bool") {
    if (/^(true|1)$/i.test(s)) return true;
    if (/^(false|0)$/i.test(s)) return false;
    throw new Error(`Not a boolean (use true/false): ${raw}`);
  }
  if (base === "string") return s;
  return s; // address / uint / int / bytes(N): validated by the encoder
}
function encodeFunctionCall(fn, args) {
  const inTypes = (fn.inputs || []).map((i) => i.type);
  const selector = keccak256(`${fn.name}(${inTypes.join(",")})`).slice(0, 8);
  return "0x" + selector + _bytesToHex(_encodeArgs(inTypes, args));
}
function ethToWeiHex(ethStr) {
  const s = String(ethStr).trim();
  if (!/^\d+(\.\d+)?$/.test(s)) throw new Error("ETH value must be a non-negative decimal number");
  const [whole, frac = ""] = s.split(".");
  const wei = BigInt(whole) * (10n ** 18n) + BigInt((frac + "0".repeat(18)).slice(0, 18) || "0");
  return "0x" + wei.toString(16);
}

/* ---------------- SVG line chart (hand-rolled, no dependency) ---------------- */
function svgChart(points, opts = {}) {
  const vals = (points || []).map((p) => p.y).filter((v) => Number.isFinite(v));
  if (!points || !points.length || !vals.length) return '<div class="empty">No chart data</div>';
  const W = 760, H = 260, padL = 72, padR = 14, padT = 14, padB = 30;
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
  return `<svg viewBox="0 0 ${W} ${H}" class="chart-svg" role="img" aria-label="${esc(opts.label || "chart")}">
    <line x1="${padL}" y1="${padT}" x2="${padL}" y2="${H - padB}" stroke="#e3e9f0"/>
    <line x1="${padL}" y1="${H - padB}" x2="${W - padR}" y2="${H - padB}" stroke="#e3e9f0"/>
    <line x1="${padL}" y1="${Y(mid).toFixed(1)}" x2="${W - padR}" y2="${Y(mid).toFixed(1)}" stroke="#eef1f5"/>
    <text x="${padL - 8}" y="${padT + 4}" text-anchor="end" class="chart-tick">${esc(fmt(max))}</text>
    <text x="${padL - 8}" y="${Y(mid).toFixed(1) + 4}" text-anchor="end" class="chart-tick">${esc(fmt(mid))}</text>
    <text x="${padL - 8}" y="${H - padB + 4}" text-anchor="end" class="chart-tick">${esc(fmt(min))}</text>
    <polyline points="${line}" fill="none" stroke="#0784c3" stroke-width="2"/>
    ${points.map((p, i) => `<circle cx="${X(i).toFixed(1)}" cy="${Y(p.y).toFixed(1)}" r="2.5" fill="#0784c3"><title>${esc(p.x)}: ${esc(fmt(p.y))}</title></circle>`).join("")}
    <text x="${padL}" y="${H - 8}" class="chart-tick">${esc(points[0].x)}</text>
    ${points.length > 2 ? `<text x="${W / 2}" y="${H - 8}" text-anchor="middle" class="chart-tick">${esc(points[Math.floor(points.length / 2)].x)}</text>` : ""}
    <text x="${W - padR}" y="${H - 8}" text-anchor="end" class="chart-tick">${esc(points[points.length - 1].x)}</text>
  </svg>`;
}

/* ---------------- links ---------------- */
function linkAddr(a, full) {
  if (!a || !a.hash) return '<span class="muted">—</span>';
  const label = a.label && a.label !== a.hash ? esc(a.label) : esc(full ? a.hash : a.short || shortH(a.hash));
  const title = a.label && a.label !== a.hash ? ` title="${esc(a.hash)}"` : "";
  const tag = a.is_contract ? ' <span class="badge grey" title="Contract">C</span>' : "";
  const pill = labelFor(a.hash) && labelFor(a.hash) !== (a.label && a.label !== a.hash ? a.label : null) ? labelPill(a.hash) : "";
  return `<a class="mono" href="#/address/${a.hash}"${title}>${label}</a>${tag}${pill}`;
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
function linkContract(addr, text) {
  if (!addr) return "";
  return `<a href="#/contract/${addr}">${esc(text || "View contract →")}</a>`;
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
  if (!lite) cells.push(`<td class="r mono">${ethFmt(t.value_eth)}&nbsp;${esc(nativeSym())}</td>`);
  else cells.push(`<td class="r mono">${ethFmt(t.value_eth)}</td>`);
  if (!lite) cells.push(`<td class="r mono">${ethFmt(t.fee_eth, 8)}</td>`);
  return `<tr>${cells.join("")}</tr>`;
}
function txHead(lite) {
  return `<tr><th>Txn Hash</th><th>Status</th><th>Block</th><th>Age</th><th>From</th><th>To</th><th class="r">Value${lite ? "" : " (" + esc(nativeSym()) + ")"}</th>${lite ? "" : '<th class="r">Fee (' + esc(nativeSym()) + ')</th>'}</tr>`;
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
  if (!tbody || !btn) return;
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

/* ---------------- CSV export ---------------- */
/* Follows next_page_params up to 25 pages / 5,000 rows, builds the CSV
   client-side with proper quoting, and triggers a download. */
async function exportCsv(btn, apiBase, filename, header, toRow) {
  if (!btn || btn.disabled) return;
  const orig = btn.textContent;
  btn.disabled = true;
  try {
    const rows = [];
    let npp = null, pages = 0, truncated = false;
    do {
      btn.textContent = `Fetching page ${pages + 1}… (${rows.length} rows)`;
      const q = npp ? "?" + new URLSearchParams(npp).toString() : "";
      const d = await get(apiBase + q);
      for (const it of d.items || []) {
        rows.push(toRow(it));
        if (rows.length >= 5000) { truncated = true; break; }
      }
      npp = d.next_page_params;
      pages++;
      if (truncated) break;
    } while (npp && pages < 25);
    if (pages >= 25 && npp) truncated = true;
    const csv = [header, ...rows].map((r) => r.map(csvCell).join(",")).join("\r\n");
    const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
    btn.textContent = `Exported ${rows.length} rows${truncated ? " (capped at 5,000 rows / 25 pages)" : ""}`;
  } catch (e) {
    btn.textContent = "Export failed: " + e.message;
  } finally {
    setTimeout(() => { btn.disabled = false; btn.textContent = orig; }, 5000);
  }
}
const TX_CSV_HEADER = ["hash", "block", "timestamp", "from", "to", "value_native", "fee_native", "gas_price_gwei", "status"];
function txCsvRow(t) {
  return [t.hash, t.block ?? "", t.timestamp ?? "", (t.from && t.from.hash) || "", (t.to && t.to.hash) || "", t.value_eth ?? "", t.fee_eth ?? "", t.gas_price_gwei ?? "", t.status ?? ""];
}
const TT_CSV_HEADER = ["tx_hash", "timestamp", "from", "to", "token_address", "token_symbol", "amount"];
function ttCsvRow(tt) {
  return [tt.tx_hash, tt.timestamp ?? "", (tt.from && tt.from.hash) || "", (tt.to && tt.to.hash) || "", (tt.token && tt.token.address) || "", (tt.token && tt.token.symbol) || "", tt.amount ?? ""];
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
  if (!form || !input || !suggestBox) return;
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
    <div class="stat"><div class="k">${esc(nativeSym())} Price</div><div class="v">$${num(s.eth_price_usd)} ${chgHtml}</div></div>
    <div class="stat"><div class="k">Latest Block</div><div class="v">${linkBlock(s.latest_block)}</div></div>
    <div class="stat"><div class="k">Avg Block Time</div><div class="v">${esc(String(s.avg_block_time_s))}<small> sec</small></div></div>
    <div class="stat"><div class="k">Total Transactions</div><div class="v">${num(s.total_transactions)}</div></div>
    <div class="stat"><div class="k">Gas Price</div><div class="v">${esc(String(gp.average ?? "—"))}<small> Gwei</small></div><div class="sub">Slow ${esc(String(gp.slow ?? "—"))} · Fast ${esc(String(gp.fast ?? "—"))}</div></div>
    <div class="stat"><div class="k">Network Utilization</div><div class="v">${esc(String(s.network_utilization_pct))}<small>%</small></div></div>`;
}

async function home() {
  clearPoll();
  const myHash = location.hash || "#/";
  document.title = `EthScope — ${chainLabel()} Block Explorer`;
  render(`
    <div class="hero">
      <h1>${esc(chainLabel())} Explorer</h1>
      <p>Blocks, transactions, addresses and tokens on ${esc(chainLabel())} — powered by the Blockscout API.</p>
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
    } catch (e) { banner("Live update failed on " + chainLabel() + ": " + e.message); }
  }
  await refresh();
  if ((location.hash || "#/") === myHash) pollTimer = setInterval(refresh, 10000);
}

/* ---------------- block page ---------------- */
async function blockPage(n) {
  clearPoll();
  render(spinner());
  let b;
  try { b = await get("/block/" + encodeURIComponent(n)); }
  catch (e) { render(`<div class="pagehead"><h2>Block</h2></div><div class="card"><div class="empty">Block not found on ${esc(chainLabel())}: ${esc(n)}<br><span class="muted">${esc(e.message)}</span></div></div>`); return; }
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
      ${detailRow("Burnt Fees", `<span class="mono">${ethFmt(b.burnt_fees_eth, 8)} ${esc(nativeSym())}</span>`)}
      ${detailRow("Block Rewards", (b.rewards || []).map((r) => `${ethFmt(r.value_eth, 8)} ${esc(nativeSym())} → ${linkAddr({ hash: r.recipient, label: r.recipient, short: shortH(r.recipient) })}`).join("<br>") || '<span class="muted">—</span>')}
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
    render(`<div class="pagehead"><h2>Transaction</h2></div><div class="card"><div class="empty">Transaction not found on ${esc(chainLabel())}<br><span class="mono muted">${esc(h)}</span><br><span class="muted">${esc(e.message)}</span></div></div>`);
    return;
  }
  document.title = `Tx ${shortH(t.hash)} — EthScope`;
  const inputShown = t.input && t.input.length > 2000 ? t.input.slice(0, 2000) + "…" : (t.input || "0x");
  const toCell = t.to
    ? `${linkAddr(t.to, true)} ${copyBtn(t.to.hash)}${t.to.is_contract ? ` <span class="muted">·</span> ${linkContract(t.to.hash)}` : ""}`
    : (t.created_contract ? `Contract created: <a class="mono" href="#/address/${esc(t.created_contract)}">${esc(shortH(t.created_contract))}</a> · ${linkContract(t.created_contract)}` : '<span class="muted">—</span>');
  render(`
    <div class="pagehead"><h2>Transaction Details</h2> ${statusBadge(t.status)}</div>
    <div class="card">
      ${detailRow("Transaction Hash", `<span class="mono">${esc(t.hash)}</span> ${copyBtn(t.hash)}`)}
      ${detailRow("Status", statusBadge(t.status))}
      ${detailRow("Block", t.block != null ? `${linkBlock(t.block)} <span class="muted">(${num(t.confirmations)} confirmations)</span>` : '<span class="badge info">Pending</span>')}
      ${detailRow("Timestamp", `${esc(t.age || "")} <span class="muted">(${esc(utc(t.timestamp))})</span>`)}
      ${detailRow("From", `${linkAddr(t.from, true)} ${copyBtn(t.from ? t.from.hash : "")}`)}
      ${detailRow("To", toCell)}
      ${detailRow("Value", `<b class="mono">${ethFmt(t.value_eth)} ${esc(nativeSym())}</b>`)}
      ${detailRow("Transaction Fee", `<span class="mono">${ethFmt(t.fee_eth, 8)} ${esc(nativeSym())}</span>`)}
      ${detailRow("Gas Price", `<span class="mono">${esc(t.gas_price_gwei)} Gwei</span>`)}
      ${detailRow("Gas Limit / Used", `<span class="mono">${num(t.gas_limit)} / ${num(t.gas_used)}</span>`)}
      ${t.max_fee_per_gas_gwei && t.max_fee_per_gas_gwei !== "0.00" ? detailRow("Max Fee / Priority", `<span class="mono">${esc(t.max_fee_per_gas_gwei)} / ${esc(t.max_priority_fee_gwei)} Gwei</span>`) : ""}
      ${detailRow("Burnt Fee", `<span class="mono">${ethFmt(t.burnt_fee_eth, 8)} ${esc(nativeSym())}</span>`)}
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
  try {
    // txcount is best-effort (public RPC): a failure must not block the page.
    const [addrRes, tcRes] = await Promise.all([
      get("/address/" + h),
      get("/address/" + h + "/txcount").catch(() => null),
    ]);
    a = addrRes;
    a._txcount = tcRes;
  }
  catch (e) { render(`<div class="pagehead"><h2>Address</h2></div><div class="card"><div class="empty">Address not found on ${esc(chainLabel())}<br><span class="mono muted">${esc(h)}</span><br><span class="muted">${esc(e.message)}</span></div></div>`); return; }
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
  const tc = a._txcount;
  render(`
    <div class="pagehead"><h2>Address</h2></div>
    <div class="card">
      <div style="padding:14px 16px;border-bottom:1px solid var(--line)">
        <div class="addr-title"><span class="mono" style="font-size:15px">${esc(a.hash)}</span> ${copyBtn(a.hash)}${labelPill(a.hash)}</div>
        <div style="margin-top:8px">${badges}</div>
      </div>
      ${detailRow(nativeSym() + " Balance", `<b class="mono">${ethFmt(a.balance_eth)} ${esc(nativeSym())}</b>`)}
      ${detailRow("Transactions", num(a.transactions_count))}
      ${tc && tc.tx_count != null ? detailRow("Nonce / Outgoing Tx Count", `<span class="mono">${num(tc.tx_count)}</span> <span class="muted">(${esc(tc.note || "nonce / outgoing-transaction count for EOAs")})</span>`) : ""}
      ${a.is_contract ? detailRow("Contract", linkContract(a.hash)) : ""}
      ${a.name ? detailRow("Name", esc(a.name)) : ""}
      ${a.creator ? detailRow("Creator", `<a class="mono" href="#/address/${esc(a.creator)}">${esc(shortH(a.creator))}</a>${labelPill(a.creator)}`) : ""}
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
    body.innerHTML = `<div class="toolbar"><span class="muted">All transactions involving this address.</span><span class="spacer"></span><button class="btn" id="csv-txs">Export CSV</button></div>
      <table class="tbl"><thead>${txHead(false)}</thead><tbody id="atx"></tbody></table><button class="loadmore" id="atx-more">Load more</button>`;
    mountPager("#atx", "#atx-more", `/address/${h}/txs`, (t) => txRow(t, false), "No transactions");
    $("#csv-txs").onclick = (e) => exportCsv(e.currentTarget, `/address/${h}/txs`, `ethscope-address-${h}-transactions.csv`, TX_CSV_HEADER, txCsvRow);
  } else if (addrTab === "token-transfers") {
    body.innerHTML = `<div class="toolbar"><span class="muted">All token transfers involving this address.</span><span class="spacer"></span><button class="btn" id="csv-tt">Export CSV</button></div>
      <table class="tbl"><thead><tr><th>Txn Hash</th><th>Age</th><th>From</th><th>To</th><th class="r">Amount</th><th>Token</th></tr></thead><tbody id="att"></tbody></table><button class="loadmore" id="att-more">Load more</button>`;
    mountPager("#att", "#att-more", `/address/${h}/token-transfers`, ttRow, "No token transfers");
    $("#csv-tt").onclick = (e) => exportCsv(e.currentTarget, `/address/${h}/token-transfers`, `ethscope-address-${h}-token-transfers.csv`, TT_CSV_HEADER, ttCsvRow);
  } else if (addrTab === "internal") {
    body.innerHTML = `<table class="tbl"><thead><tr><th>Parent Txn</th><th>Age</th><th>From</th><th>To</th><th class="r">Value</th><th>Status</th></tr></thead><tbody id="ain"></tbody></table><button class="loadmore" id="ain-more">Load more</button>`;
    mountPager("#ain", "#ain-more", `/address/${h}/internal`, internalRow, "No internal transactions");
  } else if (addrTab === "tokens") {
    body.innerHTML = `<table class="tbl"><thead><tr><th>Token</th><th class="r">Balance</th><th class="r">USD Price</th><th class="r">USD Value</th><th>Type</th></tr></thead><tbody id="atok"><tr><td colspan="5"><div class="spinner">Loading…</div></td></tr></tbody></table><div id="atok-total"></div>`;
    get(`/address/${h}/tokens`).then((d) => {
      const el = $("#atok");
      if (!el) return;
      el.innerHTML = d.items.length ? d.items.map((t) =>
        `<tr><td>${linkToken(t.address, t.symbol)} <span class="muted">${esc(t.name || "")}</span></td><td class="r mono">${esc(t.balance)}</td><td class="r mono">${t.usd_price != null ? usd(t.usd_price) : "—"}</td><td class="r mono">${t.usd_value != null ? usd(t.usd_value) : "—"}</td><td><span class="badge grey">${esc(t.type || "—")}</span></td></tr>`
      ).join("") : '<tr><td colspan="5"><div class="empty">No token balances</div></td></tr>';
      const tot = $("#atok-total");
      if (tot) tot.innerHTML = `<div class="total-row"><span>Portfolio total (USD)</span><span>${usd(d.total_usd)}</span></div><div class="muted" style="padding:8px 16px 12px;font-size:12px">USD values for ERC-20 holdings (Blockscout exchange rate, CoinGecko fallback). NFTs show no USD value.</div>`;
    }).catch((e) => { const el = $("#atok"); if (el) el.innerHTML = `<tr><td colspan="5"><div class="empty">Error: ${esc(e.message)}</div></td></tr>`; });
  }
}

/* ---------------- token page ---------------- */
async function tokenPage(h) {
  clearPoll();
  render(spinner());
  let t;
  try { t = await get("/token/" + h); }
  catch (e) { render(`<div class="pagehead"><h2>Token</h2></div><div class="card"><div class="empty">Token not found on ${esc(chainLabel())}<br><span class="mono muted">${esc(h)}</span><br><span class="muted">${esc(e.message)}</span></div></div>`); return; }
  document.title = `${t.symbol || "Token"} — EthScope`;
  const price = t.exchange_rate_usd ? `$${num(t.exchange_rate_usd)}` : "—";
  const isNft = t.type === "ERC-721" || t.type === "ERC-1155";
  render(`
    <div class="pagehead"><h2>Token <span class="badge info">${esc(t.type || "ERC-20")}</span></h2></div>
    <div class="card">
      <div style="padding:14px 16px;border-bottom:1px solid var(--line)">
        <div class="addr-title">
          ${t.icon_url ? `<img src="${esc(t.icon_url)}" alt="" width="28" height="28" style="border-radius:50%">` : ""}
          <b style="font-size:17px">${esc(t.name || "—")} (${esc(t.symbol || "—")})</b>${labelPill(t.address)}
        </div>
        <div class="mono" style="margin-top:6px"><a href="#/address/${esc(t.address)}">${esc(t.address)}</a> ${copyBtn(t.address)} · ${linkContract(t.address)}</div>
      </div>
      ${detailRow("Total Supply", `<span class="mono">${num(t.total_supply)} ${esc(t.symbol || "")}</span>`)}
      ${detailRow("Decimals", `<span class="mono">${esc(String(t.decimals ?? "—"))}</span>`)}
      ${detailRow("Holders", num(t.holders))}
      ${detailRow("Price", `<span class="mono">${price}</span>`)}
      ${t.circulating_supply ? detailRow("Circulating Supply", `<span class="mono">${num(t.circulating_supply)} ${esc(t.symbol || "")}</span>`) : ""}
      ${t.volume_24h_usd ? detailRow("Volume (24h)", `<span class="mono">$${num(t.volume_24h_usd)}</span>`) : ""}
    </div>
    ${isNft ? `<div class="card"><div class="card-head">Instances <span class="spacer"></span><span class="badge grey" id="nft-count">…</span></div><div id="nft-grid"><div class="spinner">Loading…</div></div></div>` : ""}
    <div class="card">
      <div class="card-head">Transfers</div>
      <table class="tbl"><thead><tr><th>Txn Hash</th><th>Age</th><th>From</th><th>To</th><th class="r">Amount</th><th>Token</th></tr></thead><tbody id="ttk"></tbody></table>
      <button class="loadmore" id="ttk-more">Load more</button>
    </div>`);
  mountPager("#ttk", "#ttk-more", `/token/${h}/transfers`, ttRow, "No transfers");
  if (isNft) {
    try {
      const d = await get(`/token/${h}/instances`);
      const grid = $("#nft-grid");
      if (!grid) return;
      $("#nft-count").textContent = d.items.length + (d.next_page_params ? "+" : "");
      grid.innerHTML = d.items.length ? `<div class="nftgrid">${d.items.map((inst) => {
        const img = ipfsHttp(inst.image_url || inst.media_url);
        return `<a class="nft-card" href="#/token/${esc(h)}/instance/${esc(String(inst.id))}">
          ${img ? `<img src="${esc(img)}" alt="Token #${esc(String(inst.id))}" loading="lazy" onerror="this.style.display='none'">` : ""}
          <div class="nft-id">#${esc(String(inst.id))}</div>
          ${inst.owner && inst.owner.label && inst.owner.label !== inst.owner.hash ? `<div class="nft-sub">${esc(inst.owner.label)}</div>` : ""}
        </a>`;
      }).join("")}</div>${d.next_page_params ? '<div class="empty">Showing the first 50 instances.</div>' : ""}`
        : '<div class="empty">No instances found for this collection</div>';
    } catch (e) {
      const grid = $("#nft-grid");
      if (grid) grid.innerHTML = `<div class="empty">Instances unavailable: ${esc(e.message)}</div>`;
    }
  }
}

/* ---------------- NFT instance page ---------------- */
async function tokenInstancePage(h, id) {
  clearPoll();
  render(spinner());
  let inst;
  try { inst = await get(`/token/${h}/instance/${encodeURIComponent(id)}`); }
  catch (e) { render(`<div class="pagehead"><h2>NFT Instance</h2></div><div class="card"><div class="empty">Instance not found on ${esc(chainLabel())}<br><span class="mono muted">${esc(h)} #${esc(id)}</span><br><span class="muted">${esc(e.message)}</span></div></div>`); return; }
  const meta = inst.metadata || {};
  const attrs = inst.attributes || meta.attributes || [];
  const name = meta.name || `${(inst.token && inst.token.name) || "Token"} #${inst.id}`;
  const img = ipfsHttp(inst.image_url || inst.media_url);
  document.title = `${name} — EthScope`;
  render(`
    <div class="pagehead"><h2>${esc(name)}</h2> <span class="badge info">${esc((inst.token && inst.token.type) || "NFT")}</span></div>
    <div class="card">
      <div class="nft-hero">
        <div>
          ${img ? `<img src="${esc(img)}" alt="${esc(name)}" onerror="this.style.display='none'">` : '<div class="empty">No image</div>'}
          ${inst.animation_url ? `<div style="margin-top:8px"><a href="${esc(ipfsHttp(inst.animation_url))}" target="_blank" rel="noopener">▶ View animation / media</a></div>` : ""}
          ${inst.external_app_url ? `<div style="margin-top:4px"><a href="${esc(inst.external_app_url)}" target="_blank" rel="noopener">External link ↗</a></div>` : ""}
        </div>
        <div>
          ${detailRow("Collection", `${linkToken(h, (inst.token && inst.token.symbol) || shortH(h))} <span class="muted">${esc((inst.token && inst.token.name) || "")}</span>`)}
          ${detailRow("Token ID", `<span class="mono">${esc(String(inst.id))}</span>`)}
          ${detailRow("Owner", inst.owner ? linkAddr(inst.owner, true) : '<span class="muted">—</span>')}
          ${meta.description ? detailRow("Description", esc(meta.description)) : ""}
          ${detailRow("Unique", inst.is_unique == null ? "—" : String(inst.is_unique))}
        </div>
      </div>
    </div>
    ${attrs.length ? `<div class="card"><div class="card-head">Attributes</div><table class="tbl"><thead><tr><th>Trait</th><th>Value</th></tr></thead><tbody>${attrs.map((a) => `<tr><td>${esc(a.trait_type || a.traitType || "")}</td><td>${esc(String(a.value ?? ""))}</td></tr>`).join("")}</tbody></table></div>` : ""}
    <div class="card">
      <div class="card-head">Transfers of this instance</div>
      <table class="tbl"><thead><tr><th>Txn Hash</th><th>Age</th><th>From</th><th>To</th><th class="r">Amount</th><th>Token</th></tr></thead><tbody id="itx"></tbody></table>
      <button class="loadmore" id="itx-more">Load more</button>
    </div>`);
  mountPager("#itx", "#itx-more", `/token/${h}/instance/${encodeURIComponent(id)}/transfers`, ttRow, "No transfers for this instance");
}

/* ---------------- contract page ---------------- */
let walletAccount = null;
function fnMutability(fn) {
  if (fn.stateMutability) return fn.stateMutability;
  if (fn.constant) return "view";
  return fn.payable ? "payable" : "nonpayable";
}
function fnSignature(fn) {
  const ins = (fn.inputs || []).map((i) => i.type).join(", ");
  const outs = (fn.outputs || []).map((o) => o.type).join(", ");
  return `${fn.name}(${ins})${outs ? " → (" + outs + ")" : ""}`;
}
function fnInputFields(fn, prefix) {
  return (fn.inputs || []).map((inp, j) => {
    const isArray = /\[.*\]$/.test(inp.type);
    const ph = inp.type === "bool" ? "true / false" : isArray ? 'JSON array, e.g. ["0x…", "0x…"]' : inp.type;
    return `<label class="field"><span>${esc(inp.name || "arg" + j)} <span class="muted mono">${esc(inp.type)}</span></span><input type="text" name="${prefix}${j}" placeholder="${esc(ph)}"></label>`;
  }).join("");
}
function renderCallOutputs(outEl, outputs, values) {
  if (!outputs || !outputs.length) {
    outEl.innerHTML = '<div class="muted">Call succeeded (no return values).</div>';
    return;
  }
  outEl.innerHTML = `<table><thead><tr><th>Name</th><th>Type</th><th>Value</th></tr></thead><tbody>${outputs.map((o, i) => {
    const v = values[i];
    let cell;
    if (o.type === "address" && typeof v === "string" && /^0x[0-9a-fA-F]{40}$/.test(v)) cell = linkAddr({ hash: v, label: v, short: shortH(v) });
    else if (v !== null && typeof v === "object") cell = `<span class="mono">${esc(JSON.stringify(v))}</span>`;
    else cell = `<span class="mono">${esc(String(v))}</span>`;
    return `<tr><td>${esc(o.name || "")}</td><td class="mono">${esc(o.type)}</td><td>${cell}</td></tr>`;
  }).join("")}</tbody></table>`;
}

async function contractPage(addr) {
  clearPoll();
  render(spinner());
  let c;
  try { c = await get("/contract/" + addr); }
  catch (e) { render(`<div class="pagehead"><h2>Contract</h2></div><div class="card"><div class="empty">Contract not available on ${esc(chainLabel())}<br><span class="mono muted">${esc(addr)}</span><br><span class="muted">${esc(e.message)}</span></div></div>`); return; }
  document.title = `${c.name || shortH(c.address)} — EthScope`;
  const abi = Array.isArray(c.abi) ? c.abi : [];
  const fns = abi.filter((f) => f.type === "function" && f.name);
  const readFns = fns.filter((f) => { const m = fnMutability(f); return m === "view" || m === "pure"; });
  const writeFns = fns.filter((f) => { const m = fnMutability(f); return m === "nonpayable" || m === "payable"; });
  const impls = (c.implementations || []).map((i) =>
    `<a class="mono" href="#/address/${esc(i.address)}">${esc(shortH(i.address))}</a>${i.name ? ` <span class="muted">(${esc(i.name)})</span>` : ""}`).join("<br>");
  render(`
    <div class="pagehead"><h2>Contract</h2> ${c.verified ? '<span class="badge ok">✓ Verified</span>' : '<span class="badge warn">Unverified</span>'}</div>
    <div class="card">
      <div style="padding:14px 16px;border-bottom:1px solid var(--line)">
        <div class="addr-title"><b style="font-size:17px">${esc(c.name || "Unknown contract")}</b>${labelPill(c.address)}</div>
        <div class="mono" style="margin-top:6px"><a href="#/address/${esc(c.address)}">${esc(c.address)}</a> ${copyBtn(c.address)}</div>
      </div>
      ${detailRow("Verified", c.verified ? `<span class="badge ok">Verified</span> <span class="muted">${c.verified_at ? "on " + esc(String(c.verified_at).slice(0, 10)) : ""}</span>` : '<span class="badge warn">Source not verified</span>')}
      ${detailRow("Compiler", `<span class="mono">${esc(c.compiler_version || "—")}</span>`)}
      ${detailRow("Language", esc(c.language || "—"))}
      ${detailRow("EVM Version", `<span class="mono">${esc(c.evm_version || "—")}</span>`)}
      ${detailRow("License", esc(c.license_type || "—"))}
      ${c.optimization_enabled != null ? detailRow("Optimization", esc(String(c.optimization_enabled))) : ""}
      ${c.proxy_type ? detailRow("Proxy", `<span class="mono">${esc(c.proxy_type)}</span>`) : ""}
      ${impls ? detailRow("Implementation", impls) : ""}
      ${c.creation_tx ? detailRow("Creation Txn", linkTx(c.creation_tx)) : ""}
      ${detailRow("Address Page", `<a href="#/address/${esc(c.address)}">View address →</a>`)}
    </div>
    <div class="card">
      <div class="tabs">
        <button class="tab active" data-ctab="source">Source Code</button>
        <button class="tab" data-ctab="read">Read Contract (${readFns.length})</button>
        <button class="tab" data-ctab="write">Write Contract (${writeFns.length})</button>
        <button class="tab" data-ctab="verify">Verify Contract</button>
      </div>
      <div id="contract-tab-body"></div>
    </div>`);

  const body = $("#contract-tab-body");
  function renderCTab(tab) {
    if (tab === "source") {
      const extra = (c.additional_sources || []).map((s) =>
        `<div style="margin:12px 16px 0"><b>${esc(s.filename || s.name || "additional source")}</b><pre class="source" style="margin:8px 0 0">${esc(s.source_code || s.sourceCode || "")}</pre></div>`).join("");
      body.innerHTML = c.source_code
        ? `<pre class="source">${esc(c.source_code)}</pre>${extra}`
        : '<div class="empty">Source code is not available for this contract (unverified or not published).</div>';
    } else if (tab === "read") {
      if (!readFns.length) { body.innerHTML = '<div class="empty">No view/pure functions in this contract’s ABI.</div>'; return; }
      body.innerHTML = `<div class="muted" style="padding:12px 16px 0;font-size:12.5px">Read-only calls run against the ${esc(chainLabel())} public RPC (<span class="mono">eth_call</span>) — no wallet, no gas.</div>` +
        readFns.map((fn, i) => `<div class="fncard">
          <div class="fn-sig mono">${esc(fnSignature(fn))} <span class="badge info">${esc(fnMutability(fn))}</span></div>
          <form class="read-form" data-i="${i}">${fnInputFields(fn, "arg")}
            <button class="btn primary" type="submit">Query</button>
          </form>
          <div class="fn-out" id="read-out-${i}"></div>
        </div>`).join("");
      body.querySelectorAll(".read-form").forEach((f) => f.addEventListener("submit", async (e) => {
        e.preventDefault();
        const fn = readFns[+f.dataset.i];
        const out = $("#read-out-" + f.dataset.i);
        const btn = f.querySelector("button[type=submit]");
        btn.disabled = true;
        btn.textContent = "Querying…";
        try {
          const args = (fn.inputs || []).map((inp, j) => parseArg(inp.type, f.elements["arg" + j].value));
          const res = await postJson(`/contract/${addr}/call`, {
            function: { name: fn.name, inputs: fn.inputs || [], outputs: fn.outputs || [], stateMutability: fnMutability(fn) },
            args,
          });
          renderCallOutputs(out, res.outputs, res.values);
        } catch (err) { out.innerHTML = `<div class="error-banner" style="margin:0">Error: ${esc(err.message)}</div>`; }
        btn.disabled = false;
        btn.textContent = "Query";
      }));
    } else if (tab === "write") {
      if (!writeFns.length) { body.innerHTML = '<div class="empty">No nonpayable/payable functions in this contract’s ABI.</div>'; return; }
      const hasWallet = typeof window.ethereum !== "undefined";
      body.innerHTML = `
        <div class="walletbar">
          ${hasWallet
            ? (walletAccount
              ? `<span>Connected wallet: <span class="mono">${esc(walletAccount)}</span></span><button class="btn" id="wallet-disconnect">Disconnect</button>`
              : `<button class="btn primary" id="wallet-connect">Connect Wallet</button><span class="muted">MetaMask or another injected wallet is required to send transactions.</span>`)
            : `<span class="muted">No injected wallet detected in this browser.</span><button class="btn primary" id="wallet-connect">Connect Wallet</button>`}
          <span class="muted">Make sure your wallet is on ${esc(chainLabel())} before sending — EthScope does not switch networks for you.</span>
        </div>
        ${!hasWallet ? '<div class="notice">Write Contract is read-only here: no <span class="mono">window.ethereum</span> provider was found. Install MetaMask (or another injected wallet) and reload the page to send transactions. Read Contract above still works without a wallet.</div>' : ""}
        ${writeFns.map((fn, i) => `<div class="fncard">
          <div class="fn-sig mono">${esc(fnSignature(fn))} <span class="badge ${fnMutability(fn) === "payable" ? "warn" : "grey"}">${esc(fnMutability(fn))}</span></div>
          <form class="write-form" data-i="${i}">${fnInputFields(fn, "arg")}
            ${fnMutability(fn) === "payable" ? `<label class="field"><span>Value <span class="muted mono">${esc(nativeSym())} (optional)</span></span><input type="text" name="value" placeholder="0.0"></label>` : ""}
            <button class="btn primary" type="submit">Send Transaction</button>
          </form>
          <div class="fn-out" id="write-out-${i}"></div>
        </div>`).join("")}`;
      const connectBtn = $("#wallet-connect");
      if (connectBtn) connectBtn.onclick = async () => {
        if (!window.ethereum) {
          banner("No injected wallet found — install MetaMask (or another wallet that injects window.ethereum) and reload the page.");
          return;
        }
        try {
          const accounts = await window.ethereum.request({ method: "eth_requestAccounts" });
          walletAccount = accounts && accounts[0];
          if (walletAccount) renderCTab("write");
        } catch (err) { banner("Wallet connection failed: " + (err.message || err)); }
      };
      const discBtn = $("#wallet-disconnect");
      if (discBtn) discBtn.onclick = () => { walletAccount = null; renderCTab("write"); };
      body.querySelectorAll(".write-form").forEach((f) => f.addEventListener("submit", async (e) => {
        e.preventDefault();
        const fn = writeFns[+f.dataset.i];
        const out = $("#write-out-" + f.dataset.i);
        const btn = f.querySelector("button[type=submit]");
        if (!window.ethereum) { out.innerHTML = '<div class="error-banner" style="margin:0">No injected wallet found — install MetaMask to send transactions.</div>'; return; }
        if (!walletAccount) { out.innerHTML = '<div class="error-banner" style="margin:0">Connect your wallet first (button above).</div>'; return; }
        btn.disabled = true;
        btn.textContent = "Sending…";
        try {
          const args = (fn.inputs || []).map((inp, j) => parseArg(inp.type, f.elements["arg" + j].value));
          const data = encodeFunctionCall(fn, args);
          const tx = { from: walletAccount, to: addr, data };
          if (fnMutability(fn) === "payable") {
            const v = (f.elements["value"] && f.elements["value"].value || "").trim();
            if (v) tx.value = ethToWeiHex(v);
          }
          const txHash = await window.ethereum.request({ method: "eth_sendTransaction", params: [tx] });
          out.innerHTML = `<div class="utf8box"><div class="k">Transaction sent</div><div>Hash: ${linkTx(txHash, true)} — track it on the transaction page once it is mined.</div></div>`;
        } catch (err) { out.innerHTML = `<div class="error-banner" style="margin:0">Error: ${esc(err.message || String(err))}</div>`; }
        btn.disabled = false;
        btn.textContent = "Send Transaction";
      }));
    } else if (tab === "verify") {
      /* NOTE: the EthScope backend verify route takes JSON (it forwards
         multipart to Blockscout itself), so this form POSTs JSON. The
         upstream result is shown verbatim below — never faked as success. */
      body.innerHTML = `
        <div class="notice info">Submits source code to Blockscout for verification on ${esc(chainLabel())}. The upstream result is shown verbatim — a rejection is reported as a rejection, never as a success.</div>
        <form id="verify-form" style="padding:4px 16px 16px">
          <div style="display:flex;gap:10px;flex-wrap:wrap">
            <label class="field"><span>Compiler version *</span><input type="text" name="compiler_version" placeholder="v0.8.20+commit.a1b79de6" required></label>
            <label class="field"><span>Contract name *</span><input type="text" name="contract_name" placeholder="MyToken" required></label>
            <label class="field"><span>EVM version</span><input type="text" name="evm_version" placeholder="default"></label>
            <label class="field"><span>License type</span><input type="text" name="license_type" placeholder="none"></label>
          </div>
          <label class="field" style="margin-top:10px"><span>Constructor arguments (hex, optional)</span><input type="text" name="constructor_args" placeholder="0x…"></label>
          <label class="field" style="margin-top:10px"><span>Source code *</span><textarea name="source_code" placeholder="pragma solidity …" required></textarea></label>
          <div style="margin-top:10px"><button class="btn primary" type="submit">Submit for Verification</button></div>
        </form>
        <div id="verify-out" style="padding:0 16px 16px"></div>`;
      $("#verify-form").addEventListener("submit", async (e) => {
        e.preventDefault();
        const f = e.currentTarget;
        const out = $("#verify-out");
        const btn = f.querySelector("button[type=submit]");
        btn.disabled = true;
        btn.textContent = "Submitting…";
        out.innerHTML = "";
        try {
          const res = await postJson(`/contract/${addr}/verify`, {
            source_code: f.elements["source_code"].value,
            contract_name: f.elements["contract_name"].value,
            compiler_version: f.elements["compiler_version"].value,
            constructor_args: f.elements["constructor_args"].value || "",
            evm_version: f.elements["evm_version"].value || "",
            license_type: f.elements["license_type"].value || "",
          });
          out.innerHTML = `<div class="${res.ok ? "utf8box" : "error-banner"}" style="margin:0 0 10px">${res.ok ? "Upstream reports success." : "Upstream did NOT verify this contract."} ${res.message ? "Message: " + esc(String(res.message)) : ""}</div>
            <pre class="hex" style="max-height:320px">${esc(JSON.stringify(res, null, 2))}</pre>`;
        } catch (err) { out.innerHTML = `<div class="error-banner" style="margin:0">Error: ${esc(err.message)}</div>`; }
        btn.disabled = false;
        btn.textContent = "Submit for Verification";
      });
    }
  }
  document.querySelectorAll("[data-ctab]").forEach((t) => {
    t.onclick = () => {
      document.querySelectorAll("[data-ctab]").forEach((x) => x.classList.remove("active"));
      t.classList.add("active");
      renderCTab(t.dataset.ctab);
    };
  });
  renderCTab("source");
}

/* ---------------- charts page ---------------- */
let chartDays = 30;
async function chartsPage() {
  clearPoll();
  document.title = `Charts — EthScope`;
  render(`
    <div class="pagehead"><h2>Charts <span class="muted" style="font-size:14px">— ${esc(chainLabel())}</span></h2></div>
    <div class="card"><div class="card-head">Daily Transactions <span class="spacer"></span><span class="badge grey">Blockscout</span></div><div class="chart-wrap" id="chart-tx"><div class="spinner">Loading…</div></div></div>
    <div class="card"><div class="card-head">Native Price (${esc(nativeSym())}/USD) <span class="spacer"></span><span class="daybtns"><button class="btn" data-days="7">7D</button><button class="btn active" data-days="30">30D</button><button class="btn" data-days="90">90D</button></span></div><div class="chart-wrap" id="chart-price"><div class="spinner">Loading…</div></div><div class="muted" style="padding:0 16px 12px;font-size:12px">Price history: CoinGecko keyless API.</div></div>
    <div class="card"><div class="card-head">Base Fee History (latest ~50 blocks) <span class="spacer"></span><span class="badge grey">computed</span></div><div class="chart-wrap" id="chart-gas"><div class="spinner">Loading…</div></div><div class="muted" style="padding:0 16px 12px;font-size:12px">Blockscout has no gas chart on this instance — base fees are computed from the latest blocks’ <span class="mono">base_fee_per_gas</span>.</div></div>`);

  async function loadPrice(days) {
    const el = $("#chart-price");
    if (!el) return;
    el.innerHTML = '<div class="spinner">Loading…</div>';
    try {
      const d = await get(`/charts/price?days=${days}`);
      const pts = (d.prices || []).map(([ms, p]) => ({ x: new Date(ms).toLocaleDateString("en-US"), y: Number(p) }));
      el.innerHTML = svgChart(pts, { label: `${nativeSym()} price, ${days} days`, fmtY: (v) => "$" + Number(v).toLocaleString("en-US", { maximumFractionDigits: 2 }) });
    } catch (e) { el.innerHTML = `<div class="empty">Price history unavailable: ${esc(e.message)}</div>`; }
  }
  document.querySelectorAll("[data-days]").forEach((b) => {
    b.onclick = () => {
      document.querySelectorAll("[data-days]").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
      chartDays = +b.dataset.days;
      loadPrice(chartDays);
    };
  });
  const [txRes, gasRes] = await Promise.allSettled([get("/charts/transactions"), get("/charts/gas")]);
  const txEl = $("#chart-tx"), gasEl = $("#chart-gas");
  if (txEl) {
    if (txRes.status === "fulfilled") {
      const pts = (txRes.value.chart_data || [])
        .map((p) => ({ x: p.date, y: Number(p.transactions_count) }))
        .filter((p) => Number.isFinite(p.y))
        .sort((a, b) => (a.x < b.x ? -1 : 1));
      txEl.innerHTML = svgChart(pts, { label: "Daily transactions", fmtY: (v) => Number(v).toLocaleString("en-US") });
    } else txEl.innerHTML = `<div class="empty">Transaction history unavailable: ${esc(txRes.reason.message)}</div>`;
  }
  if (gasEl) {
    if (gasRes.status === "fulfilled") {
      const pts = (gasRes.value.items || [])
        .map((p) => ({ x: "Block " + p.block, y: Number(p.base_fee_gwei) }))
        .filter((p) => Number.isFinite(p.y))
        .sort((a, b) => parseInt(a.x.slice(6), 10) - parseInt(b.x.slice(6), 10));
      gasEl.innerHTML = svgChart(pts, { label: "Base fee history", fmtY: (v) => Number(v).toFixed(2) + " Gwei" });
    } else gasEl.innerHTML = `<div class="empty">Base-fee history unavailable: ${esc(gasRes.reason.message)}</div>`;
  }
  await loadPrice(chartDays);
}

/* ---------------- gas tracker page ---------------- */
async function gasPage() {
  clearPoll();
  document.title = `Gas Tracker — EthScope`;
  render(`
    <div class="pagehead"><h2>Gas Tracker <span class="muted" style="font-size:14px">— ${esc(chainLabel())}</span></h2></div>
    <div id="gas-body"><div class="spinner">Loading…</div></div>`);
  let g;
  try { g = await get("/gastracker"); }
  catch (e) { $("#gas-body").innerHTML = `<div class="card"><div class="empty">Gas tracker unavailable on ${esc(chainLabel())}: ${esc(e.message)}</div></div>`; return; }
  const gp = g.gas_price_gwei || {};
  const hist = (g.base_fee_history || [])
    .map((p) => ({ x: "Block " + p.block, y: Number(p.base_fee_gwei), block: p.block }))
    .filter((p) => Number.isFinite(p.y))
    .sort((a, b) => a.block - b.block);
  $("#gas-body").innerHTML = `
    <div class="gascards">
      <div class="gascard"><div class="k">🐢 Slow</div><div class="v">${esc(String(gp.slow ?? "—"))} <small>Gwei</small></div><div class="sub">Cheapest — may take a while</div></div>
      <div class="gascard"><div class="k">🚗 Average</div><div class="v">${esc(String(gp.average ?? "—"))} <small>Gwei</small></div><div class="sub">Typical confirmation time</div></div>
      <div class="gascard"><div class="k">🚀 Fast</div><div class="v">${esc(String(gp.fast ?? "—"))} <small>Gwei</small></div><div class="sub">Next-block priority</div></div>
      <div class="gascard"><div class="k">Base Fee (latest)</div><div class="v">${esc(String(g.base_fee_gwei ?? "—"))} <small>Gwei</small></div><div class="sub">Avg block time ${esc(String(g.avg_block_time_s ?? "—"))}s</div></div>
    </div>
    <div class="explainer"><b>How gas works here:</b> since EIP-1559 every block has a <i>base fee</i> (burned, set by the protocol — it rises when blocks are full and falls when they are empty). You pay the base fee plus a small priority tip to the block builder. The slow / average / fast prices above come from Blockscout’s gas-price feed; the chart below is the base fee of the latest 20 blocks.</div>
    <div class="card"><div class="card-head">Base Fee — Latest 20 Blocks</div><div class="chart-wrap">${svgChart(hist, { label: "Base fee, latest 20 blocks", fmtY: (v) => Number(v).toFixed(2) + " Gwei" })}</div></div>
    ${g.note ? `<div class="muted" style="font-size:12.5px">${esc(g.note)}</div>` : ""}`;
}

/* ---------------- pending page ---------------- */
async function pendingPage() {
  clearPoll();
  const myHash = location.hash || "#/";
  document.title = `Pending Transactions — EthScope`;
  render(`
    <div class="pagehead"><h2>Pending Transactions</h2> <span class="sample-badge">Sampled — one pending block via public RPC, not the full mempool</span></div>
    <div class="card">
      <div class="card-head">Pending <span class="spacer"></span><span class="badge grey" id="pend-count">…</span><span class="muted" style="font-size:12px" id="pend-updated"></span></div>
      <table class="tbl"><thead><tr><th>Txn Hash</th><th>From</th><th>To</th><th class="r">Value (${esc(nativeSym())})</th><th class="r">Gas Price</th></tr></thead><tbody id="pend-body"><tr><td colspan="5"><div class="spinner">Loading…</div></td></tr></tbody></table>
    </div>
    <div class="muted" style="font-size:12.5px">Blockscout has no pending-transaction list, so this page samples a single pending block from the chain’s public RPC. It is a snapshot, not the mempool — it can be empty when the RPC returns no pending transactions. Auto-refreshes every 15s.</div>`);
  async function refresh() {
    try {
      const d = await get("/pending");
      const body = $("#pend-body"), count = $("#pend-count"), upd = $("#pend-updated");
      if (!body) return;
      if (count) count.textContent = String(d.items.length);
      if (upd) upd.textContent = "updated " + new Date().toLocaleTimeString();
      body.innerHTML = d.items.length ? d.items.slice(0, 50).map((t) =>
        `<tr><td>${linkTx(t.hash)}</td><td>${linkAddr(t.from)}</td><td>${linkAddr(t.to)}</td><td class="r mono">${ethFmt(t.value_eth)}</td><td class="r mono">${esc(t.gas_price_gwei)} Gwei</td></tr>`).join("")
        : '<tr><td colspan="5"><div class="empty">No pending transactions in the sampled block right now.</div></td></tr>';
    } catch (e) {
      const body = $("#pend-body");
      if (body) body.innerHTML = `<tr><td colspan="5"><div class="empty">Pending transactions unavailable on ${esc(chainLabel())}: ${esc(e.message)}</div></td></tr>`;
    }
  }
  await refresh();
  if ((location.hash || "#/") === myHash) pollTimer = setInterval(refresh, 15000);
}

/* ---------------- API docs page ---------------- */
function docsGroup(path) {
  const p = path.replace("/api/ethscope/{chain}", "").replace("/api/ethscope", "");
  if (p === "/chains" || p === "/docs") return "Meta";
  if (p === "/stats" || p === "/gastracker") return "Chain & Gas";
  if (p.startsWith("/block")) return "Blocks";
  if (p.startsWith("/tx")) return "Transactions";
  if (p.startsWith("/address")) return "Addresses";
  if (p.startsWith("/token")) return "Tokens";
  if (p.startsWith("/contract")) return "Contracts";
  if (p.startsWith("/charts")) return "Charts";
  if (p === "/pending") return "Pending";
  if (p === "/search") return "Search";
  return "Other";
}
async function apiDocsPage() {
  clearPoll();
  document.title = `API Docs — EthScope`;
  render(`<div class="pagehead"><h2>API Documentation</h2></div><div id="docs-body"><div class="spinner">Loading…</div></div>`);
  let d;
  try { d = await getGlobal("/docs"); }
  catch (e) { $("#docs-body").innerHTML = `<div class="card"><div class="empty">API docs unavailable: ${esc(e.message)}</div></div>`; return; }
  // Deduplicate legacy vs chain-prefixed entries; show the chain-prefixed path.
  const byKey = new Map();
  for (const e of d.endpoints || []) {
    const suffix = e.path.replace("/api/ethscope/{chain}", "").replace("/api/ethscope", "");
    const key = e.method + " " + suffix;
    if (!byKey.has(key) || e.path.includes("{chain}")) byKey.set(key, e);
  }
  const groups = {};
  for (const e of byKey.values()) {
    const g = docsGroup(e.path);
    (groups[g] = groups[g] || []).push(e);
  }
  const order = ["Chain & Gas", "Blocks", "Transactions", "Addresses", "Tokens", "Contracts", "Charts", "Pending", "Search", "Meta", "Other"];
  const shown = order.filter((g) => groups[g]).concat(Object.keys(groups).filter((g) => !order.includes(g)));
  $("#docs-body").innerHTML = `
    <div class="explainer"><b>Fair use:</b> this API is keyless and needs no auth — it proxies the public Blockscout API, public RPCs, and CoinGecko’s keyless tier, all of which are rate-limited upstream. Please cache responses, keep request rates modest, and expect occasional 502s when an upstream is slow. Nothing here is authenticated or billed.</div>
    ${shown.map((g) => `<div class="docgroup"><h3>${esc(g)}</h3>${groups[g].map((e) => `
      <div class="docitem">
        <span class="method ${e.method.toLowerCase()}">${esc(e.method)}</span><span class="dpath">${esc(e.path)}</span>
        <div class="ddesc">${esc(e.description)}</div>
        <div class="dmeta">${e.params && e.params.length ? `Params: ${e.params.map((p) => `<code>${esc(p)}</code>`).join(", ")}<br>` : ""}Example: <code>${esc(e.example)}</code></div>
      </div>`).join("")}</div>`).join("")}`;
}

/* ---------------- router ---------------- */
const routes = [
  [/^#\/?$/, () => home()],
  [/^#\/charts$/, () => chartsPage()],
  [/^#\/gastracker$/, () => gasPage()],
  [/^#\/pending$/, () => pendingPage()],
  [/^#\/api-docs$/, () => apiDocsPage()],
  [/^#\/block\/([^/]+)$/, (m) => blockPage(m[1])],
  [/^#\/tx\/(0x[0-9a-fA-F]{64})$/, (m) => txPage(m[1])],
  [/^#\/address\/(0x[0-9a-fA-F]{40})$/, (m) => addressPage(m[1])],
  [/^#\/token\/(0x[0-9a-fA-F]{40})\/instance\/([^/]+)$/, (m) => tokenInstancePage(m[1], decodeURIComponent(m[2]))],
  [/^#\/token\/(0x[0-9a-fA-F]{40})$/, (m) => tokenPage(m[1])],
  [/^#\/contract\/(0x[0-9a-fA-F]{40})$/, (m) => contractPage(m[1])],
];
function route() {
  banner(null);
  clearPoll();
  const h = location.hash || "#/";
  for (const [re, fn] of routes) {
    const m = h.match(re);
    if (m) { fn(m); return; }
  }
  document.title = "Not found — EthScope";
  render(`<div class="card" style="margin-top:22px"><div class="empty">Page not found: <span class="mono">${esc(h)}</span><br><a href="#/">Back to home</a></div></div>`);
}

document.addEventListener("DOMContentLoaded", async () => {
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
  await loadChains();
  await loadLabels();
  route();
});
