"""Keccak-256 and a minimal ABI codec (Ethereum types).

Pure-python; stdlib `hashlib.sha3_256` is NOT Keccak-256, hence the
hand-rolled permutation. Used by contract Read calls (function selectors,
argument encoding, output decoding) and mirrored in the frontend
(`static/app.js` has its own JS copy, unit-tested byte-exact against
this module).
"""
import re


# ---------------------------------------------------------------- keccak-256 (pure python; stdlib sha3 is NOT keccak)
_KECCAK_RC = [
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A,
    0x8000000080008000, 0x000000000000808B, 0x0000000080000001,
    0x8000000080008081, 0x8000000000008009, 0x000000000000008A,
    0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089,
    0x8000000000008003, 0x8000000000008002, 0x8000000000000080,
    0x000000000000800A, 0x800000008000000A, 0x8000000080008081,
    0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
]
_KECCAK_R = [
    [0, 36, 3, 41, 18],
    [1, 44, 10, 45, 2],
    [62, 6, 43, 15, 61],
    [28, 55, 25, 21, 56],
    [27, 20, 39, 8, 14],
]
_KECCAK_MASK = (1 << 64) - 1


def _rol(x, n):
    return ((x << n) | (x >> (64 - n))) & _KECCAK_MASK if n else x & _KECCAK_MASK


def _keccak_f1600(a):
    for rnd in range(24):
        c = [a[x] ^ a[x + 5] ^ a[x + 10] ^ a[x + 15] ^ a[x + 20] for x in range(5)]
        d = [c[(x - 1) % 5] ^ _rol(c[(x + 1) % 5], 1) for x in range(5)]
        a = [a[x + 5 * y] ^ d[x] for y in range(5) for x in range(5)]
        b = [0] * 25
        for x in range(5):
            for y in range(5):
                b[y + 5 * ((2 * x + 3 * y) % 5)] = _rol(a[x + 5 * y], _KECCAK_R[x][y])
        a = [
            b[x + 5 * y] ^ ((~b[(x + 1) % 5 + 5 * y]) & b[(x + 2) % 5 + 5 * y])
            for y in range(5)
            for x in range(5)
        ]
        a[0] ^= _KECCAK_RC[rnd]
    return a


