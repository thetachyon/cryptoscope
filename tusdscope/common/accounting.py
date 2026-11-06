"""Accounting export (phase 2, Q2) — finance-grade CSV / JSON export
for one wallet address, with USD value at time of transaction and FIFO
cost basis per coin.

Endpoint: GET /api/walletscope/accounting/export?address=&format=csv
(format=json returns the same rows plus summary, FIFO lots and notes).
Wired in common/factory.py (create_wallet_app); the wallet dashboard
links to it from the Accounting Export card.

Chains / coins (Tejas's directive: ALL coins, wherever it applies):
Ethereum / Sepolia / Optimism / Gnosis (native + ERC-20, Blockscout),
Solana (SOL + SPL token balance changes, public RPC), Bitcoin
(mempool.space / Esplora, BTC addresses only). Every coin the wallet
aggregates, not just stablecoins.

Sources (all keyless, zero new dependencies):
- EVM rows: Blockscout /addresses/{h}/transactions + /token-transfers
  (first page each per chain — the same sampled window as the PnL
  estimate), reused via common/wallet.py ADAPTER.
- Solana rows: getSignaturesForAddress (first 10) + getTransaction per
  signature (common/solana_explorer.sol_tx) — SOL balance change and
  SPL token balance changes for the address.
- Bitcoin rows: mempool.space /address/{addr}/txs (first page, 25 txs),
  raw tx objects (common/bitcoin._api) — net flow per tx, since change
  outputs cannot be identified from chain data alone.
- USD at time: one CoinGecko daily-close series per coin, fetched ONCE
  for the whole export (cached 10 min by the pricing layer), then priced
  per coin-day from that series. Live price is the fallback and is
  labelled price_basis "estimate-current"; when no price exists at all
  the USD fields are null — never invented.
- FIFO cost basis: the shared engine in common/wallet.py (_fifo_run) —
  the same code as the PnL estimate. Every incoming transfer opens a lot
  at that day's price, every outgoing transfer disposes oldest-first.
  Native coins are FIFO'd too (the PnL estimate covers ERC-20 only).

Journal mapping (QuickBooks / Xero general-journal import — see
COLUMN_MAPPING, also rendered in the UI and in the JSON notes):
- receive: Debit "Crypto Assets:{COIN}", Credit "Other Income:Crypto
  Received", both legs = amount_usd.
- send: Debit "Cost of Goods Sold:Crypto Disposed", Credit
  "Crypto Assets:{COIN}", both legs = cost_basis_usd. The realized
  gain/loss (realized_gain_usd) is reported per send row for the
  bookkeeper to post separately — a single journal row cannot carry it.
- Rows with no USD price have empty debit/credit legs (nothing to post).

Honest limits (also carried in the JSON `notes` and the UI):
- Sampled window: first page per chain (50 native txs + 50 token
  transfers per EVM chain, 10 Solana signatures, 25 Bitcoin txs).
  Older history is NOT fetched — the export is complete only for
  addresses whose whole history fits the window.
- Token-transfer rows carry no fee: the fee is paid in the native coin
  on the parent transaction and is not returned by Blockscout's
  token-transfer endpoint. Native rows carry fee_usd.
- "estimate-current" rows are priced at today's live price, not at
  transaction time — for old transactions that is a weak estimate, and
  their FIFO lots inherit the same weakness (flagged per row).
- Sepolia ETH has no market value: its USD fields are always null.
- Bitcoin rows are NET flow per transaction (received minus sent);
  the counterparty is the first other address seen. Solana
  counterparties are best-effort (largest opposite balance change).
- This is an estimate, not tax advice: transfers to exchanges, gifts
  and internal moves are all treated as disposals.
"""
import asyncio
import csv
import io
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from common.bitcoin import _api as _btc_api, btc_price, is_bitcoin_address
from common.chains import CHAINS, WALLET_CHAINS
from common.clients import _cache_get, _cache_set, coingecko
from common.formatting import token_amount, wei_to_eth
from common.pricing import (
    _cg_contract_daily_prices,
    _daily_native_prices,
    _daily_series,
    cg_token_price,
    native_quote,
)
from common.settlement import _days_since, _price_on_day, _usd
from common.solana import _cg_contract_symbol, is_solana_address, sol_native_quote
from common.solana_explorer import _signatures as _sol_signatures, sol_tx
from common.wallet import (
    ADAPTER,
    _EVM_ADDR_RE,
    _fifo_run,
    _price_on_or_before,
)

