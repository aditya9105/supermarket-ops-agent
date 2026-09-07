"""
Billing tools — the heart of the agent.

Key design decisions:
─────────────────────
1. GST calc (MRP-inclusive / reverse-extraction):
   - Indian law: MRP is the maximum price INCLUSIVE of all taxes.
   - taxable_amt = round(unit_price × qty / (1 + slab/100), 2)   ← pre-tax base
   - total_gst   = round(unit_price × qty − taxable_amt, 2)       ← GST extracted from MRP
   - cgst = sgst = round(total_gst / 2, 2)
   - line_total  = round(unit_price × qty, 2)                     ← always = MRP × qty
   - grand_total = sum(line_totals), ROUNDED to nearest rupee (standard Indian kirana practice)
   - 0% GST items: taxable_amt = line_total, cgst = sgst = 0 (no change in behaviour)

2. Oversell guard:
   - finalize_bill uses BEGIN IMMEDIATE transaction.
   - Per-line UPDATE: SET stock_qty = stock_qty - qty WHERE sku_id=? AND stock_qty >= qty
   - rowcount check: if 0 rows updated → stock insufficient → ROLLBACK entire bill.
   - This is enforced at DB layer, not by prompt.

3. Idempotency:
   - finalize_bill requires an idempotency_key.
   - Before any mutation, check idempotency_keys table.
   - If found → return stored result_json immediately (no-op).
   - Written atomically inside the same transaction as stock decrements.

4. Multi-turn:
   - add/edit/remove operate on DRAFT bills only.
   - Stock is never touched until finalize_bill.

5. Below-cost guardrail:
   - add_bill_item and edit_bill_item check unit_price >= cost_price.
   - Returns {"error": "below_cost", ...} unless confirm_below_cost=True.
"""
import json
import logging
import math
from datetime import date, datetime
from db.connection import get_conn

logger = logging.getLogger(__name__)


# ─── GST helpers ──────────────────────────────────────────────────────────────

def _calc_line(unit_price: float, qty: float, tax_slab: int) -> dict:
    """
    Compute per-line GST figures using MRP-inclusive (reverse-extraction) method.

    Indian law mandates MRP is the maximum price inclusive of all taxes.
    The customer never pays more than unit_price × qty.

        line_total  = unit_price × qty                          (= MRP × qty)
        taxable_amt = line_total / (1 + tax_slab/100)          (pre-tax base)
        total_gst   = line_total − taxable_amt
        cgst = sgst = total_gst / 2
    """
    line_total  = round(unit_price * qty, 2)
    if tax_slab == 0:
        return {
            "taxable_amt": line_total,
            "cgst_amt":    0.0,
            "sgst_amt":    0.0,
            "line_total":  line_total,
        }
    inclusive_divisor = 1 + tax_slab / 100
    taxable   = round(line_total / inclusive_divisor, 2)
    total_gst = round(line_total - taxable, 2)
    cgst      = round(total_gst / 2, 2)
    sgst      = round(total_gst - cgst, 2)   # absorbs ±0.01 rounding remainder
    return {
        "taxable_amt": taxable,
        "cgst_amt":    cgst,
        "sgst_amt":    sgst,
        "line_total":  line_total,
    }


def _round_to_rupee(amount: float) -> float:
    """Standard Indian retail rounding: nearest rupee."""
    return float(round(amount))


