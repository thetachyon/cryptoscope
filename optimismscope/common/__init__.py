"""Shared code for the cryptoscope *Scope apps.

Single source of truth for: the chain registry (`chains`), upstream
clients (`clients`), response formatting (`formatting`), USD pricing
(`pricing`), the Keccak/ABI codec (`abi`), the chain-parameterized
explorer core (`explorer`), cross-chain wallet aggregation (`wallet`),
and the thin-app factories (`factory`).

Vendored into each app (`<app>/common/`) by `scripts/sync-common.py`.
The vendored copies are GENERATED — never hand-edit them; edit here.
"""

__version__ = "1.0.0"