EXPORT_CACHE_TTL = 300  # seconds, like the PnL estimate
EVM_TX_LIMIT_NOTE = "first page per chain (50 native txs + 50 token transfers)"
SOL_SIG_LIMIT = 10
BTC_TX_LIMIT = 25

CSV_COLUMNS = [
    "date",
    "tx_hash",
    "chain",
    "coin",
    "token_address",
    "type",
    "counterparty",
    "amount",
    "usd_price",
    "amount_usd",
    "price_basis",
    "fee_usd",
    "fee_coin",
    "cost_basis_usd",
    "realized_gain_usd",
    "fifo_lots",
    "debit_account",
    "credit_account",
    "debit_usd",
    "credit_usd",
    "memo",
]

COLUMN_MAPPING = {
    "date": "Transaction date (UTC, ISO 8601). Maps to the journal date.",
    "tx_hash": "Transaction hash / signature / txid. Included in memo; not a journal field.",
    "chain": "Chain the transaction settled on (ethereum, sepolia, optimism, gnosis, solana, bitcoin).",
    "coin": "Coin / token symbol (SPL:<mint> when the SPL symbol is unknown).",
    "token_address": "Token contract (EVM) or mint (Solana); empty for native coins.",
    "type": "receive or send, from the address's point of view.",
    "counterparty": "The other party's address (first other address for Bitcoin; best-effort for Solana).",
    "amount": "Amount of coin received / sent, in human units.",
    "usd_price": "USD price of one coin on the transaction date (daily close), or the live price when price_basis is estimate-current.",
    "amount_usd": "amount x usd_price, rounded to cents. Null when no price exists.",
    "price_basis": "at-time (daily close on the tx date), estimate-current (live price fallback — NOT the price at the time), or unavailable (no price at all).",
    "fee_usd": "Transaction fee in USD at the tx date. Null on token-transfer rows: the fee is paid in the native coin on the parent transaction and Blockscout's token-transfer endpoint does not return it.",
    "fee_coin": "Coin the fee was paid in (native coin of the chain), or empty when fee_usd is null.",
    "cost_basis_usd": "FIFO cost of the disposed amount (sends only). Null when no matching acquisition exists in the sampled window (unmatched) or the coin is unpriced.",
    "realized_gain_usd": "amount_usd (matched part only) minus cost_basis_usd (sends only). Post gains/losses separately — a single journal row cannot carry them.",
    "fifo_lots": "Lots consumed by a send, as 'amount @ unit_cost (acquired YYYY-MM-DD)'. Empty on receives (they open lots instead).",
    "debit_account": "QuickBooks/Xero general-journal debit account. Receive: Crypto Assets:{COIN}. Send: Cost of Goods Sold:Crypto Disposed.",
    "credit_account": "General-journal credit account. Receive: Other Income:Crypto Received. Send: Crypto Assets:{COIN}.",
    "debit_usd": "Journal debit leg (= amount_usd on receives, = cost_basis_usd on sends). Empty when the USD value is unknown.",
    "credit_usd": "Journal credit leg (same value as debit_usd).",
    "memo": "Human-readable memo including the tx hash, e.g. 'Receive 25.04 USDC on Ethereum — tx 0x…'.",
}

