"""
app.py — Production-ready single API
Run: uvicorn app:app --host 0.0.0.0 --port 8000

Single endpoint: GET /parse?text=...
  1. LLM extracts products from raw text
  2. SIM type resolved in Python (iPhones only)
  3. DB queried for color candidates (iPhones only)
  4. LLM picks best color match (iPhones only)
  Returns flat JSON array of enriched products.
"""

import asyncio
import json
import logging
import os
import re   # === FIX 1 (added): used by _strip_order_headers and chunk pairing ===
import sys
from contextlib import asynccontextmanager
from typing import Optional

import asyncpg
import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, field_validator

from sim_rules import SimCardType, resolve_sim_type
from color_overrides import OVERRIDES as COLOR_OVERRIDES
from macbook_models import is_known_part, remember_part, extract_part_candidates

load_dotenv()

# ══════════════════════════════════════════════════════════════════════════════
# ── CONFIG — adjust these values ─────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

# LLM provider — configure in config.py (switch between Anthropic and OpenAI)
from config import LLM_PROVIDER, LLM_API_URL, LLM_API_KEY, LLM_MODEL, LLM_TIMEOUT

# Postgres
DATABASE_URL = (
    f"postgresql://{os.getenv('DB_USER')}:{os.getenv('DB_PASSWORD')}"
    f"@{os.getenv('DB_HOST', 'localhost')}:{os.getenv('DB_PORT', '5432')}"
    f"/{os.getenv('DB_NAME')}"
)
DB_POOL_MIN  = 2    # min connections in pool
DB_POOL_MAX  = 10   # max connections in pool

# DB query — adjust table/column names to match your schema
# DB query — adjust table/column names to match your schema
DB_TABLE              = "public.products"
DB_COL_PRODUCT_LINE   = "product_type"
DB_COL_MODEL_NAME     = "model"
DB_COL_MODEL_NAME_CODE = "model_code"   # SKU / Apple part number column (e.g. "MW0Y3")
DB_COL_CATEGORY       = "category"
DB_COL_STORAGE        = "size"
DB_COL_COUNTRY        = "country_code"
DB_COL_SIM_TYPE       = "sim_card_type"
DB_COL_COLOR          = "color"

# ══════════════════════════════════════════════════════════════════════════════


# ── Logging ───────────────────────────────────────────────────────────────────

from datetime import datetime as _dt

def _ts() -> str:
    return _dt.now().strftime("%Y-%m-%d %H:%M:%S")

_uvicorn_logger = logging.getLogger("uvicorn.error")
_uvicorn_logger.setLevel(logging.INFO)

class _Logger:
    def info(self, msg, *args):
        _uvicorn_logger.info(f"[{_ts()}] " + (msg % args if args else msg))
    def warning(self, msg, *args):
        _uvicorn_logger.warning(f"[{_ts()}] " + (msg % args if args else msg))
    def error(self, msg, *args):
        _uvicorn_logger.error(f"[{_ts()}] " + (msg % args if args else msg))

logger = _Logger()

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


# ── Input normalization ───────────────────────────────────────────────────────

# Collapses any run of 2+ consecutive newlines (optionally with whitespace
# between them, like "\n   \n\n") down to a single newline — i.e. removes
# all blank lines entirely. Lines end up stacked directly on top of each other.
# Why: the LLM tends to hallucinate when fed empty lines, treating the gap as
# a structural break and inventing missing items.
_MULTI_BLANK_LINES_RE = re.compile(r"(?:[ \t]*\n){2,}")

def _normalize_blank_lines(text: str) -> str:
    """Remove all blank/whitespace-only lines, leaving only a single \\n between content lines."""
    return _MULTI_BLANK_LINES_RE.sub("\n", text)


# Collapses duplicated eSIM tokens like "eSim + eSIM" → "eSIM". Users sometimes
# paste the same eSIM token twice ("eSim + eSIM", "esim/esim", "есим-есим"),
# which the LLM reads as the "sim+esim" combo and labels PHYSICAL_PLUS_ESIM
# when the correct answer is ESIM_ONLY_SINGLE.
# Only collapses contiguous eSIM forms (esim, e-sim, есим, е-сим). Leaves
# "e sim + esim" (internal space — treated as physical sim + esim) and
# "sim + esim" (legitimate combo) alone.
_DUP_ESIM_RE = re.compile(
    r"\b(?:e-?sim|есим|е-?сим)\s*[+/\-]\s*(?:e-?sim|есим|е-?сим)\b",
    re.IGNORECASE,
)

def _normalize_duplicated_esim(text: str) -> str:
    """Collapse duplicate eSIM tokens like 'eSim + eSIM' → 'eSIM'."""
    while True:
        new = _DUP_ESIM_RE.sub("eSIM", text)
        if new == text:
            return new
        text = new




 # lowercase the whole input 
def _lowercase_for_llm(text: str) -> str:
    """Lowercase everything before sending to the LLM. Experimental — may hurt
    proper-noun extraction (iPhone, Sierra Blue, (PRODUCT) RED) since these
    appear in canonical casing in the model's training data."""
    return text.lower()
           






# ── Pydantic models ───────────────────────────────────────────────────────────

class EnrichedProduct(BaseModel):
    # Core fields (matches legacy format)
    productName:      Optional[str] = None
    model:            Optional[str] = None
    size:             Optional[str] = None
    color_from_text: Optional[str] = None
    # Order info
    quantity:         int           = 1
    price:            float         = 0.0
    requestedText:    str
    countryCode:      Optional[str] = None
    # SIM (iPhones only)
    simType:          Optional[SimCardType] = None
    simConflict:      bool          = False
    # LLM extracted fields
    brand:        Optional[str] = None
    product_type:     Optional[str] = None
    category:     Optional[str] = None
    variant:      Optional[str] = None
    model_code:   Optional[str] = None
    ram:          Optional[str] = None
    LLM_color_en:     Optional[str] = None
    prod_year:         Optional[str] = None
    # Color match (iPhones only)
    matched_color:    Optional[str] = None


# === FIX 4 (added): Step-1 contract — what the LLM produces. ============================
# Why this exists separately from EnrichedProduct:
#   * EnrichedProduct is the API OUTPUT contract — it includes downstream-computed fields
#     (`simConflict`, `matched_color`) that the LLM never sees, and excludes the
#     `is_real_product` flag the LLM uses to mark non-product noise.
#   * LLMExtractedProduct is the LLM CONTRACT — exactly the 18 fields the prompt asks for.
#     Used in two places:
#       1. As the source of truth for the OpenAI strict JSON schema (FIX 3),
#       2. To validate every raw dict the LLM returns BEFORE the filter logic runs,
#          so type errors (e.g. quantity="8" string) surface as retryable failures
#          instead of crashing enrich() later.
#   * Decoupling the two models prevents one from drifting: the prompt's field list,
#     the OpenAI schema, and the runtime validator all derive from this single class.
class LLMExtractedProduct(BaseModel):
    is_real_product: bool
    productName:     Optional[str] = None
    model:           Optional[str] = None
    size:            Optional[str] = None
    color_from_text: Optional[str] = None
    quantity:        int           = 1
    price:           float         = 0.0
    requestedText:   str
    countryCode:     Optional[str] = None
    brand:           Optional[str] = None
    product_type:    Optional[str] = None
    category:        Optional[str] = None
    variant:         Optional[str] = None
    model_code:      Optional[str] = None
    ram:             Optional[str] = None
    LLM_color_en:    Optional[str] = None
    prod_year:       Optional[str] = None
    simType:         Optional[SimCardType] = None


