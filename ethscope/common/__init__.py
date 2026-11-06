"""Shared code for the cryptoscope *Scope apps.

Single source of truth for: the chain registry (`chains`), upstream
clients (`clients`), response formatting (`formatting`), USD pricing
(`pricing`), the Keccak/ABI codec (`abi`), the chain-parameterized
explorer core (`explorer`), cross-chain wallet aggregation (`wallet`),
Solana data (`solana`, `solana_explorer`), Bitcoin data (`bitcoin`),
stablecoin dashboards (`stablecoin`), the cross-chain overview
(`overview`), address risk scoring (`risk`), plain-English transaction
explanations (`explain`), token security scanning (`security`),
cross-chain identity (`crosschain`), and the thin-app factories
(`factory`).

Vendored into each app (`<app>/common/`) by `scripts/sync-common.py`.
The vendored copies are GENERATED — never hand-edit them; edit here.
"""

__version__ = "1.0.0"
