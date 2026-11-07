"""Token security API (phase 3, item 12) — keyless.

One token contract in, a security verdict out: owner powers, holder
concentration, buy/sell tax, honeypot simulation and liquidity
(EVM), or mint/freeze authority plus holder concentration (Solana) —
with the per-check evidence, the reasons, and the honest limits of
the method, in a single call.

Nothing is rebuilt here — the scan is reused wholesale:

- EVM: common/security.py ``evm_token_security`` (feature 2).
- Solana: common/security.py ``solana_token_security`` (feature 2).

Endpoint home: ``GET /api/walletscope/token/security?chain=&address=``
— a new dedicated endpoint, not a rename of the per-scope scanner.
``GET /api/<scope>/token/{h}/security`` stays exactly as it is: it is
the token-page security card's backing (and the Solana one backs the
SolScope token page), while ``/token/security`` is the productized
paid-API contract — the same scan payload plus the contract fields
(``security``, ``api_url``, ``method``, ``limits``, ``chain_label``)
and the cross-chain dispatch on ``?chain=``. Chains: ethereum /
sepolia / optimism / gnosis / solana. Bitcoin has no token contracts
to scan and is rejected with a 400, not an empty verdict.

Honest labels (also stated in every response, inherited from the
scan):
  - Buy/sell tax is "not detectable" when no standard tax function
    exists in the ABI — never an invented 0%. A tax can still hide
    in transfer logic with no standard function.
  - Liquidity totals cover DexScreener-tracked pairs on this chain
    only — a stated lower bound, not all liquidity everywhere.
  - A passing honeypot simulation does not guarantee a real swap
    succeeds (slippage, MEV, approvals) — the simulations are
    read-only eth_call only; no transaction is ever sent.
  - The scan is a heuristic screening signal, not an audit and not
    investment advice. A low verdict is not proof a token is safe.
  - On Solana the EVM checks (owner powers, tax, honeypot, DEX
    liquidity) are listed with status "n/a" and labelled EVM-only —
    they are never pretended to have run.
  - Results are cached 300s server-side; a repeat call inside the
    window returns the same scan.
"""
from common.chains import CHAINS
from common.clients import UpstreamError
from common.security import (
    LIMITS as SCAN_LIMITS,
    METHOD as SCAN_METHOD,
    evm_token_security,
    solana_token_security,
)

SUPPORTED_EVM_CHAINS = ("ethereum", "sepolia", "optimism", "gnosis")
SUPPORTED_CHAINS = SUPPORTED_EVM_CHAINS + ("solana",)

METHOD = (
    "Token security API (phase 3, item 12). The token security scan "
    "from common/security.py reused wholesale — owner powers from the "
    "verified ABI + live owner() eth_call, holder concentration from "
    "Blockscout holders, buy/sell tax only from standard tax "
    "functions, honeypot as read-only eth_call buy/sell simulations, "
    "liquidity from DexScreener-tracked pairs + on-chain V2 reserves "
    "(EVM); mint/freeze authority + holder concentration on Solana, "
    "with the EVM checks labelled EVM-only. This module only adds the "
    "paid-API contract fields (security, api_url, method, limits, "
    "chain_label) and the cross-chain dispatch on ?chain= — no check "
    "is re-implemented."
)

LIMITS = [
    SCAN_LIMITS,
    "Buy/sell tax is 'not detectable' when no standard tax function "
    "exists in the contract ABI — never an invented 0%. A tax can "
    "still hide in transfer logic with no standard function.",
    "Liquidity totals cover DexScreener-tracked pairs on this chain "
    "only — a stated lower bound, not all liquidity everywhere.",
    "A passing honeypot simulation does not guarantee a real swap "
    "succeeds (slippage, MEV, approvals). The simulations are "
    "read-only eth_call only; no transaction is ever sent.",
    "Holder concentration covers the first 50 holders from Blockscout "
    "(EVM) or the largest-accounts list from the public RPC (Solana, "
    "up to 20 accounts).",
    "On Solana the EVM checks (owner powers, tax, honeypot simulation, "
    "DEX liquidity) are listed with status 'n/a' and labelled "
    "EVM-only — they are never pretended to have run.",
    "Bitcoin has no token contracts to scan and is not accepted here — "
    "use the address screening API for Bitcoin addresses.",
    "Results are cached 300s server-side; a repeat call inside the "
    "window returns the same scan.",
]


async def token_security_api(chain, address):
    """Scan one token: the wholesale scan payload plus the paid-API
    contract fields. Raises UpstreamError (400/404) for unknown
    chains, malformed addresses and tokens no upstream knows."""
    chain = (chain or "").strip().lower()
    address = (address or "").strip()
    if chain not in SUPPORTED_CHAINS:
        raise UpstreamError(
            "Unknown chain for token security (ethereum / sepolia / "
            "optimism / gnosis / solana)",
            status=400,
        )
    if chain == "solana":
        scan = await solana_token_security(address)
        chain_label = "Solana"
    else:
        scan = await evm_token_security(chain, address)
        chain_label = CHAINS[chain]["label"]
    out = dict(scan)
    out.update(
        {
            "security": True,
            "chain_label": chain_label,
            "api_url": (
                f"/api/walletscope/token/security?chain={chain}"
                f"&address={address}"
            ),
            "method": METHOD,
            "limits": LIMITS,
        }
    )
    return out
