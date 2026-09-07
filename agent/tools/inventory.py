"""
Inventory tools — all five tool functions that manage the product catalogue and stock.

Design notes:
- Every function queries the DB and returns structured dicts — the model NEVER
  invents a product or price; it must use what these functions return.
- search_products returns ALL matches including partial/fuzzy hits so the model
  can surface them and ask the owner to disambiguate naturally.
- receive_stock uses BEGIN IMMEDIATE to serialise concurrent stock-in events.
- Deletion is NOT exposed as a tool — products can only be deactivated.
"""
import json
import logging
from datetime import datetime
from db.connection import get_conn

logger = logging.getLogger(__name__)

# ─── helpers ──────────────────────────────────────────────────────────────────

def _row_to_dict(row) -> dict:
    return dict(row) if row else {}


def _product_dict(row) -> dict:
    d = _row_to_dict(row)
    # Friendly label for display
    d["display_name"] = f"{d.get('brand', '')} {d.get('name', '')}".strip()
    return d


# ─── tool implementations ──────────────────────────────────────────────────────

def search_products(query: str, include_inactive: bool = False) -> dict:
    """
    Full-text fuzzy search over name + brand using fuzzy normalization.

    The ``norm()`` SQLite scalar (registered in db/connection.py) strips
    apostrophes, hyphens, and dots and lowercases both the stored values and
    the query string before comparison.  This means minor punctuation variants
    of the same brand ("Haldiram" vs "Haldiram's", "Amul-Lite" vs "Amul Lite")
    are treated as identical for both matching and ranking.

    Returns up to 10 matching products so the model can surface options.
    If exactly one match → model can proceed directly.
    If multiple matches → model should ask owner to choose.
    """
    conn = get_conn()
    raw = query.strip()
    # Normalize the Python-side query too before building the LIKE pattern,
    # so apostrophes/hyphens in the user's search term are ignored.
    from db.connection import _normalize as _py_norm
    norm_q = f"%{_py_norm(raw)}%"
    active_filter = "" if include_inactive else "AND active = 1"
    rows = conn.execute(
        f"""
        SELECT sku_id, name, brand, unit, mrp, cost_price, stock_qty,
               reorder_level, tax_slab, hsn_code, is_loose, active
        FROM products
        WHERE (
                norm(name)                    LIKE ?
             OR norm(brand)                   LIKE ?
             OR norm(brand || ' ' || name)    LIKE ?
             OR norm(name || ' ' || brand)    LIKE ?
          )
          {active_filter}
        ORDER BY
            CASE
              -- Exact normalized match (highest priority)
              WHEN norm(name)                  = norm(?)
                OR norm(brand || ' ' || name)  = norm(?) THEN 0
              -- Normalized prefix / starts-with match
              WHEN norm(name)  LIKE norm(?) || '%'
                OR norm(brand) LIKE norm(?) || '%' THEN 1
              -- Normalized substring match (lowest priority)
              ELSE 2
            END,
            name
        LIMIT 10
        """,
        # WHERE params (4)
        (norm_q, norm_q, norm_q, norm_q,
        # ORDER BY params (4)
         raw, raw, raw, raw),
    ).fetchall()

    products = [_product_dict(r) for r in rows]
    return {
        "count": len(products),
        "products": products,
        "ambiguous": len(products) > 1,
    }


def get_product(sku_id: int) -> dict:
    """
    Fetch a single product by its SKU ID.
    Returns error if not found or inactive.
    """
    conn = get_conn()
    row = conn.execute(
        """SELECT sku_id, name, brand, unit, mrp, cost_price, stock_qty,
                  reorder_level, tax_slab, hsn_code, is_loose, active
           FROM products WHERE sku_id = ?""",
        (sku_id,),
    ).fetchone()
    if not row:
        return {"error": "product_not_found", "sku_id": sku_id}
    return _product_dict(row)


