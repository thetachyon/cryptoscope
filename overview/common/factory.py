"""Thin-app factories for the *Scope apps.

`create_scope_app` builds a single-chain explorer (ethscope,
sepoliascope, optimismscope, gnosisscope): every route is bound to one
chain, so there is no chain switcher and no `{chain}` path segment.
`create_wallet_app` builds the cross-chain wallet dashboard
(walletscope): the wallet aggregation endpoints plus an Ethereum search
endpoint used for ENS resolution.

All data logic lives in `common/explorer.py` / `common/wallet.py`;
this module only wires FastAPI routes to it. Each app's `app.py` is a
~10-line entrypoint that calls one of these factories.
"""
import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from common.chains import CHAINS, WALLET_CHAINS
from common.clients import UpstreamError
from common.solana import is_solana_address
from common.explorer import (
    _ADDR_RE,
    _address,
    _address_internal,
    _address_token_transfers,
    _address_tokens,
    _address_txs,
    _block,
    _block_txs,
    _blocks,
    _chart_gas,
    _chart_market,
    _chart_price,
    _chart_transactions,
    _contract,
    _contract_call,
    _contract_verify,
    _docs_payload,
    _gastracker,
    _pending,
    _search,
    _stats,
    _token,
    _token_instance,
    _token_instance_transfers,
    _token_instances,
    _token_transfers,
    _tx,
    _tx_internal,
    _tx_logs,
    _tx_token_transfers,
    _txcount,
    _txs_latest,
)
from common.wallet import (
    _wallet,
    _wallet_activity,
    _wallet_activity_page,
    _wallet_history,
    _wallet_incoming,
    _wallet_nfts,
    _wallet_pnl,
    _wallet_tokens,
)
from common.accounting import accounting_csv, accounting_export
from common.accounting_api import accounting_api
from common.bitcoin import is_bitcoin_address
from common.risk import bitcoin_risk, evm_risk, solana_risk
from common.crosschain import bitcoin_crosschain, evm_crosschain, solana_crosschain
from common.explain import btc_tx_explain, evm_tx_explain, sol_tx_explain
from common.security import evm_token_security, solana_token_security
from common.solana_explorer import (
    sol_address,
    sol_address_signatures,
    sol_docs_payload,
    sol_program,
    sol_search,
    sol_slot,
    sol_slots,
    sol_stats,
    sol_token,
    sol_token_transfers,
    sol_tx,
)
from common.bitcoin import (
    btc_address,
    btc_address_txs,
    btc_address_utxo,
    btc_block,
    btc_block_txs,
    btc_blocks,
    btc_docs_payload,
    btc_mempool,
    btc_search,
    btc_stats,
    btc_tx,
)
from common.stablecoin import (
    STABLECOINS,
    sc_activity,
    sc_chains,
    sc_defi,
    sc_depegs,
    sc_docs_payload,
    sc_holders,
    sc_mint_burn,
    sc_peg,
    sc_peg_leaderboard,
    sc_reserves,
    sc_search,
    sc_stats,
    sc_supply,
    sc_supply_map,
    sc_transfers,
    sc_whales,
)
from common.freeze import freeze_watch, freeze_watch_any, sc_freeze
from common.freezefeed import freeze_feed, freeze_feed_scope
from common.supplyfeed import supply_feed, supply_feed_scope
from common.whalefeed import whale_feed, whale_feed_scope
from common.preflight import preflight
from common.screen import screen_address
from common.txscreen import screen_transaction
from common.sanctions import list_sanctions
from common.received import monitor_aftermath
from common.recurring import detect_recurring
from common.settlement import receipt, track_settlement
from common.verify import verify_settlement
from common.explain_api import explain_transaction
from common.security_api import token_security_api
from common.issuer_risk import sc_issuer_risk
from common.issuer_risk_api import ov_issuer_risk_detail
from common.attestation_monitor import ov_attestation_monitor
from common.payments_readiness import sc_payments_readiness
from common.quotes import get_quote, quote_tokens_payload
from common.quote_api import quote_api
from common.allowance import check_allowance
from common.guard import quote_guard, stablecoin_guard
from common.stablecoin_score import sc_depeg_score
from common.depeg_history import sc_depeg_score_history
from common.overview import (
    ov_activity,
    ov_bridges,
    ov_chains,
    ov_defi,
    ov_docs_payload,
    ov_freeze,
    ov_global,
    ov_issuer_risk,
    ov_mint_burn,
    ov_movers,
    ov_overview,
    ov_payments_readiness,
    ov_peg_leaderboard,
    ov_search,
    ov_stablecoins,
    ov_supply_map,
    ov_whales,
)


def _add_handlers(app: FastAPI) -> None:
    @app.exception_handler(UpstreamError)
    async def _upstream_handler(request: Request, exc: UpstreamError):
        return JSONResponse({"error": exc.msg}, status_code=exc.status)

    @app.exception_handler(Exception)
    async def _generic_handler(request: Request, exc: Exception):
        if isinstance(exc, StarletteHTTPException):
            raise exc
        return JSONResponse({"error": "Internal server error"}, status_code=500)


def _chain_entry(chain: str) -> dict:
    c = CHAINS[chain]
    return {
        "id": chain,
        "label": c["label"],
        "blockscout": c["blockscout"],
        "rpc": c["rpc"],
        "coingecko_platform": c["coingecko_platform"],
        "native": c["native"],
    }