HONEST_NOTES = [
    "Sampled window: first page per chain (50 native txs + 50 token transfers per EVM chain, 10 Solana signatures, 25 Bitcoin txs). Older history is NOT fetched — the export is complete only for addresses whose whole history fits the window.",
    "Rows with price_basis 'estimate-current' are priced at today's live price, not at transaction time; for old transactions that is a weak estimate, and their FIFO lots inherit the same weakness.",
    "Rows with price_basis 'unavailable' have null USD fields — no price was invented. Unpriced tokens (e.g. unlisted / spam tokens) still appear with their amount, so nothing is silently dropped.",
    "Token-transfer rows carry no fee_usd: the fee is paid in the native coin on the parent transaction and Blockscout's token-transfer endpoint does not return it. Native rows carry fee_usd.",
    "Sepolia ETH has no market value — its USD fields are always null (never valued at mainnet ETH prices).",
    "Bitcoin rows are NET flow per transaction (received minus sent); change outputs cannot be identified from chain data alone. Counterparty is the first other address seen. Bitcoin fee_usd (sends only) is the whole transaction's fee — when several senders fund one transaction, the address's share is unknown.",
    "Solana counterparties are best-effort (largest opposite balance change in the same transaction).",
    "FIFO cost basis reuses the PnL estimate's engine (common/wallet.py _fifo_run): every incoming transfer opens a lot at that day's price, every outgoing transfer disposes oldest-first. Disposals with no matching acquisition in the window are unmatched — cost_basis_usd is null, never guessed.",
    "This is an estimate, not tax advice: transfers to exchanges, gifts and internal moves are all treated as disposals.",
]


# ---------------------------------------------------------------- row helpers
def _dec(s):
    try:
        return Decimal(str(s))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(0)


def _fmt_amount(d):
    """Decimal -> plain string, no scientific notation."""
    try:
        return format(d.normalize(), "f")
    except (InvalidOperation, ValueError):
        return "0"


def _q9(d):
    """Quantize a SOL amount to lamport precision (9dp). Solana RPC
    balance changes arrive as floats via _lamports, so unquantized
    Decimals print artifacts like 0.023737364000000004."""
    try:
        return Decimal(d).quantize(Decimal("0.000000001")).normalize()
    except (InvalidOperation, ValueError):
        return Decimal(0)


def _iso(ts):
    """Blockscout ISO / unix timestamp -> UTC ISO string, or ''."""
    if ts is None:
        return ""
    try:
        if isinstance(ts, (int, float)):
            return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat().replace("+00:00", "Z")
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except Exception:
        return str(ts)


def _new_row(**kw):
    r = {
        "date": "",
        "tx_hash": "",
        "chain": "",
        "coin": "",
        "token_address": "",
        "type": "",
        "counterparty": "",
        "amount": "0",
        "usd_price": None,
        "amount_usd": None,
        "price_basis": "unavailable",
        "fee_usd": None,
        "fee_coin": "",
        "cost_basis_usd": None,
        "realized_gain_usd": None,
        "fifo_lots": "",
        "fifo_lots_detail": [],
        "debit_account": "",
        "credit_account": "",
        "debit_usd": None,
        "credit_usd": None,
        "memo": "",
        "_fee_amount": None,  # internal: fee in native units, priced later
        "_fee_price_key": None,  # internal: price key for the fee coin
        "_price_key": None,  # internal: (kind, chain, identifier)
        "_ts": "",  # internal: raw timestamp for pricing
    }
    r.update(kw)
    return r