def _bill_summary(bill_id: int, conn) -> dict:
    """Build a preview of a draft bill from DB rows (no side effects)."""
    bill = conn.execute(
        "SELECT * FROM bills WHERE bill_id = ?", (bill_id,)
    ).fetchone()
    if not bill:
        return {"error": "bill_not_found", "bill_id": bill_id}

    lines_rows = conn.execute(
        """SELECT bi.line_id, bi.sku_id, bi.qty, bi.unit_price,
                  bi.tax_slab, bi.hsn_code,
                  p.name, p.brand, p.unit
           FROM bill_items bi
           JOIN products p ON p.sku_id = bi.sku_id
           WHERE bi.bill_id = ?
           ORDER BY bi.line_id""",
        (bill_id,),
    ).fetchall()

    lines = []
    subtotal = 0.0
    cgst_total = 0.0
    sgst_total = 0.0

    for r in lines_rows:
        calc = _calc_line(r["unit_price"], r["qty"], r["tax_slab"])
        display = f"{r['brand']} {r['name']}".strip()
        lines.append({
            "line_id":    r["line_id"],
            "sku_id":     r["sku_id"],
            "display_name": display,
            "qty":        r["qty"],
            "unit":       r["unit"],
            "unit_price": r["unit_price"],
            "tax_slab":   r["tax_slab"],
            "hsn_code":   r["hsn_code"],
            **calc,
        })
        subtotal   += calc["taxable_amt"]
        cgst_total += calc["cgst_amt"]
        sgst_total += calc["sgst_amt"]

    grand_total = _round_to_rupee(subtotal + cgst_total + sgst_total)

    return {
        "bill_id":      bill_id,
        "status":       bill["status"],
        "customer_name": bill["customer_name"],
        "lines":        lines,
        "subtotal":     round(subtotal, 2),
        "cgst_total":   round(cgst_total, 2),
        "sgst_total":   round(sgst_total, 2),
        "grand_total":  grand_total,
        "item_count":   len(lines),
        "rounding_note": "Grand total rounded to nearest rupee (standard Indian retail practice).",
    }


# ─── tool implementations ──────────────────────────────────────────────────────

def start_bill(idempotency_key: str, customer_name: str | None = None, customer_id: int | None = None) -> dict:
    """
    Create a new DRAFT bill.
    Idempotent: if the key was already used to start a bill, return the existing bill_id.
    """
    conn = get_conn()

    # Idempotency check (start_bill uses key prefix to avoid collisions with finalize)
    start_key = f"start:{idempotency_key}"
    existing = conn.execute(
        "SELECT result_json FROM idempotency_keys WHERE idem_key = ?", (start_key,)
    ).fetchone()
    if existing:
        return json.loads(existing["result_json"])

    conn.execute("BEGIN IMMEDIATE")
    try:
        cursor = conn.execute(
            """INSERT INTO bills (customer_name, customer_id, status)
               VALUES (?, ?, 'DRAFT')""",
            (customer_name, customer_id),
        )
        bill_id = cursor.lastrowid
        result = {"status": "ok", "bill_id": bill_id, "message": f"Bill #{bill_id} started."}
        conn.execute(
            "INSERT INTO idempotency_keys (idem_key, bill_id, result_json) VALUES (?, ?, ?)",
            (start_key, bill_id, json.dumps(result)),
        )
        conn.commit()
        logger.info(f"Started bill_id={bill_id}")
        return result
    except Exception as e:
        conn.rollback()
        logger.error(f"start_bill failed: {e}")
        return {"error": "db_error", "detail": str(e)}