def create_scope_app(*, chain: str, scope: str, title: str, static_dir: str) -> FastAPI:
    """Single-chain explorer app. `chain` is the chain id (e.g. "sepolia"),
    `scope` the URL/API prefix (e.g. "sepoliascope"), `title` the display
    name, `static_dir` the app's vendored frontend directory."""
    if chain not in CHAINS:
        raise ValueError(f"Unknown chain: {chain!r}")
    api = f"/api/{scope}"
    app = FastAPI(title=title, version="2.0.0")
    _add_handlers(app)

    @app.get(api + "/chains")
    async def api_chains():
        return {"chains": [_chain_entry(chain)]}

    @app.get(api + "/docs")
    async def api_docs():
        return _docs_payload(api, chains=[chain])

    @app.get(api + "/stats")
    async def api_stats():
        return await _stats(chain)

    @app.get(api + "/blocks")
    async def api_blocks(request: Request):
        return await _blocks(chain, dict(request.query_params))

    @app.get(api + "/txs")
    async def api_txs_latest(request: Request):
        return await _txs_latest(chain, dict(request.query_params))

    @app.get(api + "/block/{n}")
    async def api_block(n: str):
        return await _block(chain, n)

    @app.get(api + "/block/{n}/txs")
    async def api_block_txs(n: str, request: Request):
        return await _block_txs(chain, n, dict(request.query_params))

    @app.get(api + "/tx/{h}")
    async def api_tx(h: str):
        return await _tx(chain, h)

    @app.get(api + "/tx/{h}/explain")
    async def api_tx_explain(h: str):
        return await evm_tx_explain(chain, h)

    @app.get(api + "/tx/{h}/token-transfers")
    async def api_tx_token_transfers(h: str):
        return await _tx_token_transfers(chain, h)

    @app.get(api + "/tx/{h}/logs")
    async def api_tx_logs(h: str):
        return await _tx_logs(chain, h)

    @app.get(api + "/tx/{h}/internal")
    async def api_tx_internal(h: str):
        return await _tx_internal(chain, h)

    @app.get(api + "/address/{h}")
    async def api_address(h: str):
        return await _address(chain, h)

    @app.get(api + "/address/{h}/txs")
    async def api_address_txs(h: str, request: Request):
        return await _address_txs(chain, h, dict(request.query_params))

    @app.get(api + "/address/{h}/token-transfers")
    async def api_address_token_transfers(h: str, request: Request):
        return await _address_token_transfers(chain, h, dict(request.query_params))

    @app.get(api + "/address/{h}/internal")
    async def api_address_internal(h: str, request: Request):
        return await _address_internal(chain, h, dict(request.query_params))

    @app.get(api + "/address/{h}/tokens")
    async def api_address_tokens(h: str):
        return await _address_tokens(chain, h)

    @app.get(api + "/address/{h}/txcount")
    async def api_address_txcount(h: str):
        return await _txcount(chain, h)

    @app.get(api + "/address/{h}/risk")
    async def api_address_risk(h: str):
        return await evm_risk(chain, h)

    @app.get(api + "/address/{h}/crosschain")
    async def api_address_crosschain(h: str):
        return await evm_crosschain(chain, h)

    @app.get(api + "/token/{h}")
    async def api_token(h: str):
        return await _token(chain, h)

    @app.get(api + "/token/{h}/transfers")
    async def api_token_transfers(h: str, request: Request):
        return await _token_transfers(chain, h, dict(request.query_params))

    @app.get(api + "/token/{h}/security")
    async def api_token_security(h: str):
        return await evm_token_security(chain, h)

    @app.get(api + "/token/{h}/instances")
    async def api_token_instances(h: str, request: Request):
        return await _token_instances(chain, h, dict(request.query_params))

    @app.get(api + "/token/{h}/instance/{iid}")
    async def api_token_instance(h: str, iid: str):
        return await _token_instance(chain, h, iid)

    @app.get(api + "/token/{h}/instance/{iid}/transfers")
    async def api_token_instance_transfers(h: str, iid: str, request: Request):
        return await _token_instance_transfers(chain, h, iid, dict(request.query_params))

    @app.get(api + "/contract/{addr}")
    async def api_contract(addr: str):
        return await _contract(chain, addr)

    @app.post(api + "/contract/{addr}/call")
    async def api_contract_call(addr: str, request: Request):
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "JSON body required"}, status_code=400)
        return await _contract_call(chain, addr, body)

    @app.post(api + "/contract/{addr}/verify")
    async def api_contract_verify(addr: str, request: Request):
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "JSON body required"}, status_code=400)
        return await _contract_verify(chain, addr, body)

    @app.get(api + "/charts/transactions")
    async def api_chart_transactions():
        return await _chart_transactions(chain)

    @app.get(api + "/charts/market")
    async def api_chart_market():
        return await _chart_market(chain)

    @app.get(api + "/charts/gas")
    async def api_chart_gas():
        return await _chart_gas(chain)

    @app.get(api + "/charts/price")
    async def api_chart_price(days: int = 30):
        if days < 1 or days > 365:
            return JSONResponse({"error": "days must be between 1 and 365"}, status_code=400)
        return await _chart_price(chain, days)

    @app.get(api + "/gastracker")
    async def api_gastracker():
        return await _gastracker(chain)

    @app.get(api + "/pending")
    async def api_pending():
        return await _pending(chain)

    @app.get(api + "/search")
    async def api_search(q: str = ""):
        return await _search(chain, q)

    # Static frontend, mounted last so /api/* routes win.
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
    return app


def _wallet_chain_entry(chain: str) -> dict:
    c = WALLET_CHAINS[chain]
    return {
        "id": chain,
        "label": c["label"],
        "blockscout": c.get("blockscout"),
        "rpc": c.get("rpc"),
        "coingecko_platform": c["coingecko_platform"],
        "native": c["native"],
    }


def _valid_wallet_address(address):
    """EVM address (0x + 40 hex) or Solana base58 address."""
    return bool(address) and (
        bool(_ADDR_RE.fullmatch(address)) or is_solana_address(address)
    )


