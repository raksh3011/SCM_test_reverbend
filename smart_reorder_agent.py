"""
smart_reorder_agent.py
"""

import sqlite3
import os
import sys
import json

DB = os.path.join(
    os.path.dirname(__file__),
    "smartreorder.db"
)

CONTEXT = os.path.join(
    os.path.dirname(__file__),
    "demand_context.txt"
)

REVIEW_PERIOD_DAYS = 7

PRICE_W = 0.4
RELIA_W = 0.3
SPEED_W = 0.3


# --------------------------------------------------
# PERCEIVE
# --------------------------------------------------

def perceive(con):

    con.row_factory = sqlite3.Row

    products = [
        dict(r)
        for r in con.execute(
            "SELECT * FROM product ORDER BY product_id"
        )
    ]

    suppliers = [
        dict(r)
        for r in con.execute(
            "SELECT * FROM supplier ORDER BY supplier_id"
        )
    ]

    context = ""

    if os.path.exists(CONTEXT):
        with open(
            CONTEXT,
            "r",
            encoding="utf-8"
        ) as f:
            context = f.read()

    return products, suppliers, context


# --------------------------------------------------
# JUDGMENT SEAM 1
# --------------------------------------------------

def predict_demand(product, context, live):

    if live:
        return _llm_predict(
            product,
            context
        )

    recorded = {
        "P1": (
            1.5,
            "rainfall + flooding; bottled water demand surge"
        ),

        "P2": (
            1.0,
            "normal beverage demand"
        ),

        "P3": (
            1.5,
            "rainfall + seasonal demand uplift"
        )
    }

    return recorded.get(
        product["product_id"],
        (1.0, "no signal")
    )


def _llm_predict(product, context):

    import anthropic

    client = anthropic.Anthropic()

    prompt = (
        f"Product={product['product_name']}\n"
        f"Normal demand={product['avg_daily_sales']}\n\n"
        f"{context}\n\n"
        "Return ONLY JSON:\n"
        '{"multiplier":1.0,"reason":"text"}'
    )

    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=200,
        messages=[
            {
                "role": "user",
                "content": prompt
            }
        ]
    )

    txt = msg.content[0].text

    d = json.loads(
        txt[
            txt.find("{"):
            txt.rfind("}") + 1
        ]
    )

    return (
        float(d["multiplier"]),
        d["reason"]
    )


# --------------------------------------------------
# JUDGMENT SEAM 2
# --------------------------------------------------

def choose_supplier(suppliers):

    min_price = min(
        s["unit_price"]
        for s in suppliers
    )

    min_lead = min(
        s["lead_time_days"]
        for s in suppliers
    )

    def score(s):

        return (
            PRICE_W *
            (
                min_price /
                s["unit_price"]
            )
            +
            RELIA_W *
            s["reliability"]
            +
            SPEED_W *
            (
                min_lead /
                s["lead_time_days"]
            )
        )

    best = max(
        suppliers,
        key=score
    )

    return best, score(best)


# --------------------------------------------------
# DECIDE
# --------------------------------------------------

def decide(
    product,
    suppliers,
    context,
    live
):

    mult, why = predict_demand(
        product,
        context,
        live
    )

    d_adj = (
        product["avg_daily_sales"]
        * mult
    )

    supplier, _ = choose_supplier(
        suppliers
    )

    L = supplier["lead_time_days"]

    SS = product["safety_stock"]

    rop = d_adj * L + SS

    on_hand = product["on_hand_qty"]

    if on_hand <= rop:

        target = (
            d_adj *
            (
                L +
                REVIEW_PERIOD_DAYS
            )
            + SS
        )

        qty = max(
            0,
            round(
                target - on_hand
            )
        )

        return {
            "action": "REORDER",
            "supplier": supplier,
            "qty": qty,
            "d_adj": d_adj,
            "rop": rop,
            "on_hand": on_hand,
            "mult": mult,
            "why": why,
        }

    return {
        "action": "HOLD",
        "supplier": supplier,
        "qty": 0,
        "d_adj": d_adj,
        "rop": rop,
        "on_hand": on_hand,
        "mult": mult,
        "why": why,
    }


# --------------------------------------------------
# ACT
# --------------------------------------------------

def act(product, d):

    head = (
        f"{product['product_id']} "
        f"{product['product_name']:<16}"
    )

    calc = (
        f"d'={d['d_adj']:.0f} "
        f"(x{d['mult']}) "
        f"ROP={d['rop']:.0f} "
        f"on_hand={d['on_hand']}"
    )

    if d["action"] == "REORDER":

        s = d["supplier"]

        total = (
            d["qty"] *
            s["unit_price"]
        )

        body = (
            f"REORDER {d['qty']} units "
            f"from {s['supplier_name']} "
            f"(${s['unit_price']:.2f}) "
            f"= ${total:,.2f}\n"
            f"        why: "
            f"stock {d['on_hand']} "
            f"<= ROP {d['rop']:.0f}; "
            f"{d['why']}"
        )

    else:

        body = (
            "HOLD — no order.\n"
            f"        why: "
            f"stock {d['on_hand']} "
            f"> ROP {d['rop']:.0f}; "
            f"{d['why']}"
        )

    return (
        f"{head} | {calc}\n"
        f"        {body}"
    )


# --------------------------------------------------
# RUN
# --------------------------------------------------

def run(live=False):

    con = sqlite3.connect(DB)

    products, suppliers, context = perceive(con)

    con.close()

    mode = (
        "LIVE"
        if live
        else "MOCK"
    )

    print(
        f"Smart Reorder Agent "
        f"[{mode}]"
    )

    print("=" * 70)

    for p in products:

        decision = decide(
            p,
            suppliers,
            context,
            live
        )

        print(
            act(
                p,
                decision
            )
        )

        print("-" * 70)


if __name__ == "__main__":

    run(
        live="--live" in sys.argv
    )