"""Response formatting for the *Scope apps — wei/ETH/token math, relative ages,
compact address objects, and short-form translators for blocks, txs,
token transfers, internal txs and NFT instances.

Pure functions; no network access. Hoist note: no FastAPI dependency.
"""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation



# ---------------------------------------------------------------- format helpers
def _wei_str(v) -> str:
    try:
        return str(int(str(v)))
    except (ValueError, TypeError):
        return "0"



def _hex_int(v) -> int:
    """Parse a hex (0x...) or decimal quantity to int; 0 on garbage."""
    try:
        s = str(v)
        return int(s, 16) if s.startswith(("0x", "0X")) else int(s)
    except (ValueError, TypeError):
        return 0



def wei_to_eth(v) -> str:
    """Wei (int-as-string) -> ETH decimal string, trailing zeros stripped."""
    try:
        d = Decimal(_wei_str(v)) / Decimal(10**18)
    except InvalidOperation:
        return "0"
    s = format(d.normalize(), "f")
    return s



def gwei(v) -> str:
    try:
        return f"{Decimal(_wei_str(v)) / Decimal(10**9):.2f}"
    except InvalidOperation:
        return "0.00"



def token_amount(value, decimals) -> str:
    try:
        dec = int(decimals or 0)
        d = Decimal(_wei_str(value)) / (Decimal(10) ** dec)
    except (InvalidOperation, ValueError):
        return "0"
    s = format(d.normalize(), "f")
    if "." in s:
        i, f = s.split(".")
        f = f[:6].rstrip("0")
        s = i + ("." + f if f else "")
    return s



def short(h: str) -> str:
    if not h or len(h) < 18:
        return h or ""
    return h[:10] + "..." + h[-8:]



def age(ts: str) -> str:
    """ISO timestamp -> '12 secs ago' style relative age."""
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        secs = int((datetime.now(timezone.utc) - dt).total_seconds())
    except Exception:
        return ts
    if secs < 0:
        secs = 0
    if secs < 60:
        return f"{secs} secs ago"
    mins = secs // 60
    if mins < 60:
        return f"{mins} mins ago"
    hrs = mins // 60
    if hrs < 24:
        return f"{hrs} hrs {mins % 60} mins ago"
    days = hrs // 24
    return f"{days} days ago"



def addr_info(a) -> dict | None:
    """Compact address object: hash + display label + flags."""
    if not a or not isinstance(a, dict):
        return None
    h = a.get("hash") or ""
    label = a.get("ens_domain_name") or a.get("name") or h
    return {
        "hash": h,
        "label": label,
        "short": short(h) if label == h else label,
        "is_contract": bool(a.get("is_contract")),
        "is_scam": bool(a.get("is_scam")),
    }



def tx_short(t: dict) -> dict:
    return {
        "hash": t.get("hash"),
        "block": t.get("block"),
        "timestamp": t.get("timestamp"),
        "age": age(t.get("timestamp")),
        "from": addr_info(t.get("from")),
        "to": addr_info(t.get("to")),
        "value_eth": wei_to_eth(t.get("value")),
        "fee_eth": wei_to_eth((t.get("fee") or {}).get("value")) if isinstance(t.get("fee"), dict) else "0",
        "gas_price_gwei": gwei(t.get("gas_price")),
        "status": t.get("result") or t.get("status"),
        "method": t.get("method"),
        "type": t.get("type"),
    }



def rpc_tx_short(t: dict) -> dict:
    """tx_short shape for a raw JSON-RPC transaction object (pending block)."""
    return {
        "hash": t.get("hash"),
        "block": None,
        "timestamp": None,
        "age": "",
        "from": addr_info({"hash": t.get("from")}) if t.get("from") else None,
        "to": addr_info({"hash": t.get("to")}) if t.get("to") else None,
        "value_eth": wei_to_eth(_hex_int(t.get("value"))),
        "fee_eth": "0",
        "gas_price_gwei": f"{_hex_int(t.get('gasPrice')) / 10**9:.2f}" if t.get("gasPrice") else "0.00",
        "status": "pending",
        "method": None,
        "type": t.get("type"),
        "gas": _hex_int(t.get("gas")),
        "nonce": _hex_int(t.get("nonce")),
    }