def create_wallet_app(*, scope: str, title: str, static_dir: str) -> FastAPI:
    """Cross-chain wallet dashboard app. No explorer routes — the wallet
    aggregation endpoints plus an Ethereum search endpoint (ENS
    resolution for the dashboard). `scope` is the URL/API prefix
    (e.g. "walletscope")."""
    api = f"/api/{scope}"
    app = FastAPI(title=title, version="2.0.0")
    _add_handlers(app)

    @app.get(api + "/chains")
    async def api_chains():
        return {"chains": [_wallet_chain_entry(cid) for cid in WALLET_CHAINS]}

    @app.get(api + "/docs")
    async def api_docs():
        return _docs_payload(api, wallet_prefix=api, include_base=False,
                             chains=list(WALLET_CHAINS.keys()))

    @app.get(api + "/quote/tokens")
    async def api_quote_tokens():
        return quote_tokens_payload()

    @app.get(api + "/quote")
    async def api_quote(request: Request):
        p = dict(request.query_params)
        from_chain = p.get("from_chain", "")
        to_chain = p.get("to_chain", "")
        if from_chain not in WALLET_CHAINS or to_chain not in WALLET_CHAINS:
            return JSONResponse({"error": "Unknown chain (from_chain / to_chain)"}, status_code=400)
        if not p.get("amount") or not p.get("from_token") or not p.get("to_token"):
            return JSONResponse({"error": "amount, from_token and to_token are required"}, status_code=400)
        payload = await get_quote(
            from_chain, p["from_token"], to_chain, p["to_token"],
            p["amount"], p.get("from_address"),
        )
        if payload.get("error"):
            return JSONResponse({"error": payload["error"]}, status_code=400)
        return payload

    @app.get(api + "/quote/with-guard")
    async def api_quote_with_guard(request: Request):
        # Quote & route API (phase 3, item 11): the P1 quote engine
        # plus the P3 peg/volatility guard for both legs, in one
        # call — the productized paid-API contract. A new dedicated
        # endpoint, not a rename: /quote stays the engine backing
        # and /quote/guard the standalone guard check, while
        # /quote/with-guard is the one-call contract (routes +
        # guard verdicts + guard summary). Built on
        # common/quotes.py get_quote and common/guard.py
        # quote_guard wholesale; see common/quote_api.py. The
        # #/quote page calls this endpoint and renders the routes
        # plus the embedded guard banners.
        p = dict(request.query_params)
        from_chain = p.get("from_chain", "")
        to_chain = p.get("to_chain", "")
        if from_chain not in WALLET_CHAINS or to_chain not in WALLET_CHAINS:
            return JSONResponse({"error": "Unknown chain (from_chain / to_chain)"}, status_code=400)
        if not p.get("amount") or not p.get("from_token") or not p.get("to_token"):
            return JSONResponse({"error": "amount, from_token and to_token are required"}, status_code=400)
        payload = await quote_api(
            from_chain, p["from_token"], to_chain, p["to_token"],
            p["amount"], p.get("from_address"),
        )
        if payload.get("error"):
            return JSONResponse({"error": payload["error"]}, status_code=400)
        return payload

    @app.get(api + "/allowance")
    async def api_allowance(request: Request):
        p = dict(request.query_params)
        owner = (p.get("owner") or "").strip()
        token = (p.get("token") or "").strip()
        spender = (p.get("spender") or "").strip()
        if not owner or not token or not spender:
            return JSONResponse({"error": "owner, token and spender are required"}, status_code=400)
        try:
            return await check_allowance(
                owner, token, spender,
                chain=p.get("chain") or "ethereum",
                amount=p.get("amount"),
            )
        except UpstreamError as e:
            return JSONResponse({"error": e.msg}, status_code=e.status)

    @app.get(api + "/quote/guard")
    async def api_quote_guard(request: Request):
        p = dict(request.query_params)
        if not p.get("coin"):
            return JSONResponse({"error": "coin is required"}, status_code=400)
        try:
            return await quote_guard(
                p["coin"], chain=p.get("chain") or None,
                amount=p.get("amount"), slippage_pct=p.get("slippage_pct"),
            )
        except Exception as e:
            from common.clients import UpstreamError as _UE
            if isinstance(e, _UE):
                return JSONResponse({"error": e.msg}, status_code=e.status)
            raise

    @app.get(api + "/freeze/watch")
    async def api_freeze_watch(request: Request):
        p = dict(request.query_params)
        address = (p.get("address") or "").strip()
        if not address:
            return JSONResponse({"error": "address is required"}, status_code=400)
        try:
            return await freeze_watch_any(
                address, coin=p.get("coin"), chain=p.get("chain"))
        except UpstreamError as e:
            return JSONResponse({"error": e.msg}, status_code=e.status)

    @app.get(api + "/freeze/feed")
    async def api_freeze_feed(request: Request):
        # Freeze event feed (phase 3, item 4): polling feed of
        # freeze/blacklist/pause events across the freezable
        # stablecoins (USDC, USDT, PYUSD, FDUSD, TUSD), paginated
        # and filterable by coin (?coin=USDC, comma-separated),
        # event type (?kind=blacklisted), address (?address=0x…),
        # and time window (?from= / ?to= epoch, ?days=N). DAI/USDe
        # have no freeze function and USDD none observed on Tron —
        # they appear in the `coins` coverage block as
        # not_applicable, never as silent empty feeds. Built on
        # common/freeze.py (feature 10) wholesale; see
        # common/freezefeed.py. Rendered at #/freeze-feed.
        try:
            return await freeze_feed(dict(request.query_params))
        except UpstreamError as e:
            return JSONResponse({"error": e.msg}, status_code=e.status)

    @app.get(api + "/token/security")
    async def api_token_security(request: Request):
        # Token security API (phase 3, item 12): one token contract
        # in, a security verdict out — owner powers, holder
        # concentration, buy/sell tax, honeypot simulation and
        # liquidity (EVM), or mint/freeze authority plus holder
        # concentration (Solana). A new dedicated endpoint, not a
        # rename of the per-scope scanner: GET
        # /api/<scope>/token/{h}/security stays exactly as it is (the
        # token-page security card's backing), while /token/security
        # is the productized paid-API contract — the same scan
        # (common/security.py, reused wholesale) plus the contract
        # fields, with cross-chain dispatch on ?chain=. See
        # common/security_api.py. Rendered at #/token-security.
        p = dict(request.query_params)
        chain = (p.get("chain") or "").strip()
        address = (p.get("address") or "").strip()
        if not chain or not address:
            return JSONResponse({"error": "chain and address are required"}, status_code=400)
        return await token_security_api(chain, address)

    @app.get(api + "/screen/address")
    async def api_screen_address(request: Request):
        # Address screening (phase 3, item 1): one address in, score +
        # band + reasons + hop exposure out. Cross-chain — the family
        # (EVM / Solana / Bitcoin) is detected from the address, and
        # ENS names resolve on Ethereum mainnet. ?chain= picks the EVM
        # chain for the walk (default ethereum).
        p = dict(request.query_params)
        address = (p.get("address") or "").strip()
        if not address:
            return JSONResponse({"error": "address is required"}, status_code=400)
        return await screen_address(address, chain=p.get("chain"))

    @app.get(api + "/screen/transaction")
    async def api_screen_transaction(request: Request):
        # Transaction screening (phase 3, item 2): one tx in, score +
        # band + reasons + hop exposure out, with the settlement
        # summary (value, USD at execution, confirmations). Reuses
        # common/settlement.py for the tx data and common/risk.py
        # for the sanctions walk (see common/txscreen.py). ?chain=
        # is the tx's chain (ethereum / sepolia / optimism / gnosis /
        # solana / bitcoin); ?hash= the tx hash / signature / txid.
        p = dict(request.query_params)
        chain = (p.get("chain") or "").strip()
        h = (p.get("hash") or "").strip()
        if not chain or not h:
            return JSONResponse({"error": "chain and hash are required"}, status_code=400)
        return await screen_transaction(chain, h)

    @app.get(api + "/sanctions")
    async def api_sanctions(request: Request):
        # Sanctions list API (phase 3, item 3): the vendored OFAC SDN
        # crypto-address set as a queryable list — filter by program
        # (?program=DPRK3), chain family (?family=evm / bitcoin /
        # solana / other), SDN-name substring (?name=LAZARUS), with
        # pagination (?page=, ?page_size=, default 50, max 100).
        # Reuses common/risk.py wholesale (see common/sanctions.py);
        # the response also carries source metadata, per-chain and
        # per-program counts, and a diff against the committed
        # baseline snapshot (empty until a rebuild changes the set).
        return list_sanctions(dict(request.query_params))

    @app.get(api + "/preflight")
    async def api_preflight(request: Request):
        p = dict(request.query_params)
        address = (p.get("address") or "").strip()
        if not address:
            return JSONResponse({"error": "address is required"}, status_code=400)
        return await preflight(address, coin=p.get("coin"),
                               chain=p.get("chain"), amount=p.get("amount"))

    @app.get(api + "/settle")
    async def api_settle(request: Request):
        p = dict(request.query_params)
        chain = (p.get("chain") or "").strip()
        h = (p.get("hash") or "").strip()
        if not chain or not h:
            return JSONResponse({"error": "chain and hash are required"}, status_code=400)
        return await track_settlement(chain, h)

    @app.get(api + "/receipt")
    async def api_receipt(request: Request):
        p = dict(request.query_params)
        chain = (p.get("chain") or "").strip()
        h = (p.get("hash") or "").strip()
        if not chain or not h:
            return JSONResponse({"error": "chain and hash are required"}, status_code=400)
        return await receipt(chain, h)

    @app.get(api + "/verify")
    async def api_verify(request: Request):
        # Settlement verification API (phase 3, item 9): one tx in,
        # verdict + finality + USD at execution + receipt out. A new
        # dedicated endpoint, not a rename of /settle: /settle stays
        # the Q1 tracker backing and /receipt the shareable receipt,
        # while /verify is the productized paid-API contract — the
        # tracker payload (common/settlement.py track_settlement)
        # plus the receipt (common/settlement.py receipt) plus the
        # verdict, reused wholesale; see common/verify.py. Rendered
        # at #/verify (the #/track page calls this endpoint).
        p = dict(request.query_params)
        chain = (p.get("chain") or "").strip()
        h = (p.get("hash") or "").strip()
        if not chain or not h:
            return JSONResponse({"error": "chain and hash are required"}, status_code=400)
        return await verify_settlement(chain, h)

    @app.get(api + "/explain")
    async def api_explain(request: Request):
        # Plain-English transaction API (phase 3, item 13): one tx
        # in, one sentence + token flows out — the tx explainer
        # (common/explain.py, feature 3) as a productized paid-API
        # contract. A new dedicated endpoint, not a rename:
        # GET /api/<scope>/tx/{h}/explain stays exactly as it is
        # (the tx-page "What happened" card's backing), while
        # /explain is the cross-chain contract — the same
        # explanation payload plus the contract fields, with
        # dispatch on ?chain= (ethereum / sepolia / optimism /
        # gnosis / solana / bitcoin). See common/explain_api.py.
        # Rendered at #/explain.
        p = dict(request.query_params)
        chain = (p.get("chain") or "").strip()
        h = (p.get("hash") or "").strip()
        if not chain or not h:
            return JSONResponse({"error": "chain and hash are required"}, status_code=400)
        return await explain_transaction(chain, h)

    @app.get(api + "/aftermath")
    async def api_aftermath(request: Request):
        p = dict(request.query_params)
        chain = (p.get("chain") or "").strip()
        h = (p.get("hash") or "").strip()
        if not chain or not h:
            return JSONResponse({"error": "chain and hash are required"}, status_code=400)
        return await monitor_aftermath(chain, h)

    @app.get(api + "/search")
    async def api_search(q: str = ""):
        # ENS / address resolution runs against Ethereum mainnet.
        return await _search("ethereum", q)

    @app.get(api + "/accounting")
    async def api_accounting(request: Request):
        # Accounting API (phase 3, item 10): the Q2 export as a
        # documented paid-API contract — the full export payload
        # (common/accounting.py accounting_export, reused wholesale)
        # plus method / limits / coverage / price_basis_counts and
        # api/csv/json URLs. A new dedicated endpoint, not a rename:
        # /accounting/export stays the download backing, while
        # /accounting is the productized contract. format=csv returns
        # the same CSV bytes as the export endpoint; the contract
        # fields are JSON-only. Rendered in the wallet dashboard's
        # Accounting Export card (which now fetches this endpoint).
        # See common/accounting_api.py.
        from fastapi.responses import Response
        p = dict(request.query_params)
        address = (p.get("address") or "").strip()
        fmt = (p.get("format") or "json").lower()
        if not address:
            return JSONResponse({"error": "address is required"}, status_code=400)
        if not _valid_wallet_address(address) and not is_bitcoin_address(address):
            return JSONResponse({"error": "Invalid address (expected 0x + 40 hex chars, a Solana base58 address, or a Bitcoin address)"}, status_code=400)
        if fmt not in ("csv", "json"):
            return JSONResponse({"error": "format must be csv or json"}, status_code=400)
        if fmt == "csv":
            return Response(
                await accounting_api(address, fmt="csv"),
                media_type="text/csv; charset=utf-8",
                headers={"Content-Disposition": f'attachment; filename="cryptoscope-accounting-{address[:12]}.csv"'},
            )
        return await accounting_api(address, fmt="json")

    @app.get(api + "/accounting/export")
    async def api_accounting_export(request: Request):
        from fastapi.responses import Response
        p = dict(request.query_params)
        address = (p.get("address") or "").strip()
        fmt = (p.get("format") or "csv").lower()
        if not address:
            return JSONResponse({"error": "address is required"}, status_code=400)
        if not _valid_wallet_address(address) and not is_bitcoin_address(address):
            return JSONResponse({"error": "Invalid address (expected 0x + 40 hex chars, a Solana base58 address, or a Bitcoin address)"}, status_code=400)
        if fmt not in ("csv", "json"):
            return JSONResponse({"error": "format must be csv or json"}, status_code=400)
        payload = await accounting_export(address)
        if fmt == "json":
            return payload
        return Response(
            accounting_csv(payload),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="cryptoscope-accounting-{address[:12]}.csv"'},
        )

    @app.get(api + "/wallet/{address}")
    async def api_wallet(address: str):
        if not _valid_wallet_address(address or ""):
            return JSONResponse({"error": "Invalid address (expected 0x + 40 hex chars, or a Solana base58 address)"}, status_code=400)
        return await _wallet(address)

    @app.get(api + "/wallet/{address}/tokens")
    async def api_wallet_tokens(address: str, chain: str | None = None):
        if not _valid_wallet_address(address or ""):
            return JSONResponse({"error": "Invalid address (expected 0x + 40 hex chars, or a Solana base58 address)"}, status_code=400)
        if chain is not None and chain not in WALLET_CHAINS:
            return JSONResponse({"error": "Unknown chain"}, status_code=404)
        return await _wallet_tokens(address, chain)

    @app.get(api + "/wallet/{address}/history")
    async def api_wallet_history(address: str, days: int = 30):
        if not _valid_wallet_address(address or ""):
            return JSONResponse({"error": "Invalid address (expected 0x + 40 hex chars, or a Solana base58 address)"}, status_code=400)
        if days < 1 or days > 90:
            return JSONResponse({"error": "days must be between 1 and 90"}, status_code=400)
        return await _wallet_history(address, days)

    @app.get(api + "/wallet/{address}/pnl")
    async def api_wallet_pnl(address: str):
        if not _valid_wallet_address(address or ""):
            return JSONResponse({"error": "Invalid address (expected 0x + 40 hex chars, or a Solana base58 address)"}, status_code=400)
        return await _wallet_pnl(address)

    @app.get(api + "/wallet/{address}/nfts")
    async def api_wallet_nfts(address: str):
        if not _valid_wallet_address(address or ""):
            return JSONResponse({"error": "Invalid address (expected 0x + 40 hex chars, or a Solana base58 address)"}, status_code=400)
        return await _wallet_nfts(address)

    @app.get(api + "/wallet/{address}/incoming")
    async def api_wallet_incoming(address: str):
        # Incoming transfers only — the invoice-reconciliation feed
        # (phase 2, Q3). Takes an address only: it accepts NO invoice
        # data, so matching must run client-side on this payload.
        if not _valid_wallet_address(address or ""):
            return JSONResponse({"error": "Invalid address (expected 0x + 40 hex chars, or a Solana base58 address)"}, status_code=400)
        return await _wallet_incoming(address)

    @app.get(api + "/recurring")
    async def api_recurring(request: Request):
        # Recurring-payment tracker (phase 2, Q5): repeating payments
        # for one address — same counterparty, same coin, approx same
        # amount, roughly regular interval. ?direction=out (default) /
        # in / all. Bitcoin addresses accepted too (like the
        # accounting export); ENS names are NOT resolved here.
        p = dict(request.query_params)
        address = (p.get("address") or "").strip()
        direction = (p.get("direction") or "out").lower()
        if not address:
            return JSONResponse({"error": "address is required"}, status_code=400)
        if not _valid_wallet_address(address) and not is_bitcoin_address(address):
            return JSONResponse({"error": "Invalid address (expected 0x + 40 hex chars, a Solana base58 address, or a Bitcoin address)"}, status_code=400)
        if direction not in ("out", "in", "all"):
            return JSONResponse({"error": "direction must be out, in or all"}, status_code=400)
        try:
            return await detect_recurring(address, direction)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    @app.get(api + "/wallet/{address}/activity")
    async def api_wallet_activity(address: str, request: Request):
        if not _valid_wallet_address(address or ""):
            return JSONResponse({"error": "Invalid address (expected 0x + 40 hex chars, or a Solana base58 address)"}, status_code=400)
        params = dict(request.query_params)
        chain = params.pop("chain", None)
        if chain:
            # One further page of one chain (Load-more): cursor params are
            # Blockscout's next_page_params for that chain.
            if chain not in CHAINS:
                return JSONResponse({"error": "Unknown chain"}, status_code=404)
            try:
                return await _wallet_activity_page(address, chain, params)
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=502)
        return await _wallet_activity(address)

    # Static frontend (wallet-only shell), mounted last so /api/* routes win.
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
    return app