# ---------------------------------------------------------------- collection
async def _collect_evm(h):
    """Native + ERC-20 rows for one EVM chain (first page each)."""
    h_low = h.lower()
    rows = []
    errors = {}

    async def _fetch(c, kind):
        try:
            if kind == "txs":
                d = await ADAPTER.transactions(c, h)
            else:
                d = await ADAPTER.token_transfers(c, h)
        except Exception as e:
            return kind, None, str(e)
        if d is None:
            return kind, [], None
        return kind, d.get("items", []), None

    for chain in CHAINS:
        tx_r, tt_r = await asyncio.gather(_fetch(chain, "txs"), _fetch(chain, "tt"))
        for kind, items, err in (tx_r, tt_r):
            if err:
                errors.setdefault(chain, []).append(f"{kind}: {err}")
                continue
            for it in items or []:
                if kind == "txs":
                    status = (it.get("status") or it.get("result") or "").lower()
                    if status in ("error", "fail", "failed"):
                        continue  # failed tx moved nothing
                    frm = (it.get("from") or {}).get("hash", "")
                    to = (it.get("to") or {}).get("hash", "")
                    if not frm or not to or frm.lower() == to.lower():
                        continue
                    amt = _dec(wei_to_eth(it.get("value")))
                    if amt <= 0:
                        continue
                    is_in = to.lower() == h_low
                    fee = it.get("fee") or {}
                    # The fee is paid by the sender (from). On a receive the
                    # address did not pay it, so the row carries no fee.
                    rows.append(_new_row(
                        date=_iso(it.get("timestamp")),
                        tx_hash=it.get("hash") or "",
                        chain=chain,
                        coin=CHAINS[chain]["native"],
                        token_address="",
                        type="receive" if is_in else "send",
                        counterparty=(frm if is_in else to),
                        amount=_fmt_amount(amt),
                        fee_coin="" if is_in else CHAINS[chain]["native"],
                        _fee_amount=(wei_to_eth(fee.get("value")) if isinstance(fee, dict) else None) if not is_in else None,
                        _fee_price_key=("native", chain, None) if not is_in else None,
                        _price_key=("native", chain, None),
                        _ts=it.get("timestamp") or "",
                    ))
                else:
                    tok = it.get("token") or {}
                    if (tok.get("type") or "") != "ERC-20":
                        continue  # NFT transfers are not fungible rows
                    frm = (it.get("from") or {}).get("hash", "")
                    to = (it.get("to") or {}).get("hash", "")
                    if not frm or not to or frm.lower() == to.lower():
                        continue
                    total = it.get("total") or {}
                    dec = total.get("decimals") if total.get("decimals") is not None else tok.get("decimals")
                    amt = _dec(token_amount(total.get("value"), dec))
                    if amt <= 0:
                        continue
                    is_in = to.lower() == h_low
                    addr = tok.get("address_hash") or tok.get("address") or ""
                    rows.append(_new_row(
                        date=_iso(it.get("timestamp")),
                        tx_hash=it.get("transaction_hash") or "",
                        chain=chain,
                        coin=tok.get("symbol") or addr,
                        token_address=addr,
                        type="receive" if is_in else "send",
                        counterparty=(frm if is_in else to),
                        amount=_fmt_amount(amt),
                        _price_key=("token", chain, addr.lower()),
                        _ts=it.get("timestamp") or "",
                    ))
    return rows, errors


async def _collect_solana(h, sig_limit=SOL_SIG_LIMIT):
    """SOL + SPL rows from the first sig_limit signatures. The
    recurring tracker (common/recurring.py) passes 25 — a recurring
    series needs more than 10 signatures to show up at all."""
    rows = []
    errors = {}
    try:
        sigs = await _sol_signatures(h, limit=sig_limit)
    except Exception as e:
        return rows, {"solana": [f"signatures: {e}"]}
    items = (sigs or {}).get("items", [])
    txs = await asyncio.gather(
        *[sol_tx(s.get("signature")) for s in items if s.get("signature")],
        return_exceptions=True,
    )
    for s, tx in zip([s for s in items if s.get("signature")], txs):
        if isinstance(tx, Exception) or not isinstance(tx, dict):
            errors.setdefault("solana", []).append(
                f"tx {str(s.get('signature'))[:12]}: {tx if isinstance(tx, Exception) else 'no data'}"
            )
            continue
        if tx.get("status") == "failed":
            continue  # failed tx moved nothing (fee aside, reported nowhere)
        ts = tx.get("block_time")
        fee_sol = _dec(tx.get("fee_sol") or "0")
        accounts = tx.get("accounts") or []
        mine = [a for a in accounts if (a.get("address") or "") == h]
        if not mine:
            continue
        my = mine[0]
        change = _dec(my.get("change_sol"))
        # The fee payer's balance change includes the fee; add it back so
        # the row is the transfer amount, not transfer-minus-fee.
        is_payer = bool(my.get("signer")) and fee_sol > 0
        transfer = _q9(change + fee_sol) if is_payer else _q9(change)
        counterparty = ""
        others = [a for a in accounts if (a.get("address") or "") != h and _dec(a.get("change_sol")) != 0]
        if others:
            others.sort(key=lambda a: abs(_dec(a.get("change_sol"))), reverse=True)
            counterparty = others[0].get("address") or ""
        if transfer != 0:
            rows.append(_new_row(
                date=_iso(ts),
                tx_hash=tx.get("signature") or "",
                chain="solana",
                coin="SOL",
                token_address="",
                type="receive" if transfer > 0 else "send",
                counterparty=counterparty,
                amount=_fmt_amount(abs(transfer)),
                fee_coin="SOL",
                _fee_amount=_fmt_amount(_q9(fee_sol)) if is_payer else None,
                _fee_price_key=("native", "solana", None) if is_payer else None,
                _price_key=("native", "solana", None),
                _ts=ts,
            ))
        for tc in tx.get("token_changes") or []:
            if (tc.get("owner") or "") != h:
                continue
            delta = _q9(_dec(tc.get("change")))
            if delta == 0:
                continue
            mint = tc.get("mint") or ""
            # Counterparty: the other owner of the same mint with the
            # opposite-sign change in this transaction.
            cp = ""
            for tc2 in tx.get("token_changes") or []:
                if (tc2.get("mint") or "") == mint and (tc2.get("owner") or "") != h:
                    if _dec(tc2.get("change")) * delta < 0:
                        cp = tc2.get("owner") or ""
                        break
            rows.append(_new_row(
                date=_iso(ts),
                tx_hash=tx.get("signature") or "",
                chain="solana",
                coin=f"SPL:{mint[:8]}" if mint else "SPL",
                token_address=mint,
                type="receive" if delta > 0 else "send",
                counterparty=cp,
                amount=_fmt_amount(abs(delta)),
                fee_coin="SOL",
                _fee_amount=None,  # fee (if any) is on the SOL row for this sig
                _fee_price_key=None,
                _price_key=("token", "solana", mint.lower()),
                _ts=ts,
            ))
    return rows, errors


