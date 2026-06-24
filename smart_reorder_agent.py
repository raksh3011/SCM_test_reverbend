"""
smart_reorder_agent.py -- the Smart Reorder agent (Circe specimen).

GOAL given to the agent: "Keep inventory optimized to prevent stockouts while
keeping holding costs low."

perceive -> read stock + suppliers + the free-text demand context
decide   -> (1) PREDICT demand uplift from the context [judgment]
            (2) compute the Reorder Point ROP = (d' x L) + SS [arithmetic]
            (3) if projected stock <= ROP, choose a supplier [judgment]
                and size the order
act      -> draft a PO, "send" it, log ERP as On Order, alert finance

TWO seams carry the AI judgment, and they are deliberately isolated:
  predict_demand()  -> turns weather/holiday text into a demand multiplier
  choose_supplier()  -> weighs price vs reliability vs speed
The ROP arithmetic between them is deterministic and exactly checkable.

Modes:
  python smart_reorder_agent.py            # mock: recorded judgments, offline + identical
  python smart_reorder_agent.py --live     # real LLM does the demand prediction

------------------------------------------------------------------------------
INPUT / OUTPUT CONTRACT
------------------------------------------------------------------------------
Inputs (read by perceive(), from a SQLite DB at DB and a text file at CONTEXT):
    product table row  -> dict with keys:
        product_id: str, product_name: str, avg_daily_sales: float,
        safety_stock: float, on_hand_qty: float
    supplier table row -> dict with keys:
        supplier_id: str, supplier_name: str, unit_price: float,
        lead_time_days: float, reliability: float (0-1)
    demand_context.txt -> free-text string (weather/calendar/sales notes),
        may be empty or missing -- the agent degrades gracefully either way.

Output of decide() -> dict:
    {
      "action":   "REORDER" | "HOLD",
      "supplier": <supplier dict>,
      "qty":      float,   # units to order (0 if HOLD)
      "d_adj":    float,   # demand-adjusted daily sales
      "rop":      float,   # reorder point
      "on_hand":  float,   # current on-hand qty
      "mult":     float,   # demand multiplier applied
      "why":      str,     # natural-language justification
    }

Output of act() -> str (human-readable PO / hold notice for that product).
Output of run() -> int exit code (0 success, 1 if no usable data at all).

------------------------------------------------------------------------------
RELIABILITY NOTES (fixes applied after validation report run 53596b7df5d3)
------------------------------------------------------------------------------
  - LLM call wrapped in try/except, given an explicit timeout, and retried
    with backoff on transient failures before giving up.
  - LLM JSON response parsed defensively (regex extraction + schema/range
    validation); any parse failure logs a warning and falls back to a
    neutral multiplier instead of crashing or feeding bad data into the
    ROP math.
  - DB and file I/O wrapped in try/except with clear, actionable error
    messages; a missing/corrupt context file degrades to "no signal"
    rather than crashing the whole run. A single product that fails to
    decide/act no longer takes down the rest of the run.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

logging.basicConfig(level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s")
log = logging.getLogger("smart_reorder_agent")

DB = os.path.join(os.path.dirname(__file__), "smartreorder.db")
CONTEXT = os.path.join(os.path.dirname(__file__), "demand_context.txt")

REVIEW_PERIOD_DAYS = 7  # how often we re-plan; used to size the order
PRICE_W, RELIA_W, SPEED_W = 0.4, 0.3, 0.3  # supplier scoring weights

# --- LLM call hardening knobs -------------------------------------------------
LLM_MODEL = "claude-sonnet-4-6"
LLM_TIMEOUT_SECONDS = 30.0
LLM_MAX_RETRIES = 2  # additional attempts after the first, on transient failure
LLM_RETRY_BACKOFF_SECONDS = 1.5
DEMAND_MULTIPLIER_MIN, DEMAND_MULTIPLIER_MAX = 0.5, 3.0
FALLBACK_MULTIPLIER, FALLBACK_REASON = 1.0, "fallback: LLM unavailable, used neutral multiplier"

Product = Dict[str, Any]
Supplier = Dict[str, Any]


# ---------------------------------------------------------------- perceive
def perceive(con: sqlite3.Connection) -> Tuple[List[Product], List[Supplier], str]:
    """
    Read products, suppliers, and free-text demand context.

    Args:
        con: an open sqlite3 connection to the SCM database.

    Returns:
        (products, suppliers, context) -- products/suppliers are lists of
        row-dicts; context is a string, empty if the file is missing,
        unreadable, or simply not present (never raises for context).

    Raises:
        RuntimeError: if the product/supplier tables can't be queried at
        all. The agent has nothing safe to decide on without them, so this
        is deliberately fatal here -- callers (see run()) catch it and
        exit cleanly instead of letting a raw sqlite3 traceback surface.
    """
    try:
        con.row_factory = sqlite3.Row
        products = [dict(r) for r in con.execute("SELECT * FROM product ORDER BY product_id")]
        suppliers = [dict(r) for r in con.execute("SELECT * FROM supplier ORDER BY supplier_id")]
    except sqlite3.Error as e:
        raise RuntimeError(f"perceive(): could not read product/supplier data from {DB}: {e}") from e

    context = ""
    try:
        if os.path.exists(CONTEXT):
            with open(CONTEXT, "r", encoding="utf-8") as f:
                context = f.read()
    except OSError as e:
        log.warning("perceive(): could not read demand context file %s (%s); continuing with no context.", CONTEXT, e)
        context = ""

    return products, suppliers, context


# ---------------------------------------------------------------- judgment seam 1
def predict_demand(product: Product, context: str, live: bool) -> Tuple[float, str]:
    """
    Return (multiplier, reason): how much to scale normal daily sales given
    the context.

    Args:
        product: a product row-dict (see module docstring for shape).
        context: free-text demand context (weather/calendar/sales notes).
        live: if True, ask the LLM (see _llm_predict); if False, use the
              recorded mock predictions below (offline, deterministic).

    Returns:
        (multiplier, reason) -- multiplier is always within
        [DEMAND_MULTIPLIER_MIN, DEMAND_MULTIPLIER_MAX]; reason is a short
        human-readable string. Never raises -- live-mode failures are
        caught internally and degrade to a fallback value.
    """
    if live:
        return _llm_predict(product, context)
    recorded = {  # what a good planner concludes from this context
        "P1": (1.5, "heatwave + July 4th weekend; cold-beverage surge"),
        "P2": (1.3, "heatwave lifts cola, but less holiday-driven than water"),
        "P3": (1.5, "heatwave + holiday + standing 15% upward sales trend"),
    }
    return recorded.get(product["product_id"], (1.0, "no signal"))


def _llm_predict(product: Product, context: str) -> Tuple[float, str]:
    """
    Ask the LLM to forecast a demand multiplier for `product` given `context`.

    Hardening applied here directly addresses the validator findings:
      - the whole call is wrapped in try/except (network/API errors never
        crash the agent or propagate)
      - an explicit request timeout is set (LLM_TIMEOUT_SECONDS)
      - transient failures are retried with backoff (LLM_MAX_RETRIES)
      - the response JSON is extracted and schema-validated defensively
        (see _parse_demand_response)
      - any failure mode at all (missing package, network, timeout,
        malformed JSON, out-of-range value) degrades to a logged, neutral
        fallback instead of bad data or a crash

    Returns:
        (multiplier, reason) -- always a valid value, even on total failure.
    """
    try:
        import anthropic
    except ImportError as e:
        log.error("_llm_predict(): 'anthropic' package not installed (%s); using fallback.", e)
        return FALLBACK_MULTIPLIER, FALLBACK_REASON

    prompt = (
        f"You forecast short-term demand. Product: {product['product_name']}, "
        f"normal daily sales {product['avg_daily_sales']}.\nContext:\n{context}\n\n"
        "Return ONLY JSON: {\"multiplier\": <float 0.5-3.0>, \"reason\": \"<short>\"}"
    )

    last_error: Optional[Exception] = None
    for attempt in range(1, LLM_MAX_RETRIES + 2):  # first attempt + retries
        try:
            client = anthropic.Anthropic(timeout=LLM_TIMEOUT_SECONDS)
            msg = client.messages.create(
                model=LLM_MODEL,
                max_tokens=200,
                timeout=LLM_TIMEOUT_SECONDS,
                messages=[{"role": "user", "content": prompt}],
            )
            text = msg.content[0].text
            return _parse_demand_response(text, product["product_id"])
        except Exception as e:  # network, timeout, API, SDK errors -- all treated as transient
            last_error = e
            log.warning(
                "_llm_predict(): attempt %d/%d failed for %s: %s",
                attempt, LLM_MAX_RETRIES + 1, product.get("product_id"), e,
            )
            if attempt <= LLM_MAX_RETRIES:
                time.sleep(LLM_RETRY_BACKOFF_SECONDS * attempt)  # linear backoff

    log.error(
        "_llm_predict(): all %d attempt(s) failed for %s (%s); falling back to neutral multiplier.",
        LLM_MAX_RETRIES + 1, product.get("product_id"), last_error,
    )
    return FALLBACK_MULTIPLIER, FALLBACK_REASON


def _parse_demand_response(text: str, product_id: str) -> Tuple[float, str]:
    """
    Defensively parse the LLM's JSON reply into (multiplier, reason).

    Uses a regex to locate the JSON object (robust to leading/trailing
    prose the model might add around it), validates the schema, and
    clamps the multiplier into the business-sane range. Any problem at
    all (no JSON found, invalid JSON, non-numeric/out-of-range multiplier,
    missing reason) returns the fallback instead of raising or silently
    passing bad data downstream into the ROP math.
    """
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        log.warning("_parse_demand_response(): no JSON object found for %s; raw text: %r", product_id, text[:200])
        return FALLBACK_MULTIPLIER, FALLBACK_REASON

    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError as e:
        log.warning("_parse_demand_response(): invalid JSON for %s (%s); raw: %r", product_id, e, match.group(0)[:200])
        return FALLBACK_MULTIPLIER, FALLBACK_REASON

    multiplier = data.get("multiplier")
    reason = data.get("reason", "")

    try:
        multiplier = float(multiplier)
    except (TypeError, ValueError):
        log.warning("_parse_demand_response(): non-numeric multiplier %r for %s; using fallback.", multiplier, product_id)
        return FALLBACK_MULTIPLIER, FALLBACK_REASON

    if not (DEMAND_MULTIPLIER_MIN <= multiplier <= DEMAND_MULTIPLIER_MAX):
        log.warning(
            "_parse_demand_response(): multiplier %.2f for %s out of range [%.1f, %.1f]; clamping.",
            multiplier, product_id, DEMAND_MULTIPLIER_MIN, DEMAND_MULTIPLIER_MAX,
        )
        multiplier = max(DEMAND_MULTIPLIER_MIN, min(DEMAND_MULTIPLIER_MAX, multiplier))

    if not isinstance(reason, str) or not reason.strip():
        reason = "LLM gave no reason"

    return multiplier, reason


# ---------------------------------------------------------------- judgment seam 2
def choose_supplier(suppliers: List[Supplier]) -> Tuple[Supplier, float]:
    """
    Weigh price vs reliability vs speed; return (best_supplier, score).

    Args:
        suppliers: non-empty list of supplier row-dicts.

    Returns:
        (best_supplier, score) -- the highest-scoring supplier and its score.

    Raises:
        ValueError: if `suppliers` is empty (nothing to choose between).
    """
    if not suppliers:
        raise ValueError("choose_supplier(): no suppliers available to choose from")

    min_price = min(s["unit_price"] for s in suppliers)
    min_lead = min(s["lead_time_days"] for s in suppliers)

    def score(s: Supplier) -> float:
        return (PRICE_W * (min_price / s["unit_price"]) +
                RELIA_W * s["reliability"] +
                SPEED_W * (min_lead / s["lead_time_days"]))

    best = max(suppliers, key=score)
    return best, score(best)


# ---------------------------------------------------------------- decide
def decide(product: Product, suppliers: List[Supplier], context: str, live: bool) -> Dict[str, Any]:
    """
    Combine demand prediction + ROP arithmetic + supplier choice into a
    single reorder decision for `product`.

    Args:
        product: a product row-dict.
        suppliers: list of supplier row-dicts (non-empty).
        context: free-text demand context.
        live: whether to use the live LLM for demand prediction.

    Returns:
        A decision dict -- see module docstring "Output of decide()" for
        the exact shape.
    """
    mult, why = predict_demand(product, context, live)
    d_adj = product["avg_daily_sales"] * mult  # d'
    supplier, _ = choose_supplier(suppliers)
    L = supplier["lead_time_days"]
    SS = product["safety_stock"]
    rop = d_adj * L + SS  # ROP = d'*L + SS
    on_hand = product["on_hand_qty"]

    if on_hand <= rop:
        target = d_adj * (L + REVIEW_PERIOD_DAYS) + SS  # order-up-to level
        qty = max(0, round(target - on_hand))
        return {"action": "REORDER", "supplier": supplier, "qty": qty,
                "d_adj": d_adj, "rop": rop, "on_hand": on_hand, "mult": mult, "why": why}

    return {"action": "HOLD", "supplier": supplier, "qty": 0,
            "d_adj": d_adj, "rop": rop, "on_hand": on_hand, "mult": mult, "why": why}


# ---------------------------------------------------------------- act
def act(product: Product, d: Dict[str, Any]) -> str:
    """Render a decide() decision into a human-readable PO / hold notice."""
    head = f"{product['product_id']} {product['product_name']:<16}"
    calc = (f"d'={d['d_adj']:.0f} (x{d['mult']}) ROP={d['rop']:.0f} "
            f"on_hand={d['on_hand']}")
    if d["action"] == "REORDER":
        s = d["supplier"]
        total = d["qty"] * s["unit_price"]
        body = (f"REORDER {d['qty']} units from {s['supplier_name']} "
                f"(${s['unit_price']:.2f}, lead {s['lead_time_days']}d) = ${total:,.2f}\n"
                f"   PO drafted -> sent to {s['supplier_name']}; "
                f"ERP marked ON ORDER; finance alerted.\n"
                f"   why: stock {d['on_hand']} <= ROP {d['rop']:.0f}; demand {d['why']}")
    else:
        body = (f"HOLD -- no order.\n"
                f"   why: stock {d['on_hand']} > ROP {d['rop']:.0f}; demand {d['why']}")
    return f"{head} | {calc}\n   {body}"


# ---------------------------------------------------------------- run
def run(live: bool = False) -> int:
    """
    Execute one perceive -> decide -> act cycle for every product.

    Args:
        live: if True, use the real LLM for demand prediction.

    Returns:
        Process exit code: 0 on success, 1 if the database couldn't be
        opened or product/supplier data couldn't be read at all.
    """
    try:
        con = sqlite3.connect(DB)
    except sqlite3.Error as e:
        log.error("run(): could not open database %s: %s", DB, e)
        return 1

    try:
        try:
            products, suppliers, context = perceive(con)
        finally:
            con.close()
    except RuntimeError as e:
        log.error("run(): %s", e)
        return 1

    if not suppliers:
        log.error("run(): no suppliers found; cannot make reorder decisions.")
        return 1

    mode = "LIVE (LLM)" if live else "MOCK (recorded)"
    print(f"Smart Reorder Agent -- goal: prevent stockouts, minimize holding cost [{mode}]")
    print("=" * 78)
    for p in products:
        try:
            print(act(p, decide(p, suppliers, context, live)))
        except Exception as e:
            # one bad product should not take down the whole run
            log.error("run(): failed to decide/act for product %s: %s", p.get("product_id"), e)
            print(f"{p.get('product_id', '?')} -- ERROR: could not compute a decision ({e})")
        print("-" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(run(live="--live" in sys.argv))