def create_solana_app(*, scope: str, title: str, static_dir: str) -> FastAPI:
    """Solana explorer app (solscope). Data is the public Solana RPC
    (keyless); no Blockscout, no EVM. `scope` is the URL/API prefix
    (e.g. "solscope")."""
    api = f"/api/{scope}"
    app = FastAPI(title=title, version="2.0.0")
    _add_handlers(app)

    @app.get(api + "/chains")
    async def api_chains():
        return {"chains": [{"id": "solana", "label": "Solana", "rpc": "https://api.mainnet-beta.solana.com", "native": "SOL", "coingecko_platform": "solana"}]}

    @app.get(api + "/docs")
    async def api_docs():
        return sol_docs_payload(api)

    @app.get(api + "/stats")
    async def api_stats():
        return await sol_stats()

    @app.get(api + "/slots")
    async def api_slots(limit: int = 10, before: int | None = None):
        return await sol_slots(limit=limit, before=before)

    @app.get(api + "/slot/{n}")
    async def api_slot(n: str):
        return await sol_slot(n)

    @app.get(api + "/tx/{sig}")
    async def api_tx(sig: str):
        return await sol_tx(sig)

    @app.get(api + "/tx/{sig}/explain")
    async def api_tx_explain(sig: str):
        return await sol_tx_explain(sig)

    @app.get(api + "/address/{addr}")
    async def api_address(addr: str):
        return await sol_address(addr)

    @app.get(api + "/address/{addr}/signatures")
    async def api_address_signatures(addr: str, before: str | None = None):
        return await sol_address_signatures(addr, before=before)

    @app.get(api + "/address/{addr}/risk")
    async def api_address_risk(addr: str):
        return await solana_risk(addr)

    @app.get(api + "/address/{addr}/crosschain")
    async def api_address_crosschain(addr: str):
        return await solana_crosschain(addr)

    @app.get(api + "/token/{mint}")
    async def api_token(mint: str):
        return await sol_token(mint)

    @app.get(api + "/token/{mint}/transfers")
    async def api_token_transfers(mint: str, before: str | None = None):
        return await sol_token_transfers(mint, before=before)

    @app.get(api + "/token/{mint}/security")
    async def api_token_security(mint: str):
        return await solana_token_security(mint)

    @app.get(api + "/program/{addr}")
    async def api_program(addr: str):
        return await sol_program(addr)

    @app.get(api + "/search")
    async def api_search(q: str = ""):
        return await sol_search(q)

    # Static frontend, mounted last so /api/* routes win.
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
    return app