async def _collect_bitcoin(h):
    """Net-flow BTC rows from the first page of address txs (25)."""
    rows = []
    errors = {}
    try:
        raw = await _btc_api(f"/address/{h}/txs")
    except Exception as e:
        return rows, {"bitcoin": [f"txs: {e}"]}
    if not isinstance(raw, list):
        return rows, {"bitcoin": ["unexpected response (not a list)"]}
    for t in raw:
        if not isinstance(t, dict):
            continue
        st = t.get("status") or {}
        if not st.get("confirmed"):
            continue  # unconfirmed: amounts may still change
        sent = received = 0
        counterparties = []
        for vin in t.get("vin") or []:
            prev = vin.get("prevout") or {}
            a = prev.get("scriptpubkey_address")
            if a == h:
                sent += int(prev.get("value") or 0)
            elif a and a not in counterparties:
                counterparties.append(a)
        for vout in t.get("vout") or []:
            a = vout.get("scriptpubkey_address")
            if a == h:
                received += int(vout.get("value") or 0)
            elif a and a not in counterparties:
                counterparties.append(a)
        net = received - sent
        if net == 0:
            continue
        fee_sats = t.get("fee")
        # The fee is the whole transaction's fee, paid by whoever funded
        # the inputs. On a receive the address did not pay it, so the row
        # carries no fee; on a send it is reported whole (when several
        # senders fund one transaction, the address's share is unknown —
        # see notes).
        is_send = net < 0
        rows.append(_new_row(
            date=_iso(st.get("block_time")),
            tx_hash=t.get("txid") or "",
            chain="bitcoin",
            coin="BTC",
            token_address="",
            type="receive" if net > 0 else "send",
            counterparty=counterparties[0] if counterparties else "",
            amount=_fmt_amount(Decimal(abs(net)) / Decimal(100_000_000)),
            fee_coin="BTC" if is_send else "",
            _fee_amount=(_fmt_amount(Decimal(int(fee_sats)) / Decimal(100_000_000)) if fee_sats is not None else None) if is_send else None,
            _fee_price_key=("native", "bitcoin", None) if is_send and fee_sats is not None else None,
            _price_key=("native", "bitcoin", None),
            _ts=st.get("block_time"),
        ))
    return rows, errors


# ---------------------------------------------------------------- pricing
async def _fetch_series(price_keys, days):
    """{price_key: {YYYY-MM-DD: price}} — one series fetch per distinct
    coin, covering the whole export window. Sepolia native has no market
    price (empty series, never mainnet ETH prices)."""
    out = {}

    async def _one(key):
        kind, chain, ident = key
        try:
            if kind == "native" and chain == "sepolia":
                return key, {}
            if kind == "native" and chain == "bitcoin":
                d = await coingecko("/coins/bitcoin/market_chart",
                                    params={"vs_currency": "usd", "days": days})
                return key, _daily_series(d.get("prices", []) if isinstance(d, dict) else [])
            if kind == "native":
                return key, await _daily_native_prices(chain, days)
            platform = WALLET_CHAINS[chain]["coingecko_platform"]
            return key, await _cg_contract_daily_prices(platform, ident, days)
        except Exception:
            return key, {}

    results = await asyncio.gather(*[_one(k) for k in price_keys])
    return dict(results)