def block_short(b: dict) -> dict:
    return {
        "height": b.get("height"),
        "hash": b.get("hash"),
        "timestamp": b.get("timestamp"),
        "age": age(b.get("timestamp")),
        "miner": addr_info(b.get("miner")),
        "tx_count": b.get("transactions_count"),
        "gas_used": b.get("gas_used"),
        "gas_limit": b.get("gas_limit"),
        "gas_used_pct": round(float(b.get("gas_used_percentage") or 0), 2),
        "base_fee_gwei": gwei(b.get("base_fee_per_gas")),
        "burnt_fees_eth": wei_to_eth(b.get("burnt_fees")),
        "rewards": [
            {"recipient": r.get("recipient"), "type": r.get("type"), "value_eth": wei_to_eth(r.get("reward"))}
            for r in (b.get("rewards") or [])
        ],
        "size": b.get("size"),
        "withdrawals_count": b.get("withdrawals_count"),
        "blob_tx_count": b.get("blob_transactions_count"),
    }



def token_transfer_short(tt: dict) -> dict:
    tok = tt.get("token") or {}
    total = tt.get("total") or {}
    dec = total.get("decimals") if total.get("decimals") is not None else tok.get("decimals")
    return {
        "tx_hash": tt.get("transaction_hash"),
        "block": tt.get("block_number"),
        "timestamp": tt.get("timestamp"),
        "age": age(tt.get("timestamp")),
        "from": addr_info(tt.get("from")),
        "to": addr_info(tt.get("to")),
        "token": {
            "address": tok.get("address_hash") or tok.get("address"),
            "name": tok.get("name"),
            "symbol": tok.get("symbol"),
            "decimals": dec,
            "type": tok.get("type"),
            "icon_url": tok.get("icon_url"),
        },
        "amount": token_amount(total.get("value"), dec),
        "type": tt.get("type"),
    }



def internal_tx_short(i: dict) -> dict:
    return {
        "tx_hash": i.get("transaction_hash"),
        "block": i.get("block_number"),
        "timestamp": i.get("timestamp"),
        "age": age(i.get("timestamp")),
        "from": addr_info(i.get("from")),
        "to": addr_info(i.get("to")),
        "value_eth": wei_to_eth(i.get("value")),
        "gas_limit": i.get("gas_limit"),
        "type": i.get("type"),
        "success": i.get("success"),
        "error": i.get("error"),
        "created_contract": i.get("created_contract", {}).get("hash") if isinstance(i.get("created_contract"), dict) else None,
    }



def decode_input(hexstr: str | None) -> str | None:
    """Return UTF-8 text of tx input if it is printable, else None."""
    if not hexstr or hexstr == "0x":
        return None
    try:
        raw = bytes.fromhex(hexstr[2:] if hexstr.startswith("0x") else hexstr)
        txt = raw.decode("utf-8")
    except Exception:
        return None
    if txt and all(32 <= ord(c) < 127 or c in "\n\r\t" for c in txt):
        return txt
    return None



def _instance_short(inst: dict) -> dict:
    return {
        "id": inst.get("id"),
        "image_url": inst.get("image_url"),
        "media_url": inst.get("media_url"),
        "media_type": inst.get("media_type"),
        "animation_url": inst.get("animation_url"),
        "external_app_url": inst.get("external_app_url"),
        "is_unique": inst.get("is_unique"),
        "metadata": inst.get("metadata"),
        "owner": addr_info(inst.get("owner")) if isinstance(inst.get("owner"), dict) else None,
        "token": (
            {
                "address": (inst.get("token") or {}).get("address_hash") or (inst.get("token") or {}).get("address"),
                "name": (inst.get("token") or {}).get("name"),
                "symbol": (inst.get("token") or {}).get("symbol"),
                "type": (inst.get("token") or {}).get("type"),
            }
            if isinstance(inst.get("token"), dict)
            else None
        ),
    }