def create_bitcoin_app(*, scope: str, title: str, static_dir: str) -> FastAPI:
    """Bitcoin explorer app (bitscope). Data is mempool.space (primary)
    with Blockstream Esplora as fallback (keyless); no EVM, no Blockscout.
    `scope` is the URL/API prefix (e.g. "bitscope")."""
    api = f"/api/{scope}"
    app = FastAPI(title=title, version="2.0.0")
    _add_handlers(app)

    @app.get(api + "/chains")
    async def api_chains():
        return {"chains": [{"id": "bitcoin", "label": "Bitcoin", "api": "https://mempool.space/api", "native": "BTC", "coingecko_platform": "bitcoin"}]}

    @app.get(api + "/docs")
    async def api_docs():
        return btc_docs_payload(api)

    @app.get(api + "/stats")
    async def api_stats():
        return await btc_stats()

    @app.get(api + "/blocks")
    async def api_blocks(before: int | None = None):
        return await btc_blocks(before=before)

    @app.get(api + "/block/{n}")
    async def api_block(n: str):
        return await btc_block(n)

    @app.get(api + "/block/{n}/txs")
    async def api_block_txs(n: str, start: int = 0):
        return await btc_block_txs(n, start=start)

    @app.get(api + "/tx/{txid}")
    async def api_tx(txid: str):
        return await btc_tx(txid)

    @app.get(api + "/tx/{txid}/explain")
    async def api_tx_explain(txid: str):
        return await btc_tx_explain(txid)

    @app.get(api + "/address/{addr}")
    async def api_address(addr: str):
        return await btc_address(addr)

    @app.get(api + "/address/{addr}/txs")
    async def api_address_txs(addr: str, after: str | None = None):
        return await btc_address_txs(addr, after=after)

    @app.get(api + "/address/{addr}/utxo")
    async def api_address_utxo(addr: str):
        return await btc_address_utxo(addr)

    @app.get(api + "/address/{addr}/risk")
    async def api_address_risk(addr: str):
        return await bitcoin_risk(addr)

    @app.get(api + "/address/{addr}/crosschain")
    async def api_address_crosschain(addr: str):
        return await bitcoin_crosschain(addr)

    @app.get(api + "/mempool")
    async def api_mempool():
        return await btc_mempool()

    @app.get(api + "/search")
    async def api_search(q: str = ""):
        return await btc_search(q)

    # Static frontend, mounted last so /api/* routes win.
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
    return app