async def _fetch_live(price_keys):
    """{price_key: live usd price or None} — one live lookup per coin."""
    out = {}

    async def _one(key):
        kind, chain, ident = key
        try:
            if kind == "native" and chain == "sepolia":
                return key, None
            if kind == "native" and chain == "bitcoin":
                price, _ = await btc_price()
                return key, price
            if kind == "native" and chain == "solana":
                price, _ = await sol_native_quote()
                return key, price
            if kind == "native":
                price, _ = await native_quote(chain)
                return key, price
            platform = WALLET_CHAINS[chain]["coingecko_platform"]
            return key, await cg_token_price(platform, ident)
        except Exception:
            return key, None

    results = await asyncio.gather(*[_one(k) for k in price_keys])
    return dict(results)


async def _resolve_spl_symbols(rows):
    """Best-effort SPL symbol lookup (CoinGecko contract endpoint, one
    call per mint, cached). Unknown mints keep the SPL:<prefix> coin
    label — never guessed."""
    mints = sorted({r["token_address"] for r in rows
                    if r["chain"] == "solana" and r["token_address"]})
    if not mints:
        return
    results = await asyncio.gather(
        *[_cg_contract_symbol(m) for m in mints], return_exceptions=True
    )
    syms = {}
    for m, res in zip(mints, results):
        if isinstance(res, tuple) and res[0]:
            syms[m] = str(res[0]).upper()
    for r in rows:
        if r["chain"] == "solana" and r["token_address"] in syms:
            r["coin"] = syms[r["token_address"]]


# ---------------------------------------------------------------- export
def _rows_to_csv(payload_rows):
    buf = io.StringIO(newline="")
    w = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, extrasaction="ignore",
                       lineterminator="\r\n")
    w.writeheader()
    for r in payload_rows:
        w.writerow({c: ("" if r.get(c) is None else r.get(c)) for c in CSV_COLUMNS})
    return buf.getvalue()


def _public_row(r):
    """JSON row: CSV columns plus the structured FIFO lot detail."""
    out = {c: r.get(c) for c in CSV_COLUMNS}
    out["fifo_lots_detail"] = [
        {
            "amount": _fmt_amount(l["amount"]),
            "unit_cost_usd": float(l["unit_cost_usd"]),
            "acquired_date": l["acquired_date"],
        }
        for l in r.get("fifo_lots_detail") or []
    ]
    return out


