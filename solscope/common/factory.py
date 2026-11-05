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
    _wallet_nfts,
    _wallet_pnl,
    _wallet_tokens,
)
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

    @app.get(api + "/token/{h}")
    async def api_token(h: str):
        return await _token(chain, h)

    @app.get(api + "/token/{h}/transfers")
    async def api_token_transfers(h: str, request: Request):
        return await _token_transfers(chain, h, dict(request.query_params))

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

    @app.get(api + "/search")
    async def api_search(q: str = ""):
        # ENS / address resolution runs against Ethereum mainnet.
        return await _search("ethereum", q)

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

    @app.get(api + "/address/{addr}")
    async def api_address(addr: str):
        return await sol_address(addr)

    @app.get(api + "/address/{addr}/signatures")
    async def api_address_signatures(addr: str, before: str | None = None):
        return await sol_address_signatures(addr, before=before)

    @app.get(api + "/token/{mint}")
    async def api_token(mint: str):
        return await sol_token(mint)

    @app.get(api + "/token/{mint}/transfers")
    async def api_token_transfers(mint: str, before: str | None = None):
        return await sol_token_transfers(mint, before=before)

    @app.get(api + "/program/{addr}")
    async def api_program(addr: str):
        return await sol_program(addr)

    @app.get(api + "/search")
    async def api_search(q: str = ""):
        return await sol_search(q)

    # Static frontend, mounted last so /api/* routes win.
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
    return app