def create_stablecoin_app(*, scope: str, title: str, static_dir: str) -> FastAPI:
    """Stablecoin dashboard app (usdtscope, usdcscope, ...). One coin per
    app; data is DeFiLlama (peg/supply) + CoinGecko (market fallback) +
    Blockscout (Ethereum holders/transfers) or TronScan (USDD on Tron),
    all keyless. `scope` is the URL/API prefix and the key into
    common.stablecoin.STABLECOINS."""
    if scope not in STABLECOINS:
        raise ValueError(f"Unknown stablecoin scope: {scope!r}")
    api = f"/api/{scope}"
    app = FastAPI(title=title, version="2.0.0")
    _add_handlers(app)

    @app.get(api + "/chains")
    async def api_chains():
        return await sc_chains(scope)

    @app.get(api + "/supply-map")
    async def api_supply_map():
        return await sc_supply_map(scope)

    @app.get(api + "/docs")
    async def api_docs():
        return sc_docs_payload(api, scope)

    @app.get(api + "/stats")
    async def api_stats():
        return await sc_stats(scope)

    @app.get(api + "/peg")
    async def api_peg(days: int = 30):
        if days < 1 or days > 365:
            return JSONResponse({"error": "days must be between 1 and 365"}, status_code=400)
        return await sc_peg(scope, days)

    @app.get(api + "/supply")
    async def api_supply(days: int = 90):
        if days < 1 or days > 365:
            return JSONResponse({"error": "days must be between 1 and 365"}, status_code=400)
        return await sc_supply(scope, days)

    @app.get(api + "/holders")
    async def api_holders(request: Request):
        return await sc_holders(scope, dict(request.query_params))

    @app.get(api + "/transfers")
    async def api_transfers(request: Request):
        return await sc_transfers(scope, dict(request.query_params))

    @app.get(api + "/depegs")
    async def api_depegs():
        return await sc_depegs(scope)

    @app.get(api + "/depeg-score")
    async def api_depeg_score():
        return await sc_depeg_score(scope)

    @app.get(api + "/depeg-score/history")
    async def api_depeg_score_history(days: int = 30):
        # Depeg early-warning score history (phase 3, item 5): the
        # current full score (reused wholesale from /depeg-score,
        # embedded as `current`) + a daily PARTIAL-score history
        # over the last `days` days + threshold-crossing events.
        # A full-score daily history is not reconstructable keyless
        # — see common/depeg_history.py: each history day covers
        # only the 47.5% of live weight measurable from the
        # DeFiLlama daily-close price history (redemption quality
        # + issuer incident half), with reserve liquidity,
        # secondary-market depth and issuer disclosure marked
        # unknown per day, never backfilled. Rendered at
        # #/depeg-history.
        if days < 7 or days > 365:
            return JSONResponse({"error": "days must be between 7 and 365"}, status_code=400)
        return await sc_depeg_score_history(scope, days)

    @app.get(api + "/quote-guard")
    async def api_quote_guard(request: Request):
        p = dict(request.query_params)
        return await stablecoin_guard(scope, amount=p.get("amount"))

    @app.get(api + "/peg-leaderboard")
    async def api_peg_leaderboard():
        return await sc_peg_leaderboard(this_scope=scope)

    @app.get(api + "/reserves")
    async def api_reserves():
        return await sc_reserves(scope)

    @app.get(api + "/mint-burn")
    async def api_mint_burn():
        return await sc_mint_burn(scope)

    @app.get(api + "/whales")
    async def api_whales():
        return await sc_whales(scope)

    @app.get(api + "/whale-feed")
    async def api_whale_feed(request: Request):
        # Whale feed (phase 3, item 14) for this coin: large-transfer
        # events (>= max($1M, 0.01% of supply), latest 10) as a
        # paginated feed, filterable by minimum USD value
        # (?min_usd=) and time window (?from= / ?to= epoch, ?days=N).
        # Built on common/stablecoin.py wholesale (feature-6
        # /whales) — see common/whalefeed.py. Rendered at
        # #/whale-feed in the overview app (with ?coin=<this coin>).
        try:
            return await whale_feed_scope(scope, dict(request.query_params))
        except UpstreamError as e:
            return JSONResponse({"error": e.msg}, status_code=e.status)

    @app.get(api + "/activity")
    async def api_activity():
        return await sc_activity(scope)

    @app.get(api + "/payments-readiness")
    async def api_payments_readiness():
        return await sc_payments_readiness(scope)

    @app.get(api + "/freeze")
    async def api_freeze():
        return await sc_freeze(scope)

    @app.get(api + "/freeze/feed")
    async def api_freeze_feed(request: Request):
        # Freeze event feed (phase 3, item 4) for this coin:
        # paginated, filterable by event type (?kind=), address
        # (?address=0x…), and time window (?from= / ?to= epoch,
        # ?days=N). For DAI/USDe/USDD the response is
        # status not_applicable with the ABI evidence — no freeze
        # function, no events to list — never a silent empty feed.
        try:
            return await freeze_feed_scope(scope, dict(request.query_params))
        except UpstreamError as e:
            return JSONResponse({"error": e.msg}, status_code=e.status)

    @app.get(api + "/freeze/watch")
    async def api_freeze_watch(request: Request):
        p = dict(request.query_params)
        address = (p.get("address") or "").strip()
        if not address:
            return JSONResponse({"error": "address is required"}, status_code=400)
        try:
            return await freeze_watch(scope, address)
        except UpstreamError as e:
            return JSONResponse({"error": e.msg}, status_code=e.status)

    @app.get(api + "/supply-feed")
    async def api_supply_feed(request: Request):
        # Supply & flow feed (phase 3, item 8) for this coin: the
        # daily net-issuance series + large on-chain mint/burn
        # events, paginated and filterable by record kind (?kind=
        # net_change / mint / burn) and time window (?from= / ?to=
        # epoch, ?days=N). Built on common/stablecoin.py wholesale
        # (feature-4 /mint-burn + feature-5 /supply-map) — see
        # common/supplyfeed.py. Rendered at #/supply-feed in the
        # overview app (with ?coin=<this coin>).
        try:
            return await supply_feed_scope(scope, dict(request.query_params))
        except UpstreamError as e:
            return JSONResponse({"error": e.msg}, status_code=e.status)

    @app.get(api + "/issuer-risk")
    async def api_issuer_risk():
        return await sc_issuer_risk(scope)

    @app.get(api + "/defi")
    async def api_defi():
        return await sc_defi(scope)

    @app.get(api + "/search")
    async def api_search(q: str = ""):
        return await sc_search(scope, q)

    # Static frontend, mounted last so /api/* routes win.
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
    return app


