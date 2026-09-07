"""
Tests for billing engine — Phase (b) verification.

Covers: GST correctness, multi-turn draft lifecycle, oversell guard,
        idempotency, below-cost guardrail, concurrent finalize.
"""
import json
import threading
import pytest
from agent.tools.inventory import search_products, receive_stock, get_product
from agent.tools.billing import (
    start_bill, add_bill_item, edit_bill_item, remove_bill_item,
    get_bill_preview, finalize_bill, cancel_bill,
)


def _get_sku(query: str) -> dict:
    """Helper: get first matching product."""
    r = search_products(query)
    assert r["count"] > 0, f"No product found for '{query}'"
    return r["products"][0]


class TestGSTCalculation:
    """Verify GST figures are computed correctly per Indian intra-state rules."""

    def test_amul_butter_gst(self):
        """
        Amul Butter 100g — 12% GST, selling at ₹60.
        2 units → taxable=₹120, CGST=₹7.20, SGST=₹7.20, total=₹134.40
        """
        sku = _get_sku("Amul Butter 100g")
        bill = start_bill(idempotency_key="test-gst-1")
        assert bill["status"] == "ok"
        bill_id = bill["bill_id"]

        add_bill_item(bill_id=bill_id, sku_id=sku["sku_id"], qty=2)
        preview = get_bill_preview(bill_id=bill_id)

        line = preview["lines"][0]
        assert line["tax_slab"] == 12
        # CGST = SGST = 6%
        assert abs(line["cgst_amt"] - line["sgst_amt"]) < 0.01
        assert abs(line["cgst_amt"] - (line["taxable_amt"] * 0.06)) < 0.01

    def test_zero_gst_loose_atta(self):
        """Loose atta is 0% GST — no tax should be charged."""
        sku = _get_sku("Atta (loose)")
        bill = start_bill(idempotency_key="test-gst-2")
        bill_id = bill["bill_id"]

        add_bill_item(bill_id=bill_id, sku_id=sku["sku_id"], qty=2)
        preview = get_bill_preview(bill_id=bill_id)

        line = preview["lines"][0]
        assert line["tax_slab"] == 0
        assert line["cgst_amt"] == 0.0
        assert line["sgst_amt"] == 0.0
        assert line["line_total"] == line["taxable_amt"]

    def test_grand_total_rounded_to_rupee(self):
        """Grand total should be rounded to nearest rupee."""
        sku = _get_sku("Amul Butter 100g")  # 12% GST
        bill = start_bill(idempotency_key="test-gst-3")
        bill_id = bill["bill_id"]
        add_bill_item(bill_id=bill_id, sku_id=sku["sku_id"], qty=3)
        preview = get_bill_preview(bill_id=bill_id)
        gt = preview["grand_total"]
        assert gt == round(gt)  # must be a whole rupee

    def test_cgst_equals_sgst(self):
        """Intra-state: CGST must always equal SGST for the same line."""
        sku = _get_sku("Surf Excel 500g")  # 18% GST
        bill = start_bill(idempotency_key="test-gst-4")
        bill_id = bill["bill_id"]
        add_bill_item(bill_id=bill_id, sku_id=sku["sku_id"], qty=1)
        preview = get_bill_preview(bill_id=bill_id)
        line = preview["lines"][0]
        assert line["cgst_amt"] == line["sgst_amt"]


class TestMultiTurnBill:
    """Verify bill builds correctly across multiple add/edit/remove calls."""

    def test_add_edit_remove_lifecycle(self):
        sku_butter = _get_sku("Amul Butter 100g")
        sku_salt   = _get_sku("Iodised Salt 1kg")
        bill = start_bill(idempotency_key="test-multiturn-1")
        bill_id = bill["bill_id"]

        # Add two items
        r1 = add_bill_item(bill_id=bill_id, sku_id=sku_butter["sku_id"], qty=2)
        r2 = add_bill_item(bill_id=bill_id, sku_id=sku_salt["sku_id"],   qty=3)
        assert r1["status"] == "ok"
        assert r2["status"] == "ok"

        # Verify preview has 2 lines
        preview = get_bill_preview(bill_id=bill_id)
        assert preview["item_count"] == 2

        # Edit first item qty
        edit_bill_item(bill_id=bill_id, line_id=r1["line_id"], new_qty=5)
        preview = get_bill_preview(bill_id=bill_id)
        lines = {l["line_id"]: l for l in preview["lines"]}
        assert lines[r1["line_id"]]["qty"] == 5

        # Remove second item
        remove_bill_item(bill_id=bill_id, line_id=r2["line_id"])
        preview = get_bill_preview(bill_id=bill_id)
        assert preview["item_count"] == 1

    def test_stock_not_decremented_until_finalize(self):
        """Adding to bill must NOT change stock — only finalize should."""
        sku = _get_sku("Parle-G 100g")
        stock_before = get_product(sku["sku_id"])["stock_qty"]

        bill = start_bill(idempotency_key="test-stockdecr-1")
        bill_id = bill["bill_id"]
        add_bill_item(bill_id=bill_id, sku_id=sku["sku_id"], qty=5)

        # Stock must be unchanged
        stock_after_add = get_product(sku["sku_id"])["stock_qty"]
        assert stock_after_add == stock_before

        # Finalize → stock should drop
        finalize_bill(
            bill_id=bill_id, payment_mode="CASH",
            idempotency_key=f"fin-test-stockdecr-1"
        )
        stock_after_fin = get_product(sku["sku_id"])["stock_qty"]
        assert stock_after_fin == stock_before - 5