def _pydantic_to_openai_strict_schema(
    pyd_model: type[BaseModel],
    wrap_in_array: bool = True,
) -> dict:
    """
    Convert a Pydantic v2 model into an OpenAI structured-outputs strict schema.

    OpenAI's strict mode has stricter requirements than Pydantic's default schema:
      1. Every property must appear in `required` (optional → nullable).
      2. `additionalProperties: false` on every object.
      3. No `$ref` / `$defs` indirection at the schema root — must inline.
      4. `anyOf: [{...}, {"type":"null"}]` from Optional[...] must collapse to
         {"type": ["X", "null"]} where possible.
      5. The top-level schema MUST be an object — so when wrap_in_array=True we wrap the
         model under "products" (the Stage 2 case: many product items).

    wrap_in_array=False is for response-envelope models that are ALREADY a top-level
    object (e.g. LLMSegmentResponse with its built-in `segments` field). Wrapping those
    again would produce {"products":[{"segments":[...]}]} — the bug fixed in FIX 7.
    """
    raw = pyd_model.model_json_schema()

    # Inline any $defs (e.g. for the SimCardType enum)
    defs = raw.pop("$defs", {}) or raw.pop("definitions", {}) or {}

    def _resolve(node):
        if isinstance(node, dict):
            # Resolve $ref
            if "$ref" in node:
                ref_name = node["$ref"].rsplit("/", 1)[-1]
                resolved = _resolve(defs.get(ref_name, {}))
                # Merge sibling keys (rare, but Pydantic emits "title" alongside $ref)
                merged = {**resolved, **{k: v for k, v in node.items() if k != "$ref"}}
                return merged
            # Collapse anyOf [{type:X}, {type:null}] → {type:[X,"null"]}
            if "anyOf" in node and isinstance(node["anyOf"], list):
                variants = [_resolve(v) for v in node["anyOf"]]
                non_null = [v for v in variants if v.get("type") != "null"]
                has_null = any(v.get("type") == "null" for v in variants)
                if has_null and len(non_null) == 1:
                    base = dict(non_null[0])
                    t = base.get("type")
                    if isinstance(t, str):
                        base["type"] = [t, "null"]
                    elif isinstance(t, list) and "null" not in t:
                        base["type"] = list(t) + ["null"]
                    # Carry enum from non-null branch and add null if missing
                    if "enum" in base and None not in base["enum"]:
                        base["enum"] = list(base["enum"]) + [None]
                    # Drop bookkeeping keys OpenAI doesn't want
                    base.pop("title", None)
                    sibling = {k: v for k, v in node.items() if k not in ("anyOf", "title", "default")}
                    return {**base, **sibling}
            return {k: _resolve(v) for k, v in node.items() if k not in ("title", "default")}
        if isinstance(node, list):
            return [_resolve(v) for v in node]
        return node

    resolved = _resolve(raw)

    # Walk objects and force strict-mode requirements
    def _strictify(node):
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node["properties"].keys())
                node["properties"] = {k: _strictify(v) for k, v in node["properties"].items()}
            else:
                node = {k: _strictify(v) for k, v in node.items()}
        elif isinstance(node, list):
            node = [_strictify(v) for v in node]
        return node

    item_schema = _strictify(resolved)

    # === FIX 7 (added): wrap_in_array gate. ===
    # wrap_in_array=True  → Stage 2: produce {"products": [item, item, ...]} envelope.
    # wrap_in_array=False → Stage 1: model IS the envelope (LLMSegmentResponse has its
    #                       own `segments` field). Use the schema as-is.
    if not wrap_in_array:
        return item_schema
    # === END FIX 7 ===

    # Wrap under "products" — OpenAI requires a top-level object.
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["products"],
        "properties": {
            "products": {
                "type": "array",
                "items": item_schema,
            }
        },
    }


# Build once at import time so we pay the conversion cost zero times per request.
_LLM_PRODUCT_SCHEMA = _pydantic_to_openai_strict_schema(LLMExtractedProduct, wrap_in_array=True)
# === END FIX 4 ===


# === FIX 6 (added): two-stage extraction — Stage 1 segmentation contract. ================
# WHY: the original single-call extractor collapses on long, multi-product inputs (the model
# tries to emit 50 fully-structured objects at once and either drops to "non-product" or
# truncates). The user's input formats vary too much for a deterministic regex splitter
# (FIX 5, removed) to be reliable.
#
# DESIGN: Stage 1 = LLM splits the input into per-product strings (cheap output, low risk).
#         Stage 2 = existing structured-output extractor, run in parallel batches over
#                   Stage 1's segments.
#
# This file's contract for Stage 1:
class LLMSegmentResponse(BaseModel):
    segments: list[str]


# === FIX 7 (call site): build segment schema WITHOUT the array wrapper. =================
# LLMSegmentResponse already has `segments` as its top-level field — wrapping it under
# `products` (the Stage 2 envelope) is what caused production to receive
# {"products":[{"segments":[...]}]} which then failed Pydantic validation.
_LLM_SEGMENT_SCHEMA = _pydantic_to_openai_strict_schema(LLMSegmentResponse, wrap_in_array=False)
# === END FIX 7 ===


# SEGMENT_PROMPT = """You are a text segmenter. Split the input into one chunk per product.

# RULES (strict):
# 1. Each chunk MUST be copied VERBATIM from the input. Do NOT paraphrase, normalize, fix typos, or change spacing/casing/punctuation. Preserve emojis, Cyrillic letters (e.g. "Prо" with Cyrillic 'о'), spacing, and punctuation EXACTLY as written.
# 2. Ignore order-status headers and metadata. Do NOT include them as segments. Examples of headers to ignore:
#    - "Заказ №..., Принят в обработку"
#    - "Выдача сегодня - 27 April 2026, 00:07"
#    - "🕗" date/time stamps
#    - Standalone notes like "отложи", "хорошо", "берём"
# 3. If input contains exactly one product, return exactly one segment.
# 4. If input contains no products (only headers, notes, conversational text), return {"segments": []}.

# OUTPUT (strict JSON, no preamble, no markdown):
# {"segments": ["chunk 1 verbatim", "chunk 2 verbatim", ...]}
# """

SEGMENT_PROMPT = """You are a text segmenter. Your only job is to split the input into chunks, one per product or note. You do NOT decide what is or isn't a product — that happens in a later step.

═══ CORE PRINCIPLE ═══
Every non-whitespace character of the input MUST appear in exactly one segment. You are partitioning the input, not filtering it. If you find yourself wanting to drop, skip, or omit something — DON'T. Include it as its own segment and let the next step decide.

═══ PROCESSING ORDER ═══
Walk the input from START to END in a single pass. Read it like a stream: process the first character, then the next, then the next, and so on until you reach the end. Do NOT jump around. Do NOT pick out obvious products first and come back for the rest. Do NOT reorder.

The order of segments in your output MUST match the order they appear in the input. The first segment is whatever comes first in the input. The last segment is whatever comes last. No skipping forward, no skipping back.

As you walk the input, ask at each line break: "Does the next chunk start here, or does it continue the current segment?" Decide using the SEGMENT BOUNDARIES rules below, then move on. Never look ahead beyond what those rules require.

═══ RULES (strict) ═══

1. VERBATIM COPYING
   Each segment MUST be copied character-for-character from the input. Do NOT paraphrase, translate, normalize, fix typos, change casing, change spacing, or remove punctuation. Preserve emojis, flag characters, Cyrillic letters (including look-alikes like "Prо" where 'о' is Cyrillic), and all whitespace within a segment EXACTLY as written.

2. NEVER OMIT
   Do NOT drop any line, phrase, token, number, emoji, or word — not even headers, notes, dates, or text that looks meaningless. Everything goes into a segment. If a line looks like a note, header, agreement phrase, or junk, give it its own segment anyway. The next step decides whether it's a real product.

3. SEGMENT BOUNDARIES
   Segments are normally one full line each. Use these specific rules to decide where to cut:

   3a. A trailing quantity suffix BELONGS TO THE LINE IT FOLLOWS.
       Patterns like " - 2", " -3", " - 5 ", "-N" (a hyphen plus a small integer at the END of a line) are the quantity for that line's product. They MUST stay attached. NEVER split before such a suffix. NEVER start a new segment with a bare number that came from the previous line's tail.

       CORRECT:   ["<model> <storage> <color> <flag> <price> - 2", "<model> <storage> <color> <flag> <price>"]
       WRONG:     ["<model> <storage> <color> <flag> <price>", "- 2", "<model> <storage> <color> <flag> <price>"]
       WRONG:     ["<model> <storage> <color> <flag> <price>", "2 <model> <storage> <color> <flag> <price>"]

   3b. A segment must not be a BARE ORPHAN QUANTITY. A bare orphan is a segment whose entire content is just a small number, a hyphen-number, or "- N" with no product information attached. These are quantity tails that got severed from the product they belong to.

       The test for any segment that STARTS with a number: does the rest of the segment contain product context — a storage size (GB / TB / ГБ / ТБ), a color word, a country/flag emoji, or a price (a large 4+ digit number)?
         - YES → it's a real product line that legitimately starts with its model number. Keep it as-is.
         - NO  → the segment is just an orphan quantity. You split wrong. Merge it back onto the previous segment.

       Apply the same test if the segment starts with "- N" or "-N": if no product context follows, it's an orphan.

   3c. Order headers and metadata get their OWN segments. Do not merge them with the product that follows. Examples:
       - "Заказ №..., Принят в обработку"
       - "Выдача сегодня - 27 April 2026, 00:07"
       - "🕗 ..." date/time stamps

   3d. Standalone notes get their OWN segments. Do not merge them with adjacent products. Examples:
       - Agreement/intent: "хорошо", "ок", "го", "берём", "беру", "возьму", "возьму так", "возьму так."
       - Deferral: "отложи", "отложите", "потом", "позже"

4. ONE PRODUCT PER SEGMENT
   Never merge two products into one segment. Never split one product across two segments. If a product spans two lines (e.g. specs on line A, "<qty> шт х <price> руб." on line B), include BOTH lines in the same segment.

5. UNCERTAIN? OVER-SPLIT.
   If you're unsure whether two adjacent lines belong together, return them as separate segments. Over-splitting is recoverable in the next step; merging is not.

═══ SELF-CHECK BEFORE OUTPUTTING ═══

Before returning, verify:
  (a) Concatenating your segments back together (with newlines between them) reproduces the input with no loss. Every word, number, emoji, and punctuation mark from the input must appear in some segment.
  (b) The segments appear in the same order as the input — segment[0] starts at the beginning of the input, segment[-1] ends at the end of the input, and no later segment refers to text that appears before an earlier segment.
  (c) No segment is a bare orphan quantity — i.e. no segment consists only of a small number, "-N", or "- N" with no storage/color/flag/price following.
  (d) The number of segments matches the number of distinct items (products + headers + notes) you can count in the input. If the input has 20 product lines and 1 header and 1 trailing note, you should produce 22 segments.

If any check fails, fix the segmentation and re-check.

═══ OUTPUT (strict JSON, no preamble, no markdown) ═══

{"segments": ["chunk 1 verbatim", "chunk 2 verbatim", ...]}
"""








