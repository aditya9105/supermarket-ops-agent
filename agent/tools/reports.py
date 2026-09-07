"""
Reports tools — daily close and sales analysis.
"""
import logging
from datetime import date, timedelta
from db.connection import get_conn

logger = logging.getLogger(__name__)


def daily_close(close_date: str | None = None) -> dict:
    """
    Summarise all PAID bills for the given date (defaults to today).
    Stamps close_date on those bills.
    Returns: total sales, per-payment-mode breakdown, top items, GST collected.
    """
    conn = get_conn()
    target_date = close_date or date.today().isoformat()

    bills = conn.execute(
        """SELECT bill_id, payment_mode, grand_total, cgst_total, sgst_total
           FROM bills
           WHERE status = 'PAID' AND bill_date = ? AND (close_date IS NULL OR close_date = ?)""",
        (target_date, target_date),
    ).fetchall()

    if not bills:
        return {
            "date": target_date,
            "message": f"No paid bills found for {target_date}.",
            "total_sales": 0,
            "bill_count": 0,
        }

    bill_ids = [b["bill_id"] for b in bills]
    placeholders = ",".join("?" * len(bill_ids))

    total_sales   = sum(b["grand_total"] or 0 for b in bills)
    total_cgst    = sum(b["cgst_total"]  or 0 for b in bills)
    total_sgst    = sum(b["sgst_total"]  or 0 for b in bills)
    bill_count    = len(bills)

    # Payment mode breakdown
    mode_breakdown: dict[str, float] = {}
    for b in bills:
        mode = b["payment_mode"] or "UNKNOWN"
        mode_breakdown[mode] = mode_breakdown.get(mode, 0) + (b["grand_total"] or 0)

    # Top 10 items by revenue
    top_items = conn.execute(
        f"""SELECT p.name, p.brand,
                   SUM(bi.qty) as total_qty, p.unit,
                   SUM(bi.line_total) as total_revenue
            FROM bill_items bi
            JOIN products p ON p.sku_id = bi.sku_id
            WHERE bi.bill_id IN ({placeholders})
            GROUP BY bi.sku_id
            ORDER BY total_revenue DESC
            LIMIT 10""",
        bill_ids,
    ).fetchall()

    # Stamp close_date on these bills
    conn.execute(
        f"UPDATE bills SET close_date = ? WHERE bill_id IN ({placeholders})",
        [target_date] + bill_ids,
    )
    conn.commit()

    return {
        "date": target_date,
        "bill_count": bill_count,
        "total_sales": round(total_sales, 2),
        "total_cgst": round(total_cgst, 2),
        "total_sgst": round(total_sgst, 2),
        "total_gst": round(total_cgst + total_sgst, 2),
        "payment_mode_breakdown": {k: round(v, 2) for k, v in mode_breakdown.items()},
        "top_items": [
            {
                "name": f"{r['brand']} {r['name']}".strip(),
                "unit": r["unit"],
                "total_qty": round(r["total_qty"], 3),
                "total_revenue": round(r["total_revenue"], 2),
            }
            for r in top_items
        ],
        "message": (
            f"Day close for {target_date}: "
            f"{bill_count} bills, ₹{round(total_sales, 2)} total sales, "
            f"₹{round(total_cgst + total_sgst, 2)} GST collected."
        ),
    }


def get_sales_report(
    from_date: str,
    to_date: str,
    group_by: str = "day",
) -> dict:
    """
    Aggregated sales data for a date range.
    group_by: 'day' | 'product' | 'payment_mode'
    Used internally by generate_analysis_deck and available directly to the agent.
    """
    conn = get_conn()
    valid_groups = {"day", "product", "payment_mode"}
    if group_by not in valid_groups:
        return {"error": "invalid_group_by", "allowed": list(valid_groups)}

    if group_by == "day":
        rows = conn.execute(
            """SELECT bill_date as label,
                      COUNT(*) as bill_count,
                      SUM(grand_total) as total_sales,
                      SUM(cgst_total + sgst_total) as total_gst
               FROM bills
               WHERE status = 'PAID' AND bill_date BETWEEN ? AND ?
               GROUP BY bill_date
               ORDER BY bill_date""",
            (from_date, to_date),
        ).fetchall()
        data = [dict(r) for r in rows]

    elif group_by == "product":
        rows = conn.execute(
            """SELECT (p.brand || ' ' || p.name) as label, p.unit,
                      SUM(bi.qty) as total_qty,
                      SUM(bi.line_total) as total_revenue
               FROM bill_items bi
               JOIN products p ON p.sku_id = bi.sku_id
               JOIN bills b ON b.bill_id = bi.bill_id
               WHERE b.status = 'PAID' AND b.bill_date BETWEEN ? AND ?
               GROUP BY bi.sku_id
               ORDER BY total_revenue DESC
               LIMIT 20""",
            (from_date, to_date),
        ).fetchall()
        data = [dict(r) for r in rows]

    elif group_by == "payment_mode":
        rows = conn.execute(
            """SELECT payment_mode as label,
                      COUNT(*) as bill_count,
                      SUM(grand_total) as total_sales
               FROM bills
               WHERE status = 'PAID' AND bill_date BETWEEN ? AND ?
               GROUP BY payment_mode""",
            (from_date, to_date),
        ).fetchall()
        data = [dict(r) for r in rows]

    total = conn.execute(
        """SELECT COUNT(*) as bills, COALESCE(SUM(grand_total),0) as revenue
           FROM bills WHERE status='PAID' AND bill_date BETWEEN ? AND ?""",
        (from_date, to_date),
    ).fetchone()

    return {
        "from_date": from_date,
        "to_date": to_date,
        "group_by": group_by,
        "total_bills": total["bills"],
        "total_revenue": round(total["revenue"], 2),
        "data": data,
    }


REPORT_TOOLS = [
    {
        "name": "daily_close",
        "description": (
            "Close the day: summarise all paid bills for the given date, "
            "showing total sales, GST collected, payment mode breakdown, and top-selling items. "
            "Stamps all included bills with the close date. Defaults to today."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "close_date": {
                    "type": "string",
                    "description": "ISO date (YYYY-MM-DD). Defaults to today if omitted.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_sales_report",
        "description": (
            "Aggregated sales data for a date range. "
            "group_by: 'day' for daily trend, 'product' for top-item revenue, "
            "'payment_mode' for cash/UPI/card split."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "from_date": {"type": "string", "description": "Start date YYYY-MM-DD."},
                "to_date":   {"type": "string", "description": "End date YYYY-MM-DD."},
                "group_by":  {"type": "string", "enum": ["day","product","payment_mode"], "default": "day"},
            },
            "required": ["from_date", "to_date"],
        },
    },
]

REPORT_HANDLERS: dict[str, callable] = {
    "daily_close":     lambda args: daily_close(**args),
    "get_sales_report": lambda args: get_sales_report(**args),
}