def add_bill_item(
    bill_id: int,
    sku_id: int,
    qty: float,
    unit_price: float | None = None,
    confirm_below_cost: bool = False,
) -> dict:
    """
    Add a line item to a DRAFT bill.
    - unit_price defaults to product MRP if omitted.
    - Guardrail: if unit_price < cost_price and confirm_below_cost is False → refuse.
    - Does NOT decrement stock (happens only at finalize).
    """
    if qty <= 0:
        return {"error": "invalid_qty", "message": "Quantity must be positive."}

    conn = get_conn()
    bill = conn.execute(
        "SELECT bill_id, status FROM bills WHERE bill_id = ?", (bill_id,)
    ).fetchone()
    if not bill:
        return {"error": "bill_not_found", "bill_id": bill_id}
    if bill["status"] != "DRAFT":
        return {"error": "bill_not_draft", "status": bill["status"]}

    product = conn.execute(
        "SELECT sku_id, name, brand, unit, mrp, cost_price, stock_qty, tax_slab, hsn_code FROM products WHERE sku_id = ? AND active = 1",
        (sku_id,),
    ).fetchone()
    if not product:
        return {"error": "product_not_found", "sku_id": sku_id}

    effective_price = unit_price if unit_price is not None else product["mrp"]

    # Below-cost guardrail
    if effective_price < product["cost_price"] and not confirm_below_cost:
        return {
            "error": "below_cost",
            "message": (
                f"Selling price ₹{effective_price} is below cost price ₹{product['cost_price']}. "
                "Call again with confirm_below_cost=true to override."
            ),
            "cost_price": product["cost_price"],
            "unit_price": effective_price,
        }

    # Stock availability pre-check (soft — enforced hard at finalize)
    if qty > product["stock_qty"]:
        return {
            "error": "insufficient_stock",
            "message": f"Only {product['stock_qty']} {product['unit']} in stock, requested {qty}.",
            "available": product["stock_qty"],
            "requested": qty,
        }

    calc = _calc_line(effective_price, qty, product["tax_slab"])
    display = f"{product['brand']} {product['name']}".strip()

    conn.execute("BEGIN IMMEDIATE")
    try:
        cursor = conn.execute(
            """INSERT INTO bill_items
               (bill_id, sku_id, qty, unit_price, tax_slab, hsn_code,
                taxable_amt, cgst_amt, sgst_amt, line_total)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (bill_id, sku_id, qty, effective_price,
             product["tax_slab"], product["hsn_code"],
             calc["taxable_amt"], calc["cgst_amt"], calc["sgst_amt"], calc["line_total"]),
        )
        line_id = cursor.lastrowid
        conn.commit()
        return {
            "status": "ok",
            "line_id": line_id,
            "display_name": display,
            "qty": qty,
            "unit": product["unit"],
            "unit_price": effective_price,
            **calc,
            "message": f"Added {qty} × {display} @ ₹{effective_price}.",
        }
    except Exception as e:
        conn.rollback()
        return {"error": "db_error", "detail": str(e)}


def edit_bill_item(
    bill_id: int,
    line_id: int,
    new_qty: float | None = None,
    new_unit_price: float | None = None,
    confirm_below_cost: bool = False,
) -> dict:
    """Update qty and/or price on an existing draft line item."""
    if new_qty is None and new_unit_price is None:
        return {"error": "no_change", "message": "Provide new_qty and/or new_unit_price."}

    conn = get_conn()
    bill = conn.execute(
        "SELECT status FROM bills WHERE bill_id = ?", (bill_id,)
    ).fetchone()
    if not bill or bill["status"] != "DRAFT":
        return {"error": "bill_not_draft", "bill_id": bill_id}

    row = conn.execute(
        """SELECT bi.line_id, bi.qty, bi.unit_price, bi.sku_id,
                  p.cost_price, p.stock_qty, p.tax_slab, p.unit, p.name, p.brand
           FROM bill_items bi
           JOIN products p ON p.sku_id = bi.sku_id
           WHERE bi.line_id = ? AND bi.bill_id = ?""",
        (line_id, bill_id),
    ).fetchone()
    if not row:
        return {"error": "line_not_found", "line_id": line_id}

    eff_qty   = new_qty        if new_qty        is not None else row["qty"]
    eff_price = new_unit_price if new_unit_price is not None else row["unit_price"]

    if eff_qty <= 0:
        return {"error": "invalid_qty", "message": "Quantity must be positive."}

    if eff_price < row["cost_price"] and not confirm_below_cost:
        return {
            "error": "below_cost",
            "message": (
                f"Price ₹{eff_price} is below cost price ₹{row['cost_price']}. "
                "Pass confirm_below_cost=true to override."
            ),
            "cost_price": row["cost_price"],
        }

    if eff_qty > row["stock_qty"]:
        return {
            "error": "insufficient_stock",
            "available": row["stock_qty"],
            "requested": eff_qty,
        }

    calc = _calc_line(eff_price, eff_qty, row["tax_slab"])
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """UPDATE bill_items
               SET qty = ?, unit_price = ?,
                   taxable_amt = ?, cgst_amt = ?, sgst_amt = ?, line_total = ?
               WHERE line_id = ?""",
            (eff_qty, eff_price,
             calc["taxable_amt"], calc["cgst_amt"], calc["sgst_amt"], calc["line_total"],
             line_id),
        )
        conn.commit()
        display = f"{row['brand']} {row['name']}".strip()
        return {
            "status": "ok",
            "line_id": line_id,
            "display_name": display,
            "new_qty": eff_qty,
            "new_unit_price": eff_price,
            **calc,
            "message": f"Updated line {line_id}: {eff_qty} × {display} @ ₹{eff_price}.",
        }
    except Exception as e:
        conn.rollback()
        return {"error": "db_error", "detail": str(e)}


def remove_bill_item(bill_id: int, line_id: int) -> dict:
    """Remove a line item from a draft bill."""
    conn = get_conn()
    bill = conn.execute(
        "SELECT status FROM bills WHERE bill_id = ?", (bill_id,)
    ).fetchone()
    if not bill or bill["status"] != "DRAFT":
        return {"error": "bill_not_draft", "bill_id": bill_id}

    affected = conn.execute(
        "DELETE FROM bill_items WHERE line_id = ? AND bill_id = ?",
        (line_id, bill_id),
    ).rowcount
    conn.commit()
    if affected == 0:
        return {"error": "line_not_found", "line_id": line_id}
    return {"status": "ok", "message": f"Removed line {line_id} from bill #{bill_id}."}


def get_bill_preview(bill_id: int) -> dict:
    """
    Return full draft summary with per-item GST calc and totals.
    No side effects. Safe to call multiple times.
    """
    conn = get_conn()
    return _bill_summary(bill_id, conn)


def finalize_bill(
    bill_id: int,
    payment_mode: str,
    idempotency_key: str,
    payment_ref: str | None = None,
) -> dict:
    """
    Finalize a DRAFT bill.

    This is the most critical function — it performs, atomically:
    1. Idempotency check: if key already processed → return stored receipt.
    2. Validate bill is DRAFT and has at least one item.
    3. For each line item, atomically decrement stock:
       UPDATE products SET stock_qty = stock_qty - qty
       WHERE sku_id = ? AND stock_qty >= qty
       → if rowcount = 0 → INSUFFICIENT STOCK → ROLLBACK entire bill.
    4. Recalculate all GST figures and update bill_items.
    5. Mark bill PAID with totals, payment info, timestamp.
    6. Write stock_movements audit rows.
    7. Write idempotency record.
    All in one BEGIN IMMEDIATE transaction — if anything fails, the whole thing rolls back.
    """
    payment_mode = payment_mode.upper()
    if payment_mode not in ("CASH", "UPI", "CARD"):
        return {"error": "invalid_payment_mode", "allowed": ["CASH", "UPI", "CARD"]}

    conn = get_conn()

    # ── Step 1: Idempotency check ─────────────────────────────────────────────
    existing = conn.execute(
        "SELECT result_json FROM idempotency_keys WHERE idem_key = ?",
        (idempotency_key,),
    ).fetchone()
    if existing:
        logger.info(f"Idempotent replay for finalize key={idempotency_key}")
        return json.loads(existing["result_json"])

    # ── Step 2: Load bill + lines ─────────────────────────────────────────────
    conn.execute("BEGIN IMMEDIATE")
    try:
        bill = conn.execute(
            "SELECT * FROM bills WHERE bill_id = ?", (bill_id,)
        ).fetchone()
        if not bill:
            conn.rollback()
            return {"error": "bill_not_found", "bill_id": bill_id}
        if bill["status"] != "DRAFT":
            conn.rollback()
            return {"error": "bill_not_draft", "status": bill["status"],
                    "message": f"Bill #{bill_id} is already {bill['status']}."}

        lines = conn.execute(
            """SELECT bi.line_id, bi.sku_id, bi.qty, bi.unit_price,
                      bi.tax_slab, bi.hsn_code,
                      p.name, p.brand, p.unit, p.cost_price, p.stock_qty
               FROM bill_items bi
               JOIN products p ON p.sku_id = bi.sku_id
               WHERE bi.bill_id = ?
               ORDER BY bi.line_id""",
            (bill_id,),
        ).fetchall()

        if not lines:
            conn.rollback()
            return {"error": "empty_bill", "message": "Cannot finalize an empty bill."}

        # ── Step 3: Decrement stock atomically per line ────────────────────────
        subtotal = 0.0; cgst_total = 0.0; sgst_total = 0.0
        receipt_lines = []

        for line in lines:
            affected = conn.execute(
                """UPDATE products
                   SET stock_qty = stock_qty - ?, updated_at = datetime('now')
                   WHERE sku_id = ? AND stock_qty >= ? AND active = 1""",
                (line["qty"], line["sku_id"], line["qty"]),
            ).rowcount

            if affected == 0:
                # Could not decrement — insufficient stock or product gone inactive
                conn.rollback()
                display = f"{line['brand']} {line['name']}".strip()
                current_stock = conn.execute(
                    "SELECT stock_qty FROM products WHERE sku_id = ?", (line["sku_id"],)
                ).fetchone()
                avail = current_stock["stock_qty"] if current_stock else 0
                return {
                    "error": "insufficient_stock",
                    "sku_id": line["sku_id"],
                    "product": display,
                    "requested": line["qty"],
                    "available": avail,
                    "message": (
                        f"Not enough stock for '{display}': "
                        f"need {line['qty']} {line['unit']}, have {avail}. Bill NOT finalized."
                    ),
                }

            # Recalculate GST for receipt
            calc = _calc_line(line["unit_price"], line["qty"], line["tax_slab"])
            subtotal   += calc["taxable_amt"]
            cgst_total += calc["cgst_amt"]
            sgst_total += calc["sgst_amt"]

            # Update stored line figures
            conn.execute(
                """UPDATE bill_items
                   SET taxable_amt=?, cgst_amt=?, sgst_amt=?, line_total=?
                   WHERE line_id=?""",
                (calc["taxable_amt"], calc["cgst_amt"], calc["sgst_amt"],
                 calc["line_total"], line["line_id"]),
            )

            # Audit trail
            conn.execute(
                """INSERT INTO stock_movements (sku_id, delta, reason, bill_id)
                   VALUES (?, ?, 'SALE', ?)""",
                (line["sku_id"], -line["qty"], bill_id),
            )

            display = f"{line['brand']} {line['name']}".strip()
            receipt_lines.append({
                "line_id":    line["line_id"],
                "display_name": display,
                "qty":        line["qty"],
                "unit":       line["unit"],
                "unit_price": line["unit_price"],
                "tax_slab":   line["tax_slab"],
                "hsn_code":   line["hsn_code"],
                **calc,
            })

        grand_total = _round_to_rupee(subtotal + cgst_total + sgst_total)
        bill_date   = date.today().isoformat()

        # ── Step 4: Mark bill PAID ─────────────────────────────────────────────
        conn.execute(
            """UPDATE bills
               SET status='PAID', payment_mode=?, payment_ref=?,
                   subtotal=?, cgst_total=?, sgst_total=?, grand_total=?,
                   bill_date=?
               WHERE bill_id=?""",
            (payment_mode, payment_ref,
             round(subtotal, 2), round(cgst_total, 2),
             round(sgst_total, 2), grand_total,
             bill_date, bill_id),
        )

        # ── Step 5: Write idempotency record ───────────────────────────────────
        result = {
            "status": "ok",
            "bill_id": bill_id,
            "bill_date": bill_date,
            "payment_mode": payment_mode,
            "payment_ref": payment_ref,
            "lines": receipt_lines,
            "subtotal": round(subtotal, 2),
            "cgst_total": round(cgst_total, 2),
            "sgst_total": round(sgst_total, 2),
            "grand_total": grand_total,
            "message": (
                f"Bill #{bill_id} finalized. "
                f"Total: ₹{grand_total} via {payment_mode}."
            ),
        }
        conn.execute(
            """INSERT INTO idempotency_keys (idem_key, bill_id, result_json)
               VALUES (?, ?, ?)""",
            (idempotency_key, bill_id, json.dumps(result)),
        )
        conn.commit()
        logger.info(
            f"Finalized bill_id={bill_id} total=₹{grand_total} mode={payment_mode}"
        )
        return result

    except Exception as e:
        conn.rollback()
        logger.error(f"finalize_bill failed: {e}")
        return {"error": "db_error", "detail": str(e)}


def cancel_bill(bill_id: int) -> dict:
    """
    Cancel a DRAFT bill. Cannot cancel a PAID or already-CANCELLED bill.
    """
    conn = get_conn()
    bill = conn.execute(
        "SELECT status FROM bills WHERE bill_id = ?", (bill_id,)
    ).fetchone()
    if not bill:
        return {"error": "bill_not_found", "bill_id": bill_id}
    if bill["status"] == "PAID":
        return {"error": "cannot_cancel_paid", "message": f"Bill #{bill_id} is already PAID. Cannot cancel."}
    if bill["status"] == "CANCELLED":
        return {"error": "already_cancelled", "message": f"Bill #{bill_id} is already cancelled."}

    conn.execute(
        "UPDATE bills SET status='CANCELLED' WHERE bill_id=?", (bill_id,)
    )
    conn.commit()
    return {"status": "ok", "message": f"Bill #{bill_id} cancelled."}


def get_open_draft_bill() -> dict:
    """
    Return the most recent DRAFT bill and its full preview.

    Use this when the user refers to an open bill (add items, edit, preview,
    finalize) but the current conversation context does not contain a bill_id.
    This lets the model recover the active draft after a context loss (e.g.
    bot restart, duplicate process, or history truncation).

    Returns:
        {"draft_bill": <full bill preview dict>}  — if a DRAFT exists.
        {"draft_bill": null, "message": "No open draft bill."}  — if none.
    """
    conn = get_conn()
    row = conn.execute(
        "SELECT bill_id FROM bills WHERE status = 'DRAFT' ORDER BY bill_id DESC LIMIT 1"
    ).fetchone()
    if not row:
        return {"draft_bill": None, "message": "No open draft bill."}
    preview = _bill_summary(row["bill_id"], conn)
    return {"draft_bill": preview}


# ─── Tool schemas ─────────────────────────────────────────────────────────────

BILLING_TOOLS = [
    {
        "name": "start_bill",
        "description": (
            "Start a new draft bill. Returns a bill_id to use for subsequent add/edit/finalize calls. "
            "Idempotent: same key always returns the same bill_id."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "idempotency_key": {"type": "string", "description": "Use the idem_key value from the [CONTEXT] prefix injected at the start of every user message. Do NOT invent or generate your own key."},
                "customer_name":   {"type": "string", "description": "Optional customer name for this bill."},
                "customer_id":     {"type": "integer", "description": "Optional customer DB ID if known."},
            },
            "required": ["idempotency_key"],
        },
    },
    {
        "name": "add_bill_item",
        "description": (
            "Add a product line to a draft bill. "
            "unit_price defaults to MRP. "
            "Will refuse if price < cost price unless confirm_below_cost=true. "
            "Does NOT decrement stock — that happens only on finalize."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "bill_id":           {"type": "integer"},
                "sku_id":            {"type": "integer", "description": "From search_products result."},
                "qty":               {"type": "number",  "description": "Quantity to sell."},
                "unit_price":        {"type": "number",  "description": "Sell price per unit. Defaults to MRP if omitted."},
                "confirm_below_cost":{"type": "boolean", "description": "Set true to override below-cost guardrail.", "default": False},
            },
            "required": ["bill_id", "sku_id", "qty"],
        },
    },
    {
        "name": "edit_bill_item",
        "description": "Edit the quantity or price of an existing line item in a draft bill.",
        "input_schema": {
            "type": "object",
            "properties": {
                "bill_id":            {"type": "integer"},
                "line_id":            {"type": "integer", "description": "Line ID from add_bill_item or get_bill_preview."},
                "new_qty":            {"type": "number"},
                "new_unit_price":     {"type": "number"},
                "confirm_below_cost": {"type": "boolean", "default": False},
            },
            "required": ["bill_id", "line_id"],
        },
    },
    {
        "name": "remove_bill_item",
        "description": "Remove a line item from a draft bill.",
        "input_schema": {
            "type": "object",
            "properties": {
                "bill_id": {"type": "integer"},
                "line_id": {"type": "integer"},
            },
            "required": ["bill_id", "line_id"],
        },
    },
    {
        "name": "get_bill_preview",
        "description": (
            "Preview the current draft bill with full GST breakdown: "
            "per-item taxable amount, CGST, SGST, line total, and grand total. "
            "No side effects — safe to call anytime."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "bill_id": {"type": "integer"},
            },
            "required": ["bill_id"],
        },
    },
    {
        "name": "finalize_bill",
        "description": (
            "Finalize a draft bill: decrement stock, record payment, produce receipt. "
            "Idempotent — duplicate calls with the same key return the original receipt. "
            "Will refuse if any item exceeds available stock (entire bill rolled back). "
            "payment_mode must be CASH, UPI, or CARD."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "bill_id":         {"type": "integer"},
                "payment_mode":    {"type": "string", "enum": ["CASH","UPI","CARD"]},
                "idempotency_key": {"type": "string", "description": "Use the idem_key value from the [CONTEXT] prefix. The same key used for start_bill must be used here to guarantee idempotency. Never invent a different key."},
                "payment_ref":     {"type": "string", "description": "UPI transaction ID or card last-4, if applicable."},
            },
            "required": ["bill_id", "payment_mode", "idempotency_key"],
        },
    },
    {
        "name": "cancel_bill",
        "description": "Cancel a draft bill. Cannot cancel a bill that is already PAID.",
        "input_schema": {
            "type": "object",
            "properties": {
                "bill_id": {"type": "integer"},
            },
            "required": ["bill_id"],
        },
    },
    {
        "name": "get_open_draft_bill",
        "description": (
            "Return the most recent open DRAFT bill and its full item preview. "
            "Call this when the user wants to continue, edit, preview, or finalize "
            "a bill but you do not have an active bill_id in your current context — "
            "do NOT tell the owner there is no open bill until you have called this first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
]

BILLING_HANDLERS: dict[str, callable] = {
    "start_bill":      lambda args: start_bill(**args),
    "add_bill_item":   lambda args: add_bill_item(**args),
    "edit_bill_item":  lambda args: edit_bill_item(**args),
    "remove_bill_item": lambda args: remove_bill_item(**args),
    "get_bill_preview":      lambda args: get_bill_preview(**args),
    "finalize_bill":         lambda args: finalize_bill(**args),
    "cancel_bill":           lambda args: cancel_bill(**args),
    "get_open_draft_bill":   lambda args: get_open_draft_bill(**args),
}