SEGMENT_PROMPT_RETRY = """You are a text segmenter. The previous attempt failed. Be more careful.

CRITICAL RULES:
1. The input contains MULTIPLE products. Your job is to split them into separate strings.
2. Each segment MUST appear as a substring of the input, character-for-character (excepting whitespace). If you cannot find a segment in the input verbatim, you have hallucinated — do not include it.
3. A typical product spans a model name, optional storage/color, optional flag emojis, and a quantity/price token. Quantity/price tokens look like "<number> шт х <number> руб." or similar.
4. Do NOT merge multiple products into one segment. Do NOT split one product into multiple segments. If you see 50 quantity tokens, return 50 segments.
5. Ignore order headers (Заказ, Принят в обработку, Выдача, 🕗 dates).

OUTPUT (strict JSON, no preamble, no markdown):
{"segments": ["chunk 1 verbatim", "chunk 2 verbatim", ...]}

Return MORE segments rather than fewer if uncertain — over-splitting is recoverable, collapsing is not.
"""
# === END FIX 6 ===


# ── Step 1 prompt ─────────────────────────────────────────────────────────────

EXTRACT_PROMPT = """You are a product extraction assistant. Follow ALL instructions carefully and precisely.

=== STEP 1 — THINK FIRST (do NOT output this) ===
Before writing any JSON, mentally identify:
1. How many real products are in the text?
2. What raw text belongs to each product?
3. Is each segment actually a real product listing, or just a note/comment/instruction?
   - Real product: has a recognizable product name, model, or SKU (e.g. "17 Pro 256GB Black 54000")
   - NOT a product: conversational notes, instructions, prices alone, Russian words like "отложи"/"хорошо"/"привет"/"берём"/"го"/"ок", standalone numbers without context

=== MULTI-PRODUCT INPUTS (IMPORTANT) ===
Input often contains an order-status header (e.g. "Заказ №..., Принят в обработку, Выдача сегодня...")
followed by MANY product lines. The header is NOT a product — IGNORE it entirely and do NOT emit an
object for it. The product lines that follow ARE real products — emit ONE object per product.
A typical product spans 2 lines:
  Line A: <model> <storage> <color> <flag(s)> [sim info]
  Line B: <qty> шт х <price> руб. = <total> руб.
Treat each such pair as one product. NEVER merge multiple products into one object.
NEVER return a single object covering the whole input. If you see 50 product lines, emit 50 objects.

=== STEP 2 — OUTPUT ===
Return ONLY a JSON array. Response MUST start with "[" and end with "]". No explanations, no markdown, no preamble.
Every object MUST have ALL fields listed below in EXACTLY this order. Never omit a field. Use null if not determinable.

=== FIELD DEFINITIONS (use this exact order in every object) ===
{
  "is_real_product":  true,   // boolean — true if this is a real product listing, false if it is a note/comment/instruction/non-product text
  "productName":      "...",  // Full constructed name: brand + line + model + storage + color. e.g. "iPhone 17 256GB Black" | null if not a real product
  "model":            "...",  // iPhone: "16+" → "16 Plus" | "17" | "16 Pro" | "17 Pro Max" | "17 Air" | "17e" | "13 Mini" | "14 Plus" | MacBook: "Air 13" | "Air 15" | "Pro 14" | "Pro 16" | iPad: "Mini 7" | "Air" | "Pro 13" | Other: "A56" | "S25 Ultra" | "Pro 14" | "Forerunner 55" | null if not determinable
  "size":             "...",  // Storage as <N>GB or <N>TB | null if not determinable
  "color_from_text":  "...",  // Raw color EXACTLY as user wrote it, any language — do NOT translate or normalize | null if not mentioned
  "quantity":         1,      // Positive integer, default 1, Cannot be Negative Number or Zero. If quantity is not mentioned, default to 1. If quantity is mentioned as part of a price (e.g. "31.5x4"), extract it and use it here, but do NOT modify the price field (keep it as the full price, e.g. "31.5x4" → quantity: 4, price: 31500). If quantity is preceded by a "-", don't include quantity as negative, just extract the number (e.g. "72600-2" → quantity: 2, price: 72600).
  "price":            0,      // Float, default 0
  "requestedText":    "...",  // Exact raw text the user wrote for this segment — do NOT strip or modify anything, but don't include the price the user wrote (keep typos, Russian words, emojis, spacing exactly as written)
  "countryCode":      null,   // 2-letter ISO code ONLY if flag/country explicitly in this segment (e.g. "🇺🇸"→"US", "🇮🇳"→"IN", "🇨🇳"→"CN") | null if not mentioned | if multiple countries, take the first
  "brand":            "...",  // Apple | Samsung | Garmin | Poco | Dyson | Sony | etc. | null if not a real product
  "product_type":     "...",  // iPhone | MacBook | iPad | Galaxy | Watch | Forerunner | etc. | null if not a real product
  "category":         "...",  // phone | laptop | tablet | watch | earbuds | accessory | other | null if not a real product
  "variant":          "...",  // Pro | Plus | Ultra | Mini | Air | Max | SE | e | null
  "model_code":       "...",  // SKU code: "MW2X3" | "MDHD4" | "MDHA4" | "MHFH4" | "SM-A520F" | null
  "ram":              "...",  // RAM only if separate from storage: "8GB" | null
  "LLM_color_en":     "...",  // Color translated/normalized to English: "белый"→"White" | "синий"→"Blue" | null if no color
  "prod_year":        "...",  // Year if mentioned: "2024" | null
  "simType":          null    // iPhone only — extract ONLY if explicitly stated in text:
                              //   "sim+esim"/"esim+sim"/"sim-esim"/"esim-sim"/"sim/esim"/"1sim"+"esim"/"сим"+"есим" → "PHYSICAL_PLUS_ESIM"
                              //   "1sim"/"1сим"/"1 sim"/"1 сим" alone → "PHYSICAL_PLUS_ESIM"
                              //   if "sim" and "esim" mentioned together in any format → "PHYSICAL_PLUS_ESIM"
                              //   "esim"/"есим"/"только esim" alone → "ESIM_ONLY_SINGLE"
                              //   "E-sim"/"e-sim"/"(E-sim)"/"Dual Esim"/"dual esim"/"2esim"/"2 esim" alone → "ESIM_ONLY_SINGLE"
                              //   "iPhone 17 Air" / "Айфон 17 Эйр" / "iPhone Air" → "ESIM_ONLY_SINGLE"
                              //   "2sim"/"2сим"/"2 sim"/"2 сим"/"dual sim"/"двойной сим"/"два сим" → "PHYSICAL_DUAL"
                              //   any other sim mention → "PHYSICAL_PLUS_ESIM"
                              //   nothing mentioned → "PHYSICAL_PLUS_ESIM"
                              //   always null for non-iPhones
}

=== QUANTITY ===
Default 1. x2 / 2шт / 35600-2 (price-qty) / 31.5x4 (price 31500, qty 4)
"green-2" = color green qty 2. "72600-2" = price 72600 qty 2.

Default quantity = 1 ONLY when no explicit quantity is present.

Be very careful before emitting quantity = 1. A standalone integer after color/storage may be quantity, not model, not price, and not size.

Examples:
"15 128 black 15 45100 45"  -> quantity = 15
"15 128 blue 12 46600 46.5" -> quantity = 12
"15 128 pink 10 47500 47.3" -> quantity = 10
"15 256 black 10 53500 53.1" -> quantity = 10
"15 256 blue 3 53000" -> quantity = 3

Quantity indicators:
- x2 / 2шт / 2 шт / 2pcs / qty 2 -> quantity = 2
- 35600-2 -> price = 35600, quantity = 2
- 31.5x4 -> price = 31500, quantity = 4
- green-2 -> color = green, quantity = 2
- 72600-2 -> price = 72600, quantity = 2



=== PRICE ===
Default 0. "35,3"→35300 / "31.5x4"→price 31500 qty 4 / "3500 дают 3400"→3400
If multiple prices appear, take the lowest one. e.g. "54500 1шт ? 54700" → price: 54500
"15 256 black 10 53500 53.1" → price: 53100 (not 53500, because 53.1 likely means 53100 rubles)

=== REQUESTEDTEXT ===
Keep exactly as the user wrote it — do NOT strip, clean, or modify anything.
Copy the raw text for this segment as-is, including typos, Russian words, emojis, spacing.

=== VARIANT ===
model→variant: variant should contain non-numeric part of model
"16 Pro"→"Pro" | "55"→null | "13 Mini"→"Mini" | "14 Plus"→"Plus" | "15 Pro Max"→"Pro Max" | "17e"→"e" | "S25 Ultra"→"Ultra" | "16e"→"e"

=== MODEL RULES ===
16E ≠ 16 (model: "16E"). PRO→Pro, PLUS→Plus, MAX→Max. N+→"N Plus": 15+→"15 Plus" | 16+→"16 Plus" | 17+→"17 Plus".
iPhone 16Max → model: "16 Pro Max", variant: "Pro Max", not "16 Max". iPhone never releases a "Max" variant without "Pro". If "Max" is mentioned → always assume "Pro Max".
"Air" alone → iPhone 17 Air (product_type: "iPhone", model: "17 Air").
"Air 7"/"iPad Air" → iPad (product_type: "iPad", model: null).
Samsung: "Galaxy A5 SM-A520F 3GB/32GB" → model:"A5", model_code:"SM-A520F", ram:"3GB", size:"32GB"

=== FIELD NOTES ===
- model: human-readable model id — "16 Pro" | "A5" | "Pro 14" | "Forerunner 55" | null if not determinable
- model_code: hardware SKU only — "SM-A520F" | "MW2X3" | "MH9J4" | null if not present. Always separate from model.
- size: storage only — "128GB" | "1TB". Never put RAM here.
- ram: RAM only if explicitly separate from storage — "8GB" | null
- ignore delivery/arrival timing notes — "к 2-3" | "к 3-5" | "к завтра" | "к пятнице" | "ETA 2-3" and similar mean expected arrival days; do NOT parse as quantity, price, or any field. Keep in requestedText as-is but extract no data from them.

=== EXAMPLES ===
Input: "iPad Mini 7 128GB Space Gray Wi-Fi MXN63 35700"
[{"is_real_product":true,"productName":"iPad Mini 7 128GB Space Gray Wi-Fi MXN63","model":"Mini 7","size":"128GB","color_from_text":"Space Gray","quantity":1,"price":35700,"requestedText":"iPad Mini 7 128GB Space Gray Wi-Fi MXN63 35700","countryCode":null,"brand":"Apple","product_type":"iPad","category":"tablet","variant":"Mini","model_code":"MXN63","ram":null,"LLM_color_en":"Space Gray","prod_year":null,"simType":null}]

Input: "Pencil Pro 2025 MX2D3 8500"
[{"is_real_product":true,"productName":"Apple Pencil Pro 2025","model":null,"size":null,"color_from_text":null,"quantity":1,"price":8500,"requestedText":"Pencil Pro 2025 MX2D3","countryCode":null,"brand":"Apple","product_type":"Pencil","category":"accessory","variant":"Pro","model_code":"MX2D3","ram":null,"LLM_color_en":null,"prod_year":"2025","simType":null}]

Input: "16 Pro 256 Black 🇮🇳 54000"
[{"is_real_product":true,"productName":"iPhone 16 Pro 256GB Black","model":"16 Pro","size":"256GB","color_from_text":"Black","quantity":1,"price":54000,"requestedText":"16 Pro 256 Black 🇮🇳","countryCode":"IN","brand":"Apple","product_type":"iPhone","category":"phone","variant":"Pro","model_code":null,"ram":null,"LLM_color_en":"Black","prod_year":null,"simType":null}]

Input: "17 Pro Max 1024 ГБ серебристый eSIM : 1 132 17 Pro Max 1024 ГБ синий eSIM : 1 126,8"
[{"is_real_product":true,"productName":"iPhone 17 Pro Max 1024GB Silver","model":"17 Pro Max","size":"1024GB","color_from_text":"серебристый","quantity":1,"price":132000,"requestedText":"17 Pro Max 1024 ГБ серебристый eSIM : 1","countryCode":null,"brand":"Apple","product_type":"iPhone","category":"phone","variant":"Pro Max","model_code":null,"ram":null,"LLM_color_en":"Silver","prod_year":null,"simType":"ESIM_ONLY_SINGLE"},{"is_real_product":true,"productName":"iPhone 17 Pro Max 1024GB Blue","model":"17 Pro Max","size":"1024GB","color_from_text":"синий","quantity":1,"price":126800,"requestedText":"17 Pro Max 1024 ГБ синий eSIM : 1","countryCode":null,"brand":"Apple","product_type":"iPhone","category":"phone","variant":"Pro Max","model_code":null,"ram":null,"LLM_color_en":"Blue","prod_year":null,"simType":"ESIM_ONLY_SINGLE"}]

Input: "iPhone 17 256gb Black sim+esim\n65 отложи"
[{"is_real_product":true,"productName":"iPhone 17 256GB Black","model":"17","size":"256GB","color_from_text":"Black","quantity":1,"price":0,"requestedText":"iPhone 17 256gb Black sim+esim","countryCode":null,"brand":"Apple","product_type":"iPhone","category":"phone","variant":null,"model_code":null,"ram":null,"LLM_color_en":"Black","prod_year":null,"simType":"PHYSICAL_PLUS_ESIM"},{"is_real_product":false,"productName":null,"model":null,"size":null,"color_from_text":null,"quantity":1,"price":0,"requestedText":"65 отложи","countryCode":null,"brand":null,"product_type":null,"category":null,"variant":null,"model_code":null,"ram":null,"LLM_color_en":null,"prod_year":null,"simType":null}]

Input: "MacBook MW0Y3 Air 13 Starlight (M4, 16GB, 256GB) 2025 68500"
[{"is_real_product":true,"productName":"MacBook Air 13 Starlight","model":"Air 13","size":"256GB","color_from_text":"Starlight","quantity":1,"price":68500,"requestedText":"MacBook MW0Y3 Air 13 Starlight (M4, 16GB, 256GB) 2025","countryCode":null,"brand":"Apple","product_type":"MacBook","category":"laptop","variant":"Air","model_code":"MW0Y3","ram":"16GB","LLM_color_en":"Starlight","prod_year":"2025","simType":null}]

Input: "Macbook Air 13\" (2026) M5 24 GB 1 TB SSD (MDH94) Silver (с СЗУ)  2 111500 к 2-3"
[{"is_real_product":true,"productName":"MacBook Air 13 1TB Silver","model":"Air 13","size":"1TB","color_from_text":"Silver","quantity":2,"price":111500,"requestedText":"Macbook Air 13\" (2026) M5 24 GB 1 TB SSD (MDH94) Silver (с СЗУ)  2 к 2-3","countryCode":null,"brand":"Apple","product_type":"MacBook","category":"laptop","variant":"Air","model_code":"MDH94","ram":"24GB","LLM_color_en":"Silver","prod_year":"2026","simType":null}]

"""