class TestOversellGuard:
    """Verify that selling beyond stock is refused mechanically at finalize time."""

    def test_finalize_refuses_oversell(self):
        """
        SCENARIO: finalize bill for 10 units when stock=6.
        EXPECT:   error returned, stock unchanged at 6.
        """
        sku = _get_sku("Maggi Noodles")
        sku_id = sku["sku_id"]

        # Set stock to exactly 6
        from db.connection import get_conn
        conn = get_conn()
        conn.execute("UPDATE products SET stock_qty = 6 WHERE sku_id = ?", (sku_id,))
        conn.commit()

        bill = start_bill(idempotency_key="test-oversell-1")
        bill_id = bill["bill_id"]
        add_bill_item(bill_id=bill_id, sku_id=sku_id, qty=6)  # Allowed: matches stock

        # Manually bump qty in DB to simulate race condition / bad edit
        conn.execute("UPDATE bill_items SET qty = 10 WHERE bill_id = ?", (bill_id,))
        conn.commit()

        r = finalize_bill(
            bill_id=bill_id, payment_mode="CASH",
            idempotency_key="fin-test-oversell-1"
        )
        assert r["error"] == "insufficient_stock"

        # Stock must remain at 6
        stock_now = get_product(sku_id)["stock_qty"]
        assert stock_now == 6

    def test_add_item_soft_check_prevents_oversell(self):
        """add_bill_item has a soft stock check to catch obvious errors early."""
        sku = _get_sku("Amul Butter 500g")
        # Set stock to 2
        from db.connection import get_conn
        conn = get_conn()
        conn.execute("UPDATE products SET stock_qty = 2 WHERE sku_id = ?", (sku["sku_id"],))
        conn.commit()

        bill = start_bill(idempotency_key="test-soft-1")
        r = add_bill_item(bill_id=bill["bill_id"], sku_id=sku["sku_id"], qty=10)
        assert r["error"] == "insufficient_stock"


class TestIdempotency:
    """
    SCENARIO: send the same finalize call twice.
    EXPECT:   second call returns the same receipt; stock decremented only once.
    """

    def test_duplicate_finalize_no_double_decrement(self):
        sku = _get_sku("Tata Iodised Salt")
        sku_id = sku["sku_id"]
        stock_before = get_product(sku_id)["stock_qty"]

        bill = start_bill(idempotency_key="test-idem-start-1")
        bill_id = bill["bill_id"]
        add_bill_item(bill_id=bill_id, sku_id=sku_id, qty=3)

        key = "fin-idem-test-1"
        r1 = finalize_bill(bill_id=bill_id, payment_mode="UPI", idempotency_key=key)
        r2 = finalize_bill(bill_id=bill_id, payment_mode="UPI", idempotency_key=key)

        assert r1["status"] == "ok"
        assert r2["status"] == "ok"
        assert r1["grand_total"] == r2["grand_total"]

        # Stock should only be decremented once
        stock_after = get_product(sku_id)["stock_qty"]
        assert stock_after == stock_before - 3

    def test_start_bill_idempotency(self):
        """Same start_bill key always returns same bill_id."""
        r1 = start_bill(idempotency_key="sb-idem-1")
        r2 = start_bill(idempotency_key="sb-idem-1")
        assert r1["bill_id"] == r2["bill_id"]