def create_overview_app(*, scope: str, title: str, static_dir: str) -> FastAPI:
    """Cryptoscope overview app (overview). Cross-chain analytics
    landing page: global market, per-chain comparison, stablecoin peg
    table, Wormhole bridge flows, top movers, and search routing to
    the right Scope. `scope` is the URL/API prefix (e.g. "overview")."""
    api = f"/api/{scope}"
    app = FastAPI(title=title, version="2.0.0")
    _add_handlers(app)

    @app.get(api + "/overview")
    async def api_overview():
        return await ov_overview()

    @app.get(api + "/stats")
    async def api_stats():
        return await ov_global()

    @app.get(api + "/chains")
    async def api_chains():
        return await ov_chains()

    @app.get(api + "/stablecoins")
    async def api_stablecoins():
        return await ov_stablecoins()

    @app.get(api + "/stablecoin-leaderboard")
    async def api_stablecoin_leaderboard():
        return await ov_peg_leaderboard()

    @app.get(api + "/mint-burn")
    async def api_mint_burn_rollup():
        return await ov_mint_burn()

    @app.get(api + "/supply-feed")
    async def api_supply_feed(request: Request):
        # Supply & flow feed (phase 3, item 8): daily net-issuance
        # series + large on-chain mint/burn events across all 8
        # stablecoin scopes, paginated and filterable by coin
        # (?coin=USDC, comma-separated, symbol or scope), record
        # kind (?kind=net_change / mint / burn) and time window
        # (?from= / ?to= epoch, ?days=N last N days). Built on
        # common/stablecoin.py wholesale (feature-4 /mint-burn +
        # feature-5 /supply-map) — see common/supplyfeed.py.
        # Standalone (not in /overview): the per-coin on-chain scans
        # are slow. Rendered at #/supply-feed.
        try:
            return await supply_feed(dict(request.query_params))
        except UpstreamError as e:
            return JSONResponse({"error": e.msg}, status_code=e.status)

    @app.get(api + "/whales")
    async def api_whales_rollup():
        return await ov_whales()

    @app.get(api + "/whale-feed")
    async def api_whale_feed(request: Request):
        # Whale feed (phase 3, item 14): large-transfer events
        # across all 8 stablecoin scopes, paginated and filterable
        # by coin (?coin=USDC, comma-separated, symbol or scope),
        # minimum USD value (?min_usd=) and time window (?from= /
        # ?to= epoch, ?days=N last N days). Built on
        # common/stablecoin.py wholesale (feature-6 /whales) — see
        # common/whalefeed.py. Standalone (not in /overview): the
        # per-coin transfer scans are slow. Rendered at #/whale-feed.
        try:
            return await whale_feed(dict(request.query_params))
        except UpstreamError as e:
            return JSONResponse({"error": e.msg}, status_code=e.status)

    @app.get(api + "/activity")
    async def api_activity_rollup():
        return await ov_activity()

    @app.get(api + "/payments-readiness")
    async def api_payments_readiness_rollup():
        return await ov_payments_readiness()

    @app.get(api + "/freeze")
    async def api_freeze_rollup():
        return await ov_freeze()

    @app.get(api + "/issuer-risk")
    async def api_issuer_risk_rollup():
        return await ov_issuer_risk()

    @app.get(api + "/issuer-risk/detail")
    async def api_issuer_risk_detail():
        # Issuer risk API (phase 3, item 6): the 8-issuer ranking
        # (the /issuer-risk rollup reused wholesale) PLUS the full
        # per-issuer payload for every coin (sc_issuer_risk reused
        # wholesale per coin, embedded as `issuers` in rank order),
        # with weights, bands, direction, methodology and limits
        # stated in every response. The per-scope /issuer-risk and
        # the rollup above are left untouched. Rendered at
        # #/issuer-risk. See common/issuer_risk_api.py.
        return await ov_issuer_risk_detail()

    @app.get(api + "/attestations")
    async def api_attestations():
        # Attestation monitor API (phase 3, item 7): all 8 coins'
        # attestation status in one call — the feature-3 tracker
        # (GET /api/<scope>/reserves -> attestation_tracker)
        # reused wholesale per coin via one gather of sc_reserves,
        # embedded verbatim, coins ordered overdue first.
        # Current-status-only: no auditor-change history exists
        # (see common/attestation_monitor.py). Rendered at
        # #/attestations. No per-scope endpoint is added — the
        # per-scope tracker already exists in /reserves.
        return await ov_attestation_monitor()

    @app.get(api + "/defi")
    async def api_defi_rollup():
        return await ov_defi()

    @app.get(api + "/supply-map")
    async def api_supply_map_rollup():
        return await ov_supply_map()

    @app.get(api + "/bridges")
    async def api_bridges():
        return await ov_bridges()

    @app.get(api + "/movers")
    async def api_movers():
        return await ov_movers()

    @app.get(api + "/docs")
    async def api_docs():
        return ov_docs_payload(api)

    @app.get(api + "/search")
    async def api_search(q: str = ""):
        try:
            return ov_search(q)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        except LookupError as e:
            return JSONResponse({"error": str(e)}, status_code=404)

    # Static frontend, mounted last so /api/* routes win.
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
    return app