# ── Step 2 color prompt ───────────────────────────────────────────────────────

COLOR_PROMPT = """\
You are a color matcher. Given the user's requested color and a list of available DB colors, pick the closest match.

Return ONLY: {"matched_color": "..."}
- matched_color must be copied exactly from available_colors
- Always return the closest match, never null
- Translate user color to English first: "белый"→White | "чёрный"→Black | "синий"→Blue | "серый"→Gray | "розовый"→Pink | "зелёный"→Green | "фиолетовый"→Purple
- Always return the closest match, never null
- Partial ok: "black" → "Black Titanium" | "midnight" → "Midnight Black" | "белый" → "White Titanium"
- White looks like "startlight" or "silver"
- Black looks like "midnight" or "space black"
"""


# ── LLM helpers ───────────────────────────────────────────────────────────────

# === FIX 3 (replaced): accept either a raw JSON array OR a {"products":[...]} object. ===
# The structured-outputs path (FIX 3 in _llm_call_inner) wraps results in an object
# because the OpenAI Responses API requires a top-level object schema; this parser
# unwraps it transparently while still accepting the legacy raw-array form.
def _parse_json_array(raw: str) -> list[dict]:
    if raw.startswith("```"):
        raw = "\n".join(l for l in raw.splitlines() if not l.startswith("```")).strip()

    obj_start, obj_end = raw.find("{"), raw.rfind("}")
    arr_start, arr_end = raw.find("["), raw.rfind("]")

    # Prefer object-wrapped form when the response starts with "{"
    if obj_start != -1 and obj_end != -1 and (arr_start == -1 or obj_start < arr_start):
        try:
            obj = json.loads(raw[obj_start : obj_end + 1])
            if isinstance(obj, dict) and isinstance(obj.get("products"), list):
                return obj["products"]
        except Exception:
            pass  # fall through to raw-array parsing

    if arr_start == -1 or arr_end == -1:
        raise ValueError(f"No JSON array in response: {raw}")
    return json.loads(raw[arr_start : arr_end + 1])