async def accounting_export(address):
    """Full export payload (dict). CSV is rendered from it by the caller.
    5-min cache, like the PnL estimate."""
    key = f"wallet:{address.lower()}:accounting"
    hit = _cache_get(key)
    if hit is not None:
        return hit

    is_btc = is_bitcoin_address(address)
    is_sol = is_solana_address(address)
    rows = []
    errors = {}
    if is_btc:
        btc_rows, btc_err = await _collect_bitcoin(address)
        rows.extend(btc_rows)
        errors.update(btc_err)
    else:
        # EVM chains only for EVM addresses: Blockscout 422s on a
        # base58 Solana address, so a Solana wallet must not fan out
        # to the EVM collectors (same guard as _wallet_activity).
        if _EVM_ADDR_RE.fullmatch(address):
            evm_rows, evm_err = await _collect_evm(address)
            rows.extend(evm_rows)
            errors.update(evm_err)
        if is_sol:
            sol_rows, sol_err = await _collect_solana(address)
            rows.extend(sol_rows)
            errors.update(sol_err)

    rows.sort(key=lambda r: (r["date"] or "", r["tx_hash"] or "", r["coin"] or ""))

    # One price series per distinct coin, sized to the oldest row.
    days = 2
    for r in rows:
        d = _days_since(r["_ts"])
        if d is not None:
            days = max(days, d)
    price_keys = sorted({r["_price_key"] for r in rows if r.get("_price_key")},
                        key=str)
    fee_keys = sorted({r["_fee_price_key"] for r in rows if r.get("_fee_price_key")},
                      key=str)
    all_keys = sorted(set(price_keys) | set(fee_keys), key=str)
    series, live = await asyncio.gather(
        _fetch_series(all_keys, days), _fetch_live(all_keys)
    )

    # Price every row: daily close on the tx date wins (at-time); else
    # the live price (estimate-current); else null (unavailable).
    for r in rows:
        pk = r["_price_key"]
        price = _price_on_day(series.get(pk, {}), r["_ts"])
        basis = "at-time"
        if price is None:
            price = live.get(pk)
            basis = "estimate-current" if price is not None else "unavailable"
        if price is not None:
            r["usd_price"] = float(price)
            r["amount_usd"] = _usd(r["amount"], price)
        r["price_basis"] = basis
        if r.get("_fee_amount") is not None and r.get("_fee_price_key"):
            fk = r["_fee_price_key"]
            fprice = _price_on_day(series.get(fk, {}), r["_ts"])
            if fprice is None:
                fprice = live.get(fk)
            if fprice is not None:
                r["fee_usd"] = _usd(r["_fee_amount"], fprice)

    await _resolve_spl_symbols(rows)

    # FIFO per (chain, coin), reusing the shared _fifo_run engine. The
    # effective series for a coin is its daily closes plus, for transfer
    # dates with no close, the live price the row already fell back to —
    # so estimate-current rows still get lots (flagged by price_basis),
    # while a coin with no price at all stays unpriced and unmatched.
    groups = {}
    for idx, r in enumerate(rows):
        gkey = (r["chain"], r["coin"], r["token_address"])
        groups.setdefault(gkey, []).append(idx)
    fifo_summary = []
    for (chain, coin, tok_addr), idxs in sorted(groups.items(), key=lambda kv: str(kv[0])):
        idxs.sort(key=lambda i: (rows[i]["date"] or "", rows[i]["tx_hash"] or ""))
        pk = rows[idxs[0]]["_price_key"]
        effective = dict(series.get(pk, {}))
        for i in idxs:
            r = rows[i]
            day = (r["date"] or "")[:10]
            if day and day not in effective and r["price_basis"] == "estimate-current" and r["usd_price"] is not None:
                effective[day] = r["usd_price"]
        transfers = [
            {
                "timestamp": rows[i]["date"],
                "amount": _dec(rows[i]["amount"]),
                "is_in": rows[i]["type"] == "receive",
            }
            for i in idxs
        ]
        run = _fifo_run(transfers, effective)
        # Attach each disposal (in chronological order) to its send row.
        # A disposal exists ONLY for sends _fifo_run could price, so the
        # send list here must use the exact same priced test (_fifo_run
        # looks back up to 7 days in the effective series); zipping
        # against every send row would misalign past an unpriced send.
        send_idxs = [
            i for i in idxs
            if rows[i]["type"] == "send"
            and _price_on_or_before(effective, (rows[i]["date"] or "")[:10]) is not None
        ]
        for disp, i in zip(run["disposals"], send_idxs):
            r = rows[i]
            if disp["unmatched_amount"] > 0 and disp["matched_amount"] == 0:
                # No acquisition at all in the window: no cost basis.
                r["cost_basis_usd"] = None
                r["realized_gain_usd"] = None
            else:
                r["cost_basis_usd"] = round(float(disp["cost_usd"]), 2)
                proceeds = round(float(disp["proceeds_usd"]), 2)
                r["realized_gain_usd"] = round(proceeds - float(disp["cost_usd"]), 2)
            r["fifo_lots_detail"] = disp["lots"]
            r["fifo_lots"] = "; ".join(
                f"{_fmt_amount(l['amount'])} @ ${float(l['unit_cost_usd']):,.4f} (acq {l['acquired_date']})"
                for l in disp["lots"]
            )
            if disp["unmatched_amount"] > 0:
                r["fifo_lots"] += (
                    f"; unmatched {_fmt_amount(disp['unmatched_amount'])} (no acquisition in window)"
                    if r["fifo_lots"] else
                    f"unmatched {_fmt_amount(disp['unmatched_amount'])} (no acquisition in window)"
                )
        fifo_summary.append({
            "chain": chain,
            "coin": coin,
            "token_address": tok_addr,
            "transfers": len(idxs),
            "transfers_priced": run["transfers_priced"],
            "transfers_unpriced": run["transfers_unpriced"],
            "transfers_unmatched": run["transfers_unmatched"],
            "proceeds_usd": round(float(run["proceeds_usd"]), 2),
            "cost_usd": round(float(run["cost_usd"]), 2),
            "realized_pnl_usd": round(float(run["realized_pnl_usd"]), 2),
            "lots_remaining": [
                {
                    "amount": _fmt_amount(l["amount"]),
                    "unit_cost_usd": float(l["unit_cost_usd"]),
                    "acquired_date": l["acquired_date"],
                }
                for l in run["lots_remaining"]
            ],
        })

    # Journal legs.
    for r in rows:
        coin = r["coin"]
        if r["type"] == "receive":
            r["debit_account"] = f"Crypto Assets:{coin}"
            r["credit_account"] = "Other Income:Crypto Received"
            r["debit_usd"] = r["amount_usd"]
            r["credit_usd"] = r["amount_usd"]
        else:
            r["debit_account"] = "Cost of Goods Sold:Crypto Disposed"
            r["credit_account"] = f"Crypto Assets:{coin}"
            r["debit_usd"] = r["cost_basis_usd"]
            r["credit_usd"] = r["cost_basis_usd"]
        r["memo"] = (
            f"{'Receive' if r['type'] == 'receive' else 'Send'} {r['amount']} {coin} "
            f"on {WALLET_CHAINS.get(r['chain'], {}).get('label', r['chain'])}"
            f" — tx {r['tx_hash']}"
        )

    for r in rows:
        for k in ("_fee_amount", "_fee_price_key", "_price_key", "_ts"):
            r.pop(k, None)

    public_rows = [_public_row(r) for r in rows]
    n_recv = sum(1 for r in rows if r["type"] == "receive")
    n_send = sum(1 for r in rows if r["type"] == "send")
    tot_recv = round(sum(_dec(r["amount_usd"]) for r in rows
                         if r["type"] == "receive" and r["amount_usd"] is not None), 2)
    tot_sent = round(sum(_dec(r["amount_usd"]) for r in rows
                         if r["type"] == "send" and r["amount_usd"] is not None), 2)
    tot_fees = round(sum(_dec(r["fee_usd"]) for r in rows
                         if r["fee_usd"] is not None), 2)
    tot_cost = round(sum(_dec(r["cost_basis_usd"]) for r in rows
                         if r["cost_basis_usd"] is not None), 2)
    tot_gain = round(sum(_dec(r["realized_gain_usd"]) for r in rows
                         if r["realized_gain_usd"] is not None), 2)
    n_unpriced = sum(1 for r in rows if r["price_basis"] == "unavailable")
    n_estimate = sum(1 for r in rows if r["price_basis"] == "estimate-current")

    out = {
        "address": address,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "rows": public_rows,
        "csv_columns": CSV_COLUMNS,
        "summary": {
            "rows": len(rows),
            "receives": n_recv,
            "sends": n_send,
            "total_received_usd": float(tot_recv),
            "total_sent_usd": float(tot_sent),
            "total_fees_usd": float(tot_fees),
            "total_cost_basis_usd": float(tot_cost),
            "total_realized_gain_usd": float(tot_gain),
            "rows_unpriced": n_unpriced,
            "rows_estimate_current": n_estimate,
            "chains": sorted({r["chain"] for r in rows}),
            "coins": sorted({r["coin"] for r in rows}),
        },
        "fifo": fifo_summary,
        "column_mapping": COLUMN_MAPPING,
        "notes": HONEST_NOTES,
        "errors": errors,
        "method": (
            "Cross-chain send/receive ledger with USD value at time of "
            "transaction (one CoinGecko daily-close series per coin) and "
            "FIFO cost basis per coin (shared engine with the PnL "
            "estimate). Journal columns are shaped for QuickBooks/Xero "
            "general-journal import — see column_mapping."
        ),
    }
    _cache_set(key, out, ttl=EXPORT_CACHE_TTL)
    return out


def accounting_csv(payload):
    """CSV text for an already-fetched export payload."""
    return _rows_to_csv(payload["rows"])