def keccak_256(data: bytes) -> bytes:
    """Keccak-256 (Ethereum's hash, not NIST SHA3-256)."""
    rate = 136  # bytes, for 512-bit capacity
    state = [0] * 25
    # pad10*1 with domain byte 0x01
    padded = bytearray(data)
    padded.append(0x01)
    while len(padded) % rate != rate - 1:
        padded.append(0x00)
    padded.append(0x80)
    for off in range(0, len(padded), rate):
        block = padded[off:off + rate]
        for i in range(rate // 8):
            state[i] ^= int.from_bytes(block[i * 8:(i + 1) * 8], "little")
        state = _keccak_f1600(state)
    return b"".join(state[i].to_bytes(8, "little") for i in range(4))


# ---------------------------------------------------------------- minimal ABI codec
_TYPE_RE = re.compile(r"^([a-zA-Z]+)([0-9]*)((?:\[\d*\])*)$")


def _parse_type(t: str):
    """'uint256[2][]' -> ('uint256', [2, None]); raises ValueError."""
    m = _TYPE_RE.match(t or "")
    if not m:
        raise ValueError(f"Unsupported ABI type: {t!r}")
    base = m.group(1) + m.group(2)
    dims = [int(x) if x else None for x in re.findall(r"\[(\d*)\]", m.group(3))]
    if base not in (
        "address", "bool", "string", "bytes",
        *[f"uint{n}" for n in range(8, 257, 8)],
        *[f"int{n}" for n in range(8, 257, 8)],
        *[f"bytes{n}" for n in range(1, 33)],
    ):
        raise ValueError(f"Unsupported ABI type: {t!r}")
    return base, dims


def _join_type(base, dims):
    return base + "".join(f"[{d if d is not None else ''}]" for d in dims)


def _is_dynamic(base, dims) -> bool:
    if dims:
        if dims[-1] is None:
            return True
        return _is_dynamic(base, dims[:-1])
    return base in ("string", "bytes")


def _static_size(base, dims) -> int:
    if dims:
        return dims[-1] * _static_size(base, dims[:-1])
    return 32


def _coerce_int(v, bits, signed):
    if isinstance(v, bool):
        raise ValueError("Boolean is not an integer argument")
    try:
        n = int(str(v), 0) if isinstance(v, str) and str(v).startswith(("0x", "0X")) else int(v)
    except (ValueError, TypeError):
        raise ValueError(f"Not an integer: {v!r}")
    lo, hi = (-(1 << (bits - 1)), (1 << (bits - 1)) - 1) if signed else (0, (1 << bits) - 1)
    if not lo <= n <= hi:
        raise ValueError(f"Integer {n} out of range for {'int' if signed else 'uint'}{bits}")
    return n


def _encode_static(base, dims, v) -> bytes:
    if dims:
        if not isinstance(v, (list, tuple)) or len(v) != dims[-1]:
            raise ValueError(f"Expected array of length {dims[-1]} for {_join_type(base, dims)}")
        elem = _join_type(base, dims[:-1])
        eb, ed = _parse_type(elem)
        return b"".join(_encode_static(eb, ed, item) for item in v)
    if base == "address":
        s = str(v)
        if not re.fullmatch(r"0x[0-9a-fA-F]{40}", s):
            raise ValueError(f"Not an address: {v!r}")
        return bytes(12) + bytes.fromhex(s[2:])
    if base.startswith("uint"):
        return _coerce_int(v, int(base[4:]), False).to_bytes(32, "big")
    if base.startswith("int"):
        n = _coerce_int(v, int(base[3:]), True)
        return (n % (1 << 256)).to_bytes(32, "big")
    if base == "bool":
        if not isinstance(v, bool):
            raise ValueError(f"Not a boolean: {v!r}")
        return (1 if v else 0).to_bytes(32, "big")
    if base.startswith("bytes") and base != "bytes":
        n = int(base[5:])
        s = v.hex() if isinstance(v, (bytes, bytearray)) else str(v)
        if s.startswith(("0x", "0X")):
            s = s[2:]
        try:
            raw = bytes.fromhex(s)
        except ValueError:
            raise ValueError(f"Not hex bytes: {v!r}")
        if len(raw) != n:
            raise ValueError(f"Expected {n} bytes for {base}, got {len(raw)}")
        return raw + bytes(32 - n)
    raise ValueError(f"Cannot statically encode {base}")


def _encode_dynamic(base, dims, v) -> bytes:
    if dims and dims[-1] is None:
        if not isinstance(v, (list, tuple)):
            raise ValueError(f"Expected array for {_join_type(base, dims)}")
        elem = _join_type(base, dims[:-1])
        body = _encode_args([elem] * len(v), list(v))
        return len(v).to_bytes(32, "big") + body
    if base == "string":
        raw = str(v).encode("utf-8")
        return len(raw).to_bytes(32, "big") + raw + bytes((-len(raw)) % 32)
    if base == "bytes":
        s = v.hex() if isinstance(v, (bytes, bytearray)) else str(v)
        if s.startswith(("0x", "0X")):
            s = s[2:]
        try:
            raw = bytes.fromhex(s)
        except ValueError:
            raise ValueError(f"Not hex bytes: {v!r}")
        return len(raw).to_bytes(32, "big") + raw + bytes((-len(raw)) % 32)
    raise ValueError(f"Cannot dynamically encode {base}")


def _encode_args(types, values) -> bytes:
    if len(types) != len(values):
        raise ValueError(f"Expected {len(types)} argument(s), got {len(values)}")
    heads, tails = [], []
    offset = 32 * len(types)
    for t, v in zip(types, values):
        base, dims = _parse_type(t)
        if _is_dynamic(base, dims):
            heads.append(offset.to_bytes(32, "big"))
            tail = _encode_dynamic(base, dims, v)
            tails.append(tail)
            offset += len(tail)
        else:
            heads.append(_encode_static(base, dims, v))
    return b"".join(heads + tails)


def _decode_static(base, dims, data, pos):
    if dims:
        sz = _static_size(base, dims[:-1])
        return [
            _decode_static(base, dims[:-1], data, pos + i * sz)
            for i in range(dims[-1])
        ]
    word = data[pos:pos + 32]
    if len(word) < 32:
        raise ValueError("Output too short")
    if base == "address":
        return "0x" + word[12:].hex()
    if base.startswith("uint"):
        return str(int.from_bytes(word, "big"))
    if base.startswith("int"):
        n = int.from_bytes(word, "big")
        bits = int(base[3:])
        if n >= 1 << (bits - 1):
            n -= 1 << bits
        return str(n)
    if base == "bool":
        return word[-1] != 0
    if base.startswith("bytes") and base != "bytes":
        return "0x" + word[:int(base[5:])].hex()
    raise ValueError(f"Cannot decode {base}")


def _decode_dynamic(base, dims, data, pos):
    if dims and dims[-1] is None:
        n = int.from_bytes(data[pos:pos + 32], "big")
        body = pos + 32
        return [
            _decode_at(_join_type(base, dims[:-1]), data, body + 32 * i, body)
            for i in range(n)
        ]
    n = int.from_bytes(data[pos:pos + 32], "big")
    raw = data[pos + 32:pos + 32 + n]
    if len(raw) < n:
        raise ValueError("Output too short")
    if base == "string":
        return raw.decode("utf-8", "replace")
    return "0x" + raw.hex()


def _decode_at(t, data, pos, origin):
    base, dims = _parse_type(t)
    if _is_dynamic(base, dims):
        off = int.from_bytes(data[pos:pos + 32], "big")
        return _decode_dynamic(base, dims, data, origin + off)
    return _decode_static(base, dims, data, pos)


def _decode_args(types, data: bytes):
    return [_decode_at(t, data, 32 * i, 0) for i, t in enumerate(types)]


def _function_selector(name, input_types) -> bytes:
    sig = f"{name}({','.join(input_types)})"
    return keccak_256(sig.encode())[:4]