# === END FIX 3 ===


async def _llm_call(
    system: str,
    user: str,
    max_tokens: int = 8192,
    cache: bool = False,
    # === FIX 6 (added): allow caller to override the strict schema. ===
    # Default is the product-array schema (Stage 2). Stage 1 passes the segment schema.
    # Pass `schema=None` explicitly to disable structured outputs entirely (e.g. for the
    # color-matcher in Step 2 where output is a tiny free-form JSON object).
    # === END FIX 6 ===
    schema: Optional[dict] = None,
    schema_name: str = "product_array",
) -> str:
    """
    Call the configured LLM provider (Anthropic or OpenAI).
    Retries on 429/503/529 with exponential backoff.
    """
    for attempt in range(4):
        try:
            return await _llm_call_inner(system, user, max_tokens, cache, schema, schema_name)
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (429, 503, 529) and attempt < 3:
                wait = 10 * (attempt + 1)
                await asyncio.sleep(wait)
                continue
            raise
    raise RuntimeError("LLM call failed after 4 attempts")


async def _llm_call_inner(
    system: str,
    user: str,
    max_tokens: int = 8192,
    cache: bool = False,
    schema: Optional[dict] = None,
    schema_name: str = "product_array",
) -> str:
    """Single attempt LLM call."""
    # === FIX 6 (default): if caller didn't pass a schema, use the product-array schema. ===
    # Sentinel None means "use the default Stage-2 schema". Pass an explicit empty-dict {}
    # if you want to disable structured outputs (rare; only the color-matcher does this).
    if schema is None:
        schema = _LLM_PRODUCT_SCHEMA
    # === END FIX 6 ===
    async with httpx.AsyncClient(timeout=LLM_TIMEOUT) as client:

        if LLM_PROVIDER == "anthropic":
            system_block = (
                [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
                if cache else system
            )
            resp = await client.post(
                LLM_API_URL,
                headers={
                    "Content-Type":      "application/json; charset=utf-8",
                    "x-api-key":         LLM_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "anthropic-beta":    "prompt-caching-2024-07-31",
                },
                json={
                    "model":      LLM_MODEL,
                    "max_tokens": max_tokens,
                    "system":     system_block,
                    "messages":   [{"role": "user", "content": user}],
                },
            )
            resp.raise_for_status()
            data  = resp.json()
            usage = data.get("usage", {})
            return data["content"][0]["text"].strip()

        elif LLM_PROVIDER == "openai":
            # GPT-5.4 family requires /v1/responses endpoint
            # GPT-4.1 family uses /v1/chat/completions
            is_gpt5 = LLM_MODEL.startswith("gpt-5")
            headers = {
                "Content-Type":  "application/json; charset=utf-8",
                "Authorization": f"Bearer {LLM_API_KEY}",
            }
            if is_gpt5:
                url  = "https://api.openai.com/v1/responses"
                body = {
                    "model": LLM_MODEL,
                    "input": [
                        {"role": "system", "content": system},
                        {"role": "user",   "content": user},
                    ],
                    "max_output_tokens": max_tokens,
                    # === FIX 3 (parameterized by FIX 6): JSON-schema constraint. ===
                    # Stage 2 uses _LLM_PRODUCT_SCHEMA (the default). Stage 1 passes
                    # _LLM_SEGMENT_SCHEMA. Callers pass schema={} to skip structured outputs
                    # entirely (used by the color-matcher in step2_match_color).
                    **(
                        {"text": {"format": {
                            "type": "json_schema",
                            "name": schema_name,
                            "strict": True,
                            "schema": schema,
                        }}}
                        if schema  # skip if empty dict
                        else {}
                    ),
                    # === END FIX 3 ===
                    # "service_tier": "priority",  # Add this line to use the priority tier for faster responses
                }
            else:
                url  = LLM_API_URL
                body = {
                    "model":      LLM_MODEL,
                    "max_tokens": max_tokens,
                    "messages":   [
                        {"role": "system", "content": system},
                        {"role": "user",   "content": user},
                    ],
                }
            resp = await client.post(url, headers=headers, json=body)
            resp.raise_for_status()
            data  = resp.json()
            usage = data.get("usage", {})
            if is_gpt5:
                # Responses API returns output as a list of content blocks
                return data["output"][-1]["content"][0]["text"].strip()
            else:
                return data["choices"][0]["message"]["content"].strip()

        else:
            raise ValueError(f"Unknown LLM_PROVIDER: {LLM_PROVIDER!r}")


# ── DB helpers ────────────────────────────────────────────────────────────────

async def fetch_official_colors(
    pool: asyncpg.Pool,
    model_name: Optional[str],
) -> list[str]:
    """Query public.products for official color names for this model."""
    if not model_name:
        return []
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT DISTINCT color
            FROM   public.products
            WHERE  "model" = $1
              AND  color IS NOT NULL
              AND product_type ILIKE 'iPhone'
              AND category ILIKE 'phone'
        """, model_name)
    return [r["color"] for r in rows]


async def macbook_part_in_db(
    pool: asyncpg.Pool,
    part_number: Optional[str],
) -> bool:
    """
    Check public.products for whether a MacBook part number (model_code) exists.
    Returns True if a matching laptop row is found, else False.

    Matches the 5-char base case-insensitively and tolerant of the region suffix
    (DB may store "MW0Y3", "MW0Y3LL/A", etc.) by comparing on the leading 5 chars.
    """
    if not part_number:
        return False
    base = part_number.strip().upper()
    base = "".join(ch for ch in base.split("/")[0] if ch.isalnum())[:5]
    if not base:
        return False
    async with pool.acquire() as conn:
        row = await conn.fetchrow(f"""
            SELECT 1
            FROM   {DB_TABLE}
            WHERE  upper(left(regexp_replace({DB_COL_MODEL_NAME_CODE}, '[^A-Za-z0-9]', '', 'g'), 5)) = $1
              AND  category ILIKE 'laptop'
            LIMIT 1
        """, base)
    return row is not None


# ── Core pipeline steps ───────────────────────────────────────────────────────

REQUIRED_FIELDS = ("model", "quantity", "price", "requestedText")  # kept for backward compat

# Required fields by product type — used for retry validation
IPHONE_REQUIRED     = ("model", "quantity", "price", "requestedText")
NON_IPHONE_REQUIRED = ("quantity", "price", "requestedText")

# Final drop required fields — checked after productName fallback is applied
IPHONE_FINAL_REQUIRED     = ("model", "quantity", "price", "requestedText")
NON_IPHONE_FINAL_REQUIRED = ("quantity", "price", "requestedText", "productName")


def _is_iphone_dict(p: dict) -> bool:
    """Check if a raw dict is an iPhone product."""
    return (
        (p.get("brand") or "").lower() == "apple"
        and "iphone" in (p.get("product_type") or "").lower()
        and (p.get("category") or "").lower() == "phone"
    )


async def _confirm_part(
    pool: asyncpg.Pool,
    part_number: Optional[str],
) -> bool:
    """
    Confirm a single part number is known: static set → DB → promote.
      1. STATIC set (instant, no I/O).
      2. On miss → DB membership check.
      3. On DB hit → promote into the static set (read-through cache).
    Returns True if known, False otherwise. Assumes `part_number` is already
    shape-valid (caller checks). Does not translate to a model name.
    """
    if not part_number:
        return False
    if is_known_part(part_number):
        return True
    try:
        found = await macbook_part_in_db(pool, part_number)
    except Exception as e:
        logger.error("MacBook DB lookup failed for %r: %s", part_number, e)
        return False
    if found:
        remember_part(part_number)
        logger.info("── MACBOOK ── %r → DB hit (promoted to static)", part_number)
        return True
    return False


async def resolve_macbook_code(
    pool: asyncpg.Pool,
    llm_model_code: Optional[str],
    requested_text: str,
) -> Optional[str]:
    """
    Decide the authoritative model_code for a MacBook.

    Rule: regex-extract part-number candidates from requested_text, confirm each
    against the known set (static → DB, with promotion), and return the first
    CONFIRMED code — it wins over the LLM whether the LLM was empty, agreed, or
    disagreed. If no candidate is confirmed, return the LLM's model_code
    unchanged (the fallback path; the LLM code may be a real-but-unlisted SKU).

    Returns the value that should populate model_code (may equal llm_model_code).
    """
    candidates = extract_part_candidates(requested_text)
    for cand in candidates:
        if await _confirm_part(pool, cand):
            if cand != (llm_model_code or "").strip().upper():
                logger.info(
                    "── MACBOOK ── confirmed model_code=%r from text (LLM had %r) — regex+set wins",
                    cand, llm_model_code,
                )
            else:
                logger.info("── MACBOOK ── confirmed model_code=%r (matches LLM)", cand)
            return cand

    # No confirmed match — keep the LLM's value (could be a real unlisted code).
    logger.info(
        "── MACBOOK ── no confirmed part number in text; keeping LLM model_code=%r",
        llm_model_code,
    )
    return llm_model_code




def _missing_required_single(p: dict) -> list[str]:
    """Return list of null required fields for a single product dict (type-aware)."""
    fields = IPHONE_REQUIRED if _is_iphone_dict(p) else NON_IPHONE_REQUIRED
    return [f for f in fields if p.get(f) is None]


async def _extract_single(requested_text: str, is_iphone: bool, max_attempts: int) -> dict:
    """
    Retry extraction for a single product by its requestedText.
    Returns the best result after all attempts.
    """
    fields = IPHONE_REQUIRED if is_iphone else NON_IPHONE_REQUIRED
    best = None
    for attempt in range(max_attempts):
        raw = await _llm_call(EXTRACT_PROMPT, requested_text, max_tokens=1024, cache=False)
        logger.info("── STEP 1 retry (attempt %d/%d) for %r ──\n%s", attempt + 1, max_attempts, requested_text[:60], raw)
        try:
            results = _parse_json_array(raw)
            if not results:
                await asyncio.sleep(0.5)
                continue
            p = results[0]
            # === FIX 4 (retry path): validate before treating as a real result. ===
            try:
                p = LLMExtractedProduct.model_validate(p).model_dump(mode="json")
            except Exception as ve:
                logger.warning(
                    "FIX 4 (retry %d/%d): validation failed for %r: %s",
                    attempt + 1, max_attempts, requested_text[:60], ve,
                )
                if attempt < max_attempts - 1:
                    await asyncio.sleep(0.5)
                continue
            # === END FIX 4 ===
            nulls = [f for f in fields if p.get(f) is None]
            if not nulls:
                return p
            best = p
            logger.warning("Retry attempt %d/%d — still null fields %s for %r", attempt + 1, max_attempts, nulls, requested_text[:60])
            if attempt < max_attempts - 1:
                await asyncio.sleep(0.5)
        except Exception as e:
            logger.warning("Retry attempt %d/%d failed: %s", attempt + 1, max_attempts, e)
            if attempt < max_attempts - 1:
                await asyncio.sleep(0.5)
    return best or {}


# === FIX 1 (rewritten v2): strip order-status header phrases anywhere in the input. ===
# v1 used line-anchored regexes (^...$ with MULTILINE). That FAILED on inputs that arrive
# as a single flat string (e.g. URL-encoded query parameters with no newlines). When the
# whole order is one line, "^...$" matches the entire thing and either deletes everything
# or leaves the header in place — both cause the LLM to misread the input.
#
# v2 matches header PHRASES (not lines) and removes them wherever they appear, so the
# behavior is identical for line-separated and single-line inputs.
_HEADER_PHRASE_PATTERNS = [
    re.compile(r"Заказ\s*№?\s*\d+\s*[,;:]?",                            re.IGNORECASE),
    re.compile(r"Принят\s+в\s+обработку\s*[,;:]?",                      re.IGNORECASE),
    re.compile(r"Выдача\s+(?:сегодня|завтра)\s*[,;:]?",                 re.IGNORECASE),
    re.compile(r"🕗\s*[-–—]?\s*\d{1,2}\s+\w+\s+20\d{2}[^\d]*?\d{1,2}:\d{2}"),  # "🕗 - 27 April 2026, 00:07"
    re.compile(r"🕗"),  # stray clock emoji
]


def _strip_order_headers(text: str) -> str:
    """Remove order-status header phrases anywhere in the input."""
    for pat in _HEADER_PHRASE_PATTERNS:
        text = pat.sub(" ", text)
    # Collapse runs of whitespace caused by the substitutions
    text = re.sub(r"\s{2,}", " ", text).strip()
    return text
# === END FIX 1 ===


# === FIX 6 (added): Stage 1 LLM-based segmenter (replaces removed FIX 5 regex splitter). ===
# Why removed: input formats vary too much for a deterministic regex to be reliable. A
# regex that works for "8 шт х 39000 руб." won't work for "5 × iPhone 17 @ $700 each" or
# free-form descriptions, and silent under-splitting causes downstream collapse.
#
# Why LLM: handles arbitrary formats and languages. Cost is one extra call per request,
# but Stage 1 output is small and Stage 2 calls run in parallel so end-to-end latency
# stays comparable.
#
# Pipeline shape:
#   step1_extract(text)
#     ├─ if input is short/simple → skip Stage 1, send to Stage 2 directly
#     └─ else → Stage 1 segments → batched parallel Stage 2 → merge & validate

# Heuristic for "is this input long/complex enough to warrant Stage 1?". This is NOT a
# splitter — it only decides whether Stage 1 is worth running. We trigger if EITHER:
#   * the input is long (≥ LONG_INPUT_CHARS chars), OR
#   * we see ≥ MULTI_PRODUCT_HINTS quantity-token-shaped patterns.
# Patterns are a loose union of common quantity markers across languages; missed matches
# just mean we don't run Stage 1 (fine — short inputs are unlikely to collapse).
LONG_INPUT_CHARS    = 600
MULTI_PRODUCT_HINTS = 7

_QTY_HINT_RX = re.compile(
    r"(?:"
    r"\d+\s*шт"                     # Russian: "8 шт"
    r"|\d+\s*pcs"                   # English: "8 pcs"
    r"|\d+\s*x\s*\d"                # "8 x 39000"
    r"|\d+\s*х\s*\d"                # "8 х 39000" (Cyrillic х)
    r"|\d+\s*×\s*\d"                # "8 × 39000"
    r"|\d+\s*@\s*"                  # "8 @ $700"
    r"|шт\s*[xх×]\s*\d"             # "шт х 39000"
    r")",
    re.IGNORECASE,
)


def _looks_multi_product(text: str) -> bool:
    """Return True if input is long enough or contains enough quantity hints to warrant Stage 1."""
    if len(text) >= LONG_INPUT_CHARS:
        return True
    return len(_QTY_HINT_RX.findall(text)) >= MULTI_PRODUCT_HINTS


def _verify_segments_verbatim(segments: list[str], original: str) -> list[str]:
    """
    Soft drift check: every segment should appear (modulo whitespace) as a substring of
    the original input. Returns the list of segments that DRIFTED (failed the check).
    Empty list = clean result.
    """
    norm_original = re.sub(r"\s+", "", original).casefold()
    drifted: list[str] = []
    for seg in segments:
        norm_seg = re.sub(r"\s+", "", seg).casefold()
        if norm_seg and norm_seg not in norm_original:
            drifted.append(seg)
    return drifted


async def _stage1_call(text: str, prompt: str) -> list[str]:
    """One Stage 1 call. Raises on parse failure."""
    raw = await _llm_call(
        prompt, text,
        max_tokens=4096,
        cache=False,
        schema=_LLM_SEGMENT_SCHEMA,
        schema_name="segments",
    )
    # Parse — Stage 1 schema guarantees {"segments": [...]} shape, but defensively unwrap.
    if raw.startswith("```"):
        raw = "\n".join(l for l in raw.splitlines() if not l.startswith("```")).strip()
    obj_start = raw.find("{")
    obj_end   = raw.rfind("}")
    if obj_start == -1 or obj_end == -1:
        raise ValueError(f"Stage 1: no JSON object in response: {raw[:200]!r}")
    parsed = json.loads(raw[obj_start : obj_end + 1])
    return LLMSegmentResponse.model_validate(parsed).segments




async def stage1_segment(text: str) -> list[str]:
    """Wrapper that logs the final Stage 1 segments regardless of which path produced them."""
    segments = await _stage1_segment_impl(text)
    logger.info(
        "── STAGE 1 RESULT ── %d segment(s):\n%s",
        len(segments),
        "\n".join(f"  [{i}] {s!r}" for i, s in enumerate(segments)) if segments else "  (none)",
    )
    return segments




async def _stage1_segment_impl(text: str) -> list[str]:
    """
    LLM-based Stage 1 segmentation. Returns one verbatim per-product string per segment.
    Behavior matrix:
      * Stage 1 returns ≥ 2 segments, no drift              → return segments
      * Stage 1 returns ≥ 2 segments WITH drift             → retry once with stronger prompt
      * Stage 1 returns 0 or 1 segments on a long input     → retry once with stronger prompt
      * Both attempts fail (still drifted / still 0-1 segs) → log + return [] (caller decides fallback)
    """
    # First attempt
    try:
        segments = await _stage1_call(text, SEGMENT_PROMPT)
    except Exception as e:
        logger.warning("Stage 1 first attempt failed: %s", e)
        segments = []

    drifted = _verify_segments_verbatim(segments, text) if segments else []

    # Decide whether to retry
    needs_retry = bool(drifted) or (len(segments) <= 1 and _looks_multi_product(text))

    if not needs_retry:
        if drifted:  # only logs if we couldn't avoid it (shouldn't reach here)
            logger.warning("Stage 1: %d segment(s) drifted from input — proceeding anyway", len(drifted))
        return segments

    # One retry with stronger prompt
    if drifted:
        logger.warning(
            "Stage 1 retry: %d/%d segments drifted from input (e.g. %r)",
            len(drifted), len(segments), drifted[0][:60] if drifted else "",
        )
    else:
        logger.warning(
            "Stage 1 retry: returned %d segment(s) but input looks multi-product (len=%d)",
            len(segments), len(text),
        )

    try:
        retry_segments = await _stage1_call(text, SEGMENT_PROMPT_RETRY)
    except Exception as e:
        logger.error("Stage 1 retry failed: %s", e)
        return segments  # return whatever first attempt got, even if drifted

    retry_drifted = _verify_segments_verbatim(retry_segments, text) if retry_segments else []

    # Pick the better of the two attempts
    if retry_segments and not retry_drifted:
        return retry_segments
    if retry_drifted:
        logger.warning(
            "Stage 1 retry STILL drifted: %d/%d segments — proceeding with the longer attempt",
            len(retry_drifted), len(retry_segments),
        )
    # If retry collapsed too, keep whichever attempt has more segments (over-splitting > collapse)
    if len(retry_segments) > len(segments):
        return retry_segments
    return segments
# === END FIX 6 ===


# === FIX 6 (replaces FIX 2): batched Stage 2 extraction. =================================
# This replaces both the original "one giant call" and the FIX 2 chunked-fallback. With
# Stage 1 producing clean per-product segments, Stage 2 always operates on a manageable
# batch (≤ STAGE2_BATCH_SIZE products per call). Calls run in parallel.
#
# v2 (FIX 8): segments are joined by blank lines without "[N]" numbering. The strict
# schema (FIX 3/FIX 4) already guarantees one product object per input segment, so the
# numbering was redundant — and worse, it leaked into requestedText because the prompt
# tells the model to copy requestedText verbatim. Now requestedText stays clean.
STAGE2_BATCH_SIZE  = 10
STAGE2_MAX_TOKENS  = 8192

# === FIX 8 (added): defensive scrubber for any "[N] " or "[N]" prefix that still leaks. ==
# In case the model accidentally prepends item numbering despite the prompt, strip it
# before the value reaches the API consumer.
_LEAKED_NUM_PREFIX_RX = re.compile(r"^\s*\[\d+\]\s*")
def _scrub_numbering_prefix(s: Optional[str]) -> Optional[str]:
    if not s:
        return s
    return _LEAKED_NUM_PREFIX_RX.sub("", s)
# === END FIX 8 ===


async def _stage2_extract_batch(segments: list[str]) -> list[dict]:
    """
    Run Stage 2 extraction on one batch of pre-segmented product strings.
    Returns validated, dumped LLMExtractedProduct dicts.
    """
    if not segments:
        return []
    # === FIX 8: join segments with blank-line separators only, no "[N]" numbering. ===
    # The strict schema enforces N-in-N-out; the model doesn't need numeric anchors.
    # Stripping numbering keeps requestedText byte-identical to what the user wrote.
    user_msg = "\n\n".join(segments)
    # === END FIX 8 ===
    try:
        raw = await _llm_call(EXTRACT_PROMPT, user_msg, max_tokens=STAGE2_MAX_TOKENS, cache=True)
    except Exception as e:
        logger.error("Stage 2 batch extraction failed (size=%d): %s", len(segments), e)
        return []

    try:
        parsed = _parse_json_array(raw)
    except Exception as e:
        logger.error("Stage 2 batch parse failed: %s — raw=%r", e, raw[:300])
        return []

    out: list[dict] = []
    for idx, p in enumerate(parsed):
        # === FIX 8 (defensive): scrub leaked "[N]" prefix from requestedText ===
        # Belt-and-braces — even if the model invents a prefix, we strip it.
        if isinstance(p, dict) and p.get("requestedText"):
            cleaned = _scrub_numbering_prefix(p["requestedText"])
            if cleaned != p["requestedText"]:
                logger.warning("FIX 8: scrubbed [N] prefix from requestedText: %r → %r",
                               p["requestedText"][:60], cleaned[:60])
                p["requestedText"] = cleaned
        # === END FIX 8 ===
        try:
            out.append(LLMExtractedProduct.model_validate(p).model_dump(mode="json"))
        except Exception as e:
            logger.warning("Stage 2 batch[%d]: validation failed for item %d: %s — raw=%r",
                           len(segments), idx, e, p)
    return out
# === END FIX 6 ===


async def step1_extract(text: str) -> list[dict]:
    """
    Two-stage extraction:
      Stage 0  — strip order-status headers (FIX 1)
      Gating   — short/simple inputs skip Stage 1 (single Stage 2 call)
      Stage 1  — LLM segmenter (FIX 6) returns one verbatim string per product
      Stage 2  — batched parallel extraction with strict schema (FIX 3/4/6)
    Real products with null required fields are retried individually by requestedText
    (existing _extract_single retry path, preserved).
    """
    # === FIX 1 (call site): remove order-status header phrases. ===
    text = _strip_order_headers(text)
    # === END FIX 1 ===

    if not text:
        logger.warning("step1_extract: empty input after header strip")
        return []

    # === FIX 6 (call site): two-stage extraction ===========================================
    # Decide whether the input warrants Stage 1. If short/simple, skip it.
    if _looks_multi_product(text):
        logger.info("Stage 1 (segmentation) — input chars=%d", len(text))
        segments = await stage1_segment(text)

        if len(segments) < 2 and _looks_multi_product(text):
            # Stage 1 failed twice (segment + retry). Per the design decision: give up cleanly.
            logger.error(
                "Stage 1 failed: only %d segment(s) on input that looks multi-product (chars=%d). "
                "Returning [] rather than guessing.",
                len(segments), len(text),
            )
            return []

        if not segments:
            # Stage 1 confidently said "no products" (e.g. just headers/notes)
            logger.info("Stage 1: no products in input")
            return []
    else:
        # Short/simple input — skip Stage 1, treat the whole input as one segment
        logger.info("Stage 1 skipped (input chars=%d below threshold)", len(text))
        segments = [text]

    # ── Stage 2: batched parallel extraction ────────────────────────────────────────────
    batches = [
        segments[k : k + STAGE2_BATCH_SIZE]
        for k in range(0, len(segments), STAGE2_BATCH_SIZE)
    ]
    logger.info(
        "Stage 2: %d segment(s) split into %d batch(es) of up to %d each",
        len(segments), len(batches), STAGE2_BATCH_SIZE,
    )

    batch_results = await asyncio.gather(*[_stage2_extract_batch(b) for b in batches])

    # Flatten while keeping order. Drop is_real_product=false items (Stage 1 already
    # filtered headers; if Stage 2 still flags one, it's truly noise).
    first_result: list[dict] = []
    for batch in batch_results:
        for p in batch:
            if p.get("is_real_product", True):
                first_result.append(p)
            else:
                logger.info(
                    "Stage 2: dropping non-real item: %r",
                    (p.get("requestedText") or "")[:60],
                )

    if not first_result:
        logger.warning("Stage 2 returned 0 real products from %d segment(s)", len(segments))
        return []
    # === END FIX 6 ===

    # ── Per-product processing: retry items with null required fields ─────────────────
    # (Unchanged from original — still uses _extract_single for missing-field retries.)
    final_results: list[tuple[int, dict]] = []
    retry_tasks: list[tuple[int, dict]] = []

    for i, p in enumerate(first_result):
        nulls = _missing_required_single(p)
        if not nulls:
            final_results.append((i, p))
        else:
            logger.warning("Real product[%d] has null required fields %s — will retry individually", i, nulls)
            retry_tasks.append((i, p))

    if retry_tasks:
        retried = await asyncio.gather(*[
            _extract_single(
                p.get("requestedText") or text,
                is_iphone=_is_iphone_dict(p),
                max_attempts=3 if _is_iphone_dict(p) else 2,
            )
            for _, p in retry_tasks
        ])
        for (i, _), retried_p in zip(retry_tasks, retried):
            if retried_p:
                final_results.append((i, retried_p))
            else:
                logger.warning("Retry failed for product[%d], dropping", i)

    final_results.sort(key=lambda x: x[0])
    return [p for _, p in final_results]


async def step2_match_color(
    pool: asyncpg.Pool,
    model_name: Optional[str],
    requested_text: str,
    color_en: Optional[str],
) -> Optional[str]:
    """
    1. Check OVERRIDES dict (instant, no API call)
    2. Query products table + LLM picks best match
    """
    if not color_en and not requested_text:
        return None

    model_key = (model_name or "").strip().lower()
    color_key = (color_en or "").strip().lower()

    override = COLOR_OVERRIDES.get((model_key, color_key))
    if override:
            return override

    official_colors = await fetch_official_colors(pool, model_name)
    if not official_colors:
        return None

    user_msg = json.dumps({
        "requested_text":     requested_text,
        "user_color_english": color_en,
        "available_colors":   official_colors,
    }, ensure_ascii=False)

    for attempt in range(3):
        try:
            # === FIX 6: disable strict product schema for color-matcher. ===
            # COLOR_PROMPT returns {"matched_color": "..."}, not a product array.
            # Passing schema={} tells _llm_call to skip the json_schema constraint.
            raw = await _llm_call(COLOR_PROMPT, user_msg, max_tokens=64, schema={})
            break
        except Exception as e:
            if "429" in str(e) and attempt < 2:
                wait = 10 * (attempt + 1)
                await asyncio.sleep(wait)
            else:
                raise
    if raw.startswith("```"):
        raw = "\n".join(l for l in raw.splitlines() if not l.startswith("```")).strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1:
        return None
    matched_color = json.loads(raw[start:end+1]).get("matched_color")
    logger.info("── STEP 2 color match ── model=%r requested=%r color_en=%r → matched=%r",
        model_name, requested_text, color_en, matched_color)
    return matched_color


def _is_iphone(product: dict) -> bool:
    brand    = (product.get("brand") or "").lower()
    line     = (product.get("product_type") or "").lower()
    category = (product.get("category") or "").lower()
    return brand == "apple" and "iphone" in line and category == "phone"


def _is_macbook(product: dict) -> bool:
    brand    = (product.get("brand") or "").lower()
    line     = (product.get("product_type") or "").lower()
    category = (product.get("category") or "").lower()
    return brand == "apple" and "macbook" in line and category == "laptop"


# ── FastAPI app ───────────────────────────────────────────────────────────────

_pool: Optional[asyncpg.Pool] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Apply timestamp format to uvicorn.error handlers (runs per worker)
    _fmt = logging.Formatter("%(asctime)s %(levelname)s — %(message)s")
    for _h in logging.getLogger("uvicorn.error").handlers:
        _h.setFormatter(_fmt)
    for _h in logging.getLogger("uvicorn.access").handlers:
        _h.setFormatter(logging.Formatter('%(asctime)s %(levelname)s — %(message)s'))
    global _pool
    logger.info("Starting app worker pid=%s", os.getpid())
    _pool = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=DB_POOL_MIN,
        max_size=DB_POOL_MAX,
        server_settings={"client_encoding": "UTF8"},
    )
    logger.info("DB pool created pid=%s", os.getpid())
    yield
    logger.info("Shutting down worker pid=%s", os.getpid())
    await _pool.close()


app = FastAPI(title="Product Parser", version="1.0.0", lifespan=lifespan)


@app.get("/parse", response_model=list[EnrichedProduct])
async def parse(
    text: str = Query(..., description="Raw order text, may be multi-line"),
):
    """
    Full pipeline in one call:
      Step 1 — LLM extracts products from raw text
      Step 2 — SIM rules applied (iPhones only)
      Step 3 — DB color match via LLM (iPhones only, runs concurrently)

    Returns a flat JSON array of enriched products.
    """
    if not text.strip():
        raise HTTPException(status_code=400, detail="text must not be empty")

    # Collapse runs of 2+ blank lines into a single blank line. Done once here
    # so every downstream step (LLM prompt, fallback splitlines, logs) sees the
    # cleaned input.
    text = _normalize_blank_lines(text)
    text = _normalize_duplicated_esim(text)


    # lowercase the llm input
    text = _lowercase_for_llm(text)


    

    _t0 = asyncio.get_event_loop().time()
    logger.info("\n" + "="*80)
    logger.info("── REQUEST ── %r", text)

    # ── Step 1: extract ───────────────────────────────────────────────────────
    try:
        raw_products = await step1_extract(text)
    except Exception as e:
        logger.error("Step 1 failed after all retries: %s", e)
        # Return one fallback product per non-empty line instead of crashing
        fallback = [
            EnrichedProduct(requestedText=line).model_dump()
            for line in text.splitlines()
            if line.strip()
        ] or [EnrichedProduct(requestedText=text).model_dump()]
        return [EnrichedProduct.model_validate(r) for r in fallback]

    logger.info("── STEP 1 parsed ── %d product(s):\n%s", len(raw_products), json.dumps(raw_products, ensure_ascii=False, indent=2))


    # If LLM returned empty, return one fallback product per line
    if not raw_products:
        raw_products = [
            {"requestedText": line}
            for line in text.splitlines()
            if line.strip()
        ] or [{"requestedText": text}]

    # ── Filter: normalize size, strip internal fields, drop products missing required fields ─
    _GB_TO_TB = {"1024GB": "1TB", "2048GB": "2TB"}
    valid_products, dropped = [], []
    for raw in raw_products:
        # Normalize size
        if raw.get("size") in _GB_TO_TB:
            raw["size"] = _GB_TO_TB[raw["size"]]
        # Strip is_real_product — internal field, never exposed in API response
        raw.pop("is_real_product", None)
        # Build productName fallback for non-iPhones before validation
        if not _is_iphone_dict(raw) and not raw.get("productName"):
            parts = [raw.get("product_type"), raw.get("model"), raw.get("size"), raw.get("color_from_text")]
            raw["productName"] = " ".join(p for p in parts if p) or None
        # Validate required fields (type-aware)
        final_required = IPHONE_FINAL_REQUIRED if _is_iphone_dict(raw) else NON_IPHONE_FINAL_REQUIRED
        nulls = [f for f in final_required if raw.get(f) is None]
        if nulls:
            logger.warning("Dropping product with null required fields %s: %r", nulls, raw)
            dropped.append(raw)
        else:
            valid_products.append(raw)

    if dropped and not valid_products:
        # Every product was invalid — return empty list rather than crash
        logger.error("All %d products dropped due to null required fields", len(dropped))
        return []

    raw_products = valid_products

    # ── Step 2: SIM + color match concurrently ────────────────────────────────
    async def enrich(raw: dict) -> EnrichedProduct:
        # Pydantic validation + field coercion
        p = EnrichedProduct.model_validate(raw)

        # Resolve SIM type (iPhones only)
        if _is_iphone(raw):
            # Pass LLM-extracted simType as the explicit indicator to resolve_sim_type
            # so it is used when no country code is present
            sim = resolve_sim_type(
                product_type="iPhone",
                model=p.model,
                country_code=p.countryCode,
                requested_text=p.requestedText,
                llm_sim_type=p.simType,  # LLM-extracted value from Step 1
            )
            p.simType     = sim["simType"]
            p.simConflict = sim["simConflict"]

            # Color match from DB
            try:
                p.matched_color = await step2_match_color(
                    _pool,
                    p.model,
                    p.requestedText,
                    p.LLM_color_en,
                )
            except Exception as e:
                logger.error("Step 2 color match failed for %s: %s", p.requestedText, e)

        # MacBook: prefer a part number confirmed from requestedText against the
        # known set (static → DB → promote); else keep the LLM's model_code.
        elif _is_macbook(raw):
            p.model_code = await resolve_macbook_code(_pool, p.model_code, p.requestedText)

        return p

    # Run enrichment for all products concurrently
    results = await asyncio.gather(*[enrich(r) for r in raw_products])
    results = list(results)

    out = [r.model_dump() for r in results]
    _elapsed = asyncio.get_event_loop().time() - _t0
    logger.info("── FINAL OUTPUT ── %d product(s) in %.2fs:\n%s", len(out), _elapsed, json.dumps(out, ensure_ascii=False, indent=2))
    logger.info("="*80 + "\n")
    return results


# ── POST /parse ───────────────────────────────────────────────────────────────
# Convenience endpoint for clients that can't easily send multi-line text via
# query strings (Swagger UI strips newlines, Postman has issues with `+`, etc.).
# Accepts raw text directly in the request body — no JSON wrapping, no escaping.
# Behavior is IDENTICAL to GET /parse — same pipeline, same response, same logs.
# The GET endpoint is unchanged and remains the primary route for production.
#
# Postman usage:
#   Method: POST
#   URL:    http://localhost:8000/parse
#   Body:   raw → Text → paste your multi-line text directly

from fastapi import Request


@app.post("/parse", response_model=list[EnrichedProduct])
async def parse_post(request: Request):
    """Raw-body variant of /parse. Paste text directly, no JSON needed."""
    body_bytes = await request.body()
    text = body_bytes.decode("utf-8", errors="replace")
    return await parse(text=text)


@app.get("/health")
async def health():
    logger.info("Health check pid=%s", os.getpid())
    return {"status": "ok"}