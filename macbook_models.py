"""
macbook_models.py
Known MacBook part numbers — membership set + DB-backed read-through cache.

Design:
    1. STATIC set (MACBOOK_PART_NUMBERS) is the hot path — instant, no I/O.
       Holds the 5-character base part numbers (e.g. "MW0Y3"), uppercased.
    2. On a miss, the caller queries the DB (macbook_part_in_db in app.py).
    3. A DB hit is PROMOTED into the static set at runtime via remember_part(),
       so the next lookup for that part number skips the DB entirely.
       (Process-local read-through cache; resets on restart, re-warms from DB.)

This only answers "is this part number known?". The part number itself stays in
the product's `model_code` field — nothing is translated to a model name.

Part numbers are stored WITHOUT the region suffix. Apple lists them as
"MW0Y3xx/A" where xx = region (LL=US, RU=Russia, etc.). Incoming order text
typically gives only the 5-char base, so we normalize to that.
"""

from __future__ import annotations

import re
from typing import Optional


# Apple part-number shape validator (anchored, standalone code only).
#   base: 1-2 leading letters + 3-4 alphanumerics  -> "MW0Y3", "MGDR4"
#   optional region suffix: "LL/A", "T/A", "/A", "xx/A"
# Shape only — does NOT confirm the code is real. Rejects obvious non-codes
# (colors, prices, sizes, words) before they become garbage lookup keys.
_PART_VALIDATE_RE = re.compile(
    r'^[A-Za-z]{1,2}[A-Za-z0-9]{3,4}(?:[A-Za-z]{0,2}/[A-Za-z])?$'
)


def is_valid_part_shape(part: Optional[str]) -> bool:
    """True if `part` is shaped like an Apple part number. Shape only — not realness."""
    return bool(part and _PART_VALIDATE_RE.match(part.strip()))


# Unanchored scanner for pulling part-number candidates OUT of free text.
# Looser than the anchored validator: finds part-shaped tokens anywhere in a
# string. Returns raw matches; caller normalizes + confirms against the set.
_PART_SCAN_RE = re.compile(
    r'\b([A-Za-z]{1,2}[A-Za-z0-9]{3,4}(?:[A-Za-z]{0,2}/[A-Za-z])?)\b'
)


def extract_part_candidates(text: Optional[str]) -> list[str]:
    """
    Pull all part-number-shaped tokens from free text, normalized to 5-char base,
    de-duplicated, order preserved. Shape only — does NOT confirm realness.
    Many tokens will be false positives (words, other codes); the caller filters
    against the known set.
    """
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for raw in _PART_SCAN_RE.findall(text):
        key = _norm_part(raw)
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def _norm_part(part: Optional[str]) -> str:
    """
    Normalize a raw part number to the 5-char base key.
      "MW0Y3LL/A" -> "MW0Y3"
      "mw0y3"     -> "MW0Y3"
      "MDHA4xx/A" -> "MDHA4"
    """
    if not part:
        return ""
    p = part.strip().upper().split("/")[0]                   # "MW0Y3LL" from "MW0Y3LL/A"
    return "".join(ch for ch in p if ch.isalnum())[:5]       # first 5 alnum chars


# ── STATIC SEED ───────────────────────────────────────────────────────────────
# Known part-number bases. Membership only — no values. 155 entries, sorted.
# Codes not listed here fall through to the DB lookup and, on a hit, get
# promoted into this set at runtime.
MACBOOK_PART_NUMBERS: set[str] = {
    "MC654", "MC6A4", "MC6C4", "MC6J4", "MC6K4", "MC6L4",
    "MC6T4", "MC6U4", "MC6V4", "MC7A4", "MC7C4", "MC7D4",
    "MC7U4", "MC7V4", "MC7W4", "MC7X4", "MC8G4", "MC8H4",
    "MC8J4", "MC8K4", "MC8M4", "MC8N4", "MC8P4", "MC8Q4",
    "MC9D4", "MC9E4", "MC9F4", "MC9G4", "MC9H4", "MC9J4",
    "MC9K4", "MC9L4", "MCX04", "MCX14", "MDE04", "MDE14",
    "MDE34", "MDE44", "MDE54", "MDE64", "MGN63", "MGN93",
    "MGND3", "MK183", "MK193", "MK1A3", "MK1E3", "MK1F3",
    "MK1H3", "MKGP3", "MKGQ3", "MKGR3", "MKGT3", "MLXW3",
    "MLXX3", "MLXY3", "MLY03", "MLY13", "MLY23", "MLY33",
    "MLY43", "MNEH3", "MNEJ3", "MNEP3", "MNEQ3", "MNW83",
    "MNW93", "MNWA3", "MNWC3", "MNWD3", "MNWE3", "MPHE3",
    "MPHF3", "MPHG3", "MPHH3", "MPHJ3", "MPHK3", "MQKP3",
    "MQKQ3", "MQKR3", "MQKT3", "MQKU3", "MQKV3", "MQKW3",
    "MQKX3", "MR7K3", "MRW13", "MRW23", "MRW43", "MRW73",
    "MRX33", "MRX43", "MRX63", "MRX73", "MRXN3", "MRXP3",
    "MRXQ3", "MRXT3", "MRXU3", "MRXV3", "MRXW3", "MRYM3",
    "MRYN3", "MRYP3", "MRYQ3", "MRYR3", "MRYT3", "MRYU3",
    "MRYV3", "MTL73", "MTL83", "MUW63", "MW0W3", "MW0X3",
    "MW0Y3", "MW103", "MW123", "MW133", "MW1G3", "MW1H3",
    "MW1J3", "MW1K3", "MW1L3", "MW1M3", "MW2U3", "MW2V3",
    "MW2W3", "MW2X3", "MX2E3", "MX2F3", "MX2G3", "MX2H3",
    "MX2J3", "MX2K3", "MX2T3", "MX2U3", "MX2V3", "MX2W3",
    "MX2X3", "MX2Y3", "MX303", "MX313", "MXCR3", "MXCT3",
    "MXCU3", "MXCV3", "MXD13", "MXD23", "MXD33", "MXD43",
    "MXE13", "MYD82", "MYD92", "MYDA2", "MYDC2",
}


def is_known_part(part: Optional[str]) -> bool:
    """Static-only membership check. True if the part number is in the set."""
    key = _norm_part(part)
    return bool(key) and key in MACBOOK_PART_NUMBERS


def remember_part(part: Optional[str]) -> None:
    """
    Promote a DB-confirmed part number into the static set so subsequent lookups
    skip the DB. Idempotent.
    """
    key = _norm_part(part)
    if key:
        MACBOOK_PART_NUMBERS.add(key)