class TestBelowCostGuardrail:
    """Verify below-cost guardrail is enforced at tool layer."""

    def test_below_cost_rejected_by_default(self):
        sku = _get_sku("Parle-G 100g")
        bill = start_bill(idempotency_key="test-belowcost-1")
        cost = sku["cost_price"]
        r = add_bill_item(
            bill_id=bill["bill_id"], sku_id=sku["sku_id"],
            qty=1, unit_price=cost - 2.0  # below cost
        )
        assert r["error"] == "below_cost"

    def test_below_cost_allowed_with_confirmation(self):
        sku = _get_sku("Parle-G 100g")
        bill = start_bill(idempotency_key="test-belowcost-2")
        cost = sku["cost_price"]
        r = add_bill_item(
            bill_id=bill["bill_id"], sku_id=sku["sku_id"],
            qty=1, unit_price=cost - 2.0,
            confirm_below_cost=True
        )
        assert r["status"] == "ok"


class TestConcurrentFinalize:
    """
    SCENARIO: two threads try to finalize two separate bills that both need
              the same product when only enough stock exists for one.
    EXPECT:   exactly one succeeds; the other gets insufficient_stock.
    """

    def test_concurrent_stock_contention(self, tmp_path):
        """
        Uses a temp-file DB (not :memory:) so all threads share the same DB.
        The conftest autouse fixture uses :memory: which is per-thread in SQLite;
        for this concurrency test we spin up a separate file-backed DB.
        """
        import os
        import threading
        from db.connection import close_conn

        # Switch to a temp file DB for this test
        db_file = str(tmp_path / "conc_test.db")
        original_db = os.environ.get("DB_PATH", ":memory:")
        os.environ["DB_PATH"] = db_file

        try:
            close_conn()  # close any existing :memory: connection
            from db.migrations import run_migrations, seed_products
            run_migrations()
            seed_products()

            sku = _get_sku("Maggi Noodles")
            sku_id = sku["sku_id"]

            # Set stock to exactly 5
            from db.connection import get_conn
            conn = get_conn()
            conn.execute("UPDATE products SET stock_qty = 5 WHERE sku_id = ?", (sku_id,))
            conn.commit()
            close_conn()  # release so threads can open fresh connections

            # Bill A: wants 4 units
            bill_a = start_bill(idempotency_key="conc-start-a")
            add_bill_item(bill_id=bill_a["bill_id"], sku_id=sku_id, qty=4)
            close_conn()

            # Bill B: wants 4 units (total 8, only 5 available)
            bill_b = start_bill(idempotency_key="conc-start-b")
            add_bill_item(bill_id=bill_b["bill_id"], sku_id=sku_id, qty=4)
            close_conn()

            results = []

            def do_finalize(bid, key):
                # Each thread gets its own file-backed connection
                close_conn()
                r = finalize_bill(bill_id=bid, payment_mode="CASH", idempotency_key=key)
                results.append(r)
                close_conn()

            t1 = threading.Thread(target=do_finalize, args=(bill_a["bill_id"], "fin-conc-a"))
            t2 = threading.Thread(target=do_finalize, args=(bill_b["bill_id"], "fin-conc-b"))
            t1.start(); t2.start()
            t1.join();  t2.join()

            successes = [r for r in results if r.get("status") == "ok"]
            failures  = [r for r in results if r.get("error") == "insufficient_stock"]

            assert len(successes) == 1, f"Expected 1 success, got {results}"
            assert len(failures)  == 1

            # Total stock should be 5 - 4 = 1
            close_conn()
            conn = get_conn()
            stock_now = conn.execute(
                "SELECT stock_qty FROM products WHERE sku_id = ?", (sku_id,)
            ).fetchone()["stock_qty"]
            assert stock_now == 1

        finally:
            close_conn()
            os.environ["DB_PATH"] = original_db
            close_conn()


class TestCancelBill:
    def test_cancel_draft(self):
        bill = start_bill(idempotency_key="test-cancel-1")
        r = cancel_bill(bill_id=bill["bill_id"])
        assert r["status"] == "ok"

    def test_cannot_cancel_paid_bill(self):
        sku = _get_sku("Tata Iodised Salt")
        bill = start_bill(idempotency_key="test-cancel-paid-1")
        add_bill_item(bill_id=bill["bill_id"], sku_id=sku["sku_id"], qty=1)
        finalize_bill(
            bill_id=bill["bill_id"], payment_mode="CASH",
            idempotency_key="fin-cancel-paid-1"
        )
        r = cancel_bill(bill_id=bill["bill_id"])
        assert r["error"] == "cannot_cancel_paid"