def add_product(
    name: str,
    brand: str,
    unit: str,
    mrp: float,
    tax_slab: int,
    hsn_code: str,
    cost_price: float | None = None,
    reorder_level: float = 0,
    initial_qty: float = 0,
    is_loose: bool = False,
) -> dict:
    """
    Add a new SKU to the catalogue.

    ``cost_price`` is intentionally optional: a product can be catalogued with
    its MRP, GST slab, and HSN code before any stock has been received.  The
    cost price is set (or updated) when stock arrives via ``receive_stock``.

    Grounding rule: never invent a cost price or opening stock quantity.
    Only use values the owner explicitly provides.

    Validates: unit ∈ allowed set, tax_slab ∈ {0,5,12,18},
               mrp ≥ cost_price (only checked when cost_price is given).
    """
    valid_units = {"kg", "g", "litre", "ml", "packet", "dozen", "piece"}
    if unit not in valid_units:
        return {"error": "invalid_unit", "allowed": sorted(valid_units)}

    if tax_slab not in (0, 5, 12, 18):
        return {"error": "invalid_tax_slab", "allowed": [0, 5, 12, 18]}

    # Only enforce the MRP ≥ cost guard when the caller actually supplied a cost.
    if cost_price is not None and mrp < cost_price:
        return {
            "error": "mrp_below_cost",
            "message": f"MRP ₹{mrp} is less than cost price ₹{cost_price}. Refusing to add.",
        }

    conn = get_conn()
    conn.execute("BEGIN IMMEDIATE")
    try:
        cursor = conn.execute(
            """INSERT INTO products
               (name, brand, unit, mrp, cost_price, stock_qty, reorder_level,
                tax_slab, hsn_code, is_loose)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (name.strip(), brand.strip(), unit, mrp, cost_price,
             initial_qty, reorder_level, tax_slab, hsn_code.strip(),
             1 if is_loose else 0),
        )
        sku_id = cursor.lastrowid
        if initial_qty > 0:
            conn.execute(
                """INSERT INTO stock_movements (sku_id, delta, reason)
                   VALUES (?, ?, 'RECEIVE')""",
                (sku_id, initial_qty),
            )
        conn.commit()
        logger.info(f"Added product sku_id={sku_id} name='{name}' brand='{brand}'")
        cost_note = f", cost price ₹{cost_price}" if cost_price is not None else " (cost price not yet set)"
        stock_note = f", opening stock {initial_qty} {unit}" if initial_qty > 0 else ", no opening stock"
        return {
            "status": "ok",
            "sku_id": sku_id,
            "message": (
                f"Added '{(brand + ' ' + name).strip()}' (SKU {sku_id})"
                f"{cost_note}{stock_note}."
            ),
        }
    except Exception as e:
        conn.rollback()
        logger.error(f"add_product failed: {e}")
        return {"error": "db_error", "detail": str(e)}


def receive_stock(sku_id: int, qty: float, cost_price_override: float | None = None) -> dict:
    """
    Receive stock for an existing SKU.
    Atomically increments stock_qty using BEGIN IMMEDIATE.
    Optionally updates cost price (e.g. new supplier rate).
    Records an audit entry in stock_movements.
    """
    if qty <= 0:
        return {"error": "invalid_qty", "message": "Quantity must be positive."}

    conn = get_conn()
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT sku_id, name, brand, unit, stock_qty, cost_price FROM products WHERE sku_id = ? AND active = 1",
            (sku_id,),
        ).fetchone()
        if not row:
            conn.rollback()
            return {"error": "product_not_found", "sku_id": sku_id}

        old_qty = row["stock_qty"]
        # If cost_price_override is given, use it; otherwise keep the existing
        # value (which may be NULL for a product added before any stock receipt).
        new_cost = cost_price_override if cost_price_override is not None else row["cost_price"]

        conn.execute(
            """UPDATE products
               SET stock_qty = stock_qty + ?,
                   cost_price = ?,
                   updated_at = datetime('now')
               WHERE sku_id = ?""",
            (qty, new_cost, sku_id),
        )
        conn.execute(
            """INSERT INTO stock_movements (sku_id, delta, reason)
               VALUES (?, ?, 'RECEIVE')""",
            (sku_id, qty),
        )
        conn.commit()
        display = f"{row['brand']} {row['name']}".strip()
        logger.info(f"Received {qty} {row['unit']} of sku_id={sku_id} ('{display}')")
        return {
            "status": "ok",
            "sku_id": sku_id,
            "display_name": display,
            "unit": row["unit"],
            "previous_stock": old_qty,
            "added_qty": qty,
            "new_stock": old_qty + qty,
            "cost_price": new_cost,
            "message": f"Stock updated: '{display}' now has {old_qty + qty} {row['unit']}.",
        }
    except Exception as e:
        conn.rollback()
        logger.error(f"receive_stock failed: {e}")
        return {"error": "db_error", "detail": str(e)}


def get_stock_report(low_stock_only: bool = False) -> dict:
    """
    Returns inventory report.
    If low_stock_only=True, returns only items at or below their reorder level.
    """
    conn = get_conn()
    filter_clause = "AND stock_qty <= reorder_level" if low_stock_only else ""
    rows = conn.execute(
        f"""
        SELECT sku_id, name, brand, unit, mrp, cost_price,
               stock_qty, reorder_level, tax_slab, hsn_code, is_loose
        FROM products
        WHERE active = 1 {filter_clause}
        ORDER BY
            CASE WHEN stock_qty <= reorder_level THEN 0 ELSE 1 END,
            name
        """,
    ).fetchall()

    items = [_product_dict(r) for r in rows]
    low_count = sum(1 for i in items if i["stock_qty"] <= i["reorder_level"])
    return {
        "total_skus": len(items),
        "low_stock_count": low_count,
        "low_stock_only_filter": low_stock_only,
        "items": items,
    }


def update_product_price(
    sku_id: int,
    new_mrp: float | None = None,
    new_cost_price: float | None = None,
) -> dict:
    """
    Update MRP and/or cost price for a product.
    Guardrail: refuses if new_mrp < effective cost_price.
    """
    if new_mrp is None and new_cost_price is None:
        return {"error": "no_update", "message": "Provide at least one of new_mrp or new_cost_price."}

    conn = get_conn()
    row = conn.execute(
        "SELECT sku_id, name, brand, mrp, cost_price FROM products WHERE sku_id = ? AND active = 1",
        (sku_id,),
    ).fetchone()
    if not row:
        return {"error": "product_not_found", "sku_id": sku_id}

    effective_cost = new_cost_price if new_cost_price is not None else row["cost_price"]
    effective_mrp  = new_mrp       if new_mrp       is not None else row["mrp"]

    # Only enforce the MRP >= cost guardrail when both values are known.
    # effective_cost is None when a product was catalogued before its first
    # stock receipt (cost_price is NULL in the DB) and no new_cost_price
    # has been provided in this call.
    if effective_cost is not None and effective_mrp < effective_cost:
        return {
            "error": "mrp_below_cost",
            "message": (
                f"Cannot set MRP ₹{effective_mrp} below cost price ₹{effective_cost}. "
                "Fix the cost price or raise the MRP."
            ),
        }

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """UPDATE products
               SET mrp = ?, cost_price = ?, updated_at = datetime('now')
               WHERE sku_id = ?""",
            (effective_mrp, effective_cost, sku_id),
        )
        conn.commit()
        display = f"{row['brand']} {row['name']}".strip()
        return {
            "status": "ok",
            "sku_id": sku_id,
            "display_name": display,
            "old_mrp": row["mrp"],
            "new_mrp": effective_mrp,
            "old_cost_price": row["cost_price"],
            "new_cost_price": effective_cost,
        }
    except Exception as e:
        conn.rollback()
        return {"error": "db_error", "detail": str(e)}


# ─── Tool schemas ─────────────────────────────────────────────────────────────

INVENTORY_TOOLS = [
    {
        "name": "search_products",
        "description": (
            "Search the product catalogue by name or brand (fuzzy match). "
            "Always call this first when the owner mentions a product by name. "
            "Returns a list of matching SKUs with prices, tax slab, and stock. "
            "If multiple matches are returned (ambiguous=true), present the options "
            "to the owner and ask them to choose before proceeding."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Product name, brand, or keyword to search for.",
                },
                "include_inactive": {
                    "type": "boolean",
                    "description": "If true, include deactivated products in results.",
                    "default": False,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_product",
        "description": "Fetch full details of a single product by its SKU ID.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sku_id": {"type": "integer", "description": "The product's SKU ID."},
            },
            "required": ["sku_id"],
        },
    },
    {
        "name": "add_product",
        "description": (
            "Add a new product (SKU) to the catalogue. "
            "Only call this when the owner explicitly asks to add a new product. "
            "cost_price and initial_qty are optional — omit them when the owner "
            "has not stated a purchase price or opening stock; NEVER invent these values. "
            "Stock and cost price are set later via receive_stock when the first "
            "delivery arrives. Refuses if MRP < cost_price when cost_price is given."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name":          {"type": "string",  "description": "Product name, e.g. 'Atta 5kg'."},
                "brand":         {"type": "string",  "description": "Brand name, e.g. 'Aashirvaad'. Use empty string for unbranded/loose items."},
                "unit":          {"type": "string",  "enum": ["kg","g","litre","ml","packet","dozen","piece"]},
                "mrp":           {"type": "number",  "description": "Selling price per unit in ₹. Required."},
                "tax_slab":      {"type": "integer", "enum": [0,5,12,18], "description": "GST rate in percent."},
                "hsn_code":      {"type": "string",  "description": "HSN code for this product."},
                "cost_price":    {
                    "type": "number",
                    "description": (
                        "Purchase/cost price per unit in ₹. "
                        "ONLY provide this when the owner explicitly states the cost price. "
                        "Omit (do not send this field) when the owner has not mentioned it — "
                        "do NOT guess or invent a value."
                    ),
                },
                "reorder_level": {"type": "number",  "description": "Minimum stock before a reorder alert is raised.", "default": 0},
                "initial_qty":   {
                    "type": "number",
                    "description": (
                        "Opening stock quantity. "
                        "ONLY provide this when the owner explicitly states a starting quantity. "
                        "Omit (do not send this field) when not mentioned — "
                        "do NOT invent a quantity."
                    ),
                    "default": 0,
                },
                "is_loose":      {"type": "boolean", "description": "True for loose/weighed items (sugar, rice by kg).", "default": False},
            },
            "required": ["name", "brand", "unit", "mrp", "tax_slab", "hsn_code"],
        },
    },
    {
        "name": "receive_stock",
        "description": (
            "Record incoming stock for an existing product. "
            "Increments stock quantity atomically. "
            "Optionally update the cost price if the supplier rate has changed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sku_id":               {"type": "integer", "description": "SKU ID of the product."},
                "qty":                  {"type": "number",  "description": "Quantity received (in the product's unit)."},
                "cost_price_override":  {"type": "number",  "description": "New cost price per unit if supplier rate changed. Omit to keep existing."},
            },
            "required": ["sku_id", "qty"],
        },
    },
    {
        "name": "get_stock_report",
        "description": (
            "Return the full inventory or only items that are at/below their reorder level. "
            "Use low_stock_only=true for a quick reorder list."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "low_stock_only": {
                    "type": "boolean",
                    "description": "If true, show only items needing reorder.",
                    "default": False,
                },
            },
            "required": [],
        },
    },
    {
        "name": "update_product_price",
        "description": (
            "Update the MRP and/or cost price of an existing product. "
            "Refuses if the resulting MRP would be below cost price."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sku_id":         {"type": "integer", "description": "SKU ID of the product to update."},
                "new_mrp":        {"type": "number",  "description": "New selling price per unit in ₹. Omit to leave unchanged."},
                "new_cost_price": {"type": "number",  "description": "New cost price per unit in ₹. Omit to leave unchanged."},
            },
            "required": ["sku_id"],
        },
    },
]

INVENTORY_HANDLERS: dict[str, callable] = {
    "search_products":    lambda args: search_products(**args),
    "get_product":        lambda args: get_product(**args),
    "add_product":        lambda args: add_product(**args),
    "receive_stock":      lambda args: receive_stock(**args),
    "get_stock_report":   lambda args: get_stock_report(**args),
    "update_product_price": lambda args: update_product_price(**args),
}
