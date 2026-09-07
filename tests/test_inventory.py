"""
Tests for inventory tools — Phase (a) verification.
"""
import pytest
from agent.tools.inventory import (
    search_products, get_product, add_product,
    receive_stock, get_stock_report, update_product_price,
)


class TestSearchProducts:
    def test_search_by_name(self):
        r = search_products("maggi")
        assert r["count"] >= 1
        assert any("Maggi" in p["brand"] for p in r["products"])

    def test_search_by_brand(self):
        r = search_products("Amul")
        assert r["count"] >= 1
        assert all("Amul" in p["brand"] for p in r["products"])

    def test_search_returns_multiple_for_atta(self):
        """'atta' should return multiple SKUs — testing disambiguation."""
        r = search_products("atta")
        assert r["count"] > 1
        assert r["ambiguous"] is True

    def test_search_no_results(self):
        r = search_products("xyznonexistent")
        assert r["count"] == 0
        assert r["ambiguous"] is False

    def test_search_result_has_required_fields(self):
        r = search_products("tata")
        p = r["products"][0]
        for field in ["sku_id", "name", "brand", "unit", "mrp", "cost_price",
                      "stock_qty", "tax_slab", "hsn_code"]:
            assert field in p, f"Missing field: {field}"


class TestGetProduct:
    def test_get_existing(self):
        r = search_products("Parle-G")
        sku_id = r["products"][0]["sku_id"]
        p = get_product(sku_id)
        assert p["sku_id"] == sku_id
        assert "Parle" in p["brand"]

    def test_get_nonexistent(self):
        r = get_product(99999)
        assert r.get("error") == "product_not_found"


class TestAddProduct:
    def test_add_valid_product(self):
        r = add_product(
            name="Cornflakes 500g", brand="Kellogg's", unit="packet",
            mrp=180.0, cost_price=150.0, tax_slab=12, hsn_code="1904",
            reorder_level=5, initial_qty=20,
        )
        assert r["status"] == "ok"
        assert r["sku_id"] > 0

    def test_add_product_invalid_unit(self):
        r = add_product(
            name="Test", brand="Brand", unit="box",
            mrp=100, cost_price=80, tax_slab=5, hsn_code="1234",
        )
        assert r["error"] == "invalid_unit"

    def test_add_product_invalid_tax_slab(self):
        r = add_product(
            name="Test", brand="Brand", unit="packet",
            mrp=100, cost_price=80, tax_slab=7, hsn_code="1234",
        )
        assert r["error"] == "invalid_tax_slab"

    def test_add_product_mrp_below_cost(self):
        """Guardrail: MRP < cost_price must be rejected."""
        r = add_product(
            name="Test", brand="Brand", unit="packet",
            mrp=50, cost_price=80, tax_slab=5, hsn_code="1234",
        )
        assert r["error"] == "mrp_below_cost"


class TestReceiveStock:
    def test_receive_increments_stock(self):
        sku_id = search_products("Parle-G")["products"][0]["sku_id"]
        before = get_product(sku_id)["stock_qty"]
        r = receive_stock(sku_id=sku_id, qty=50)
        assert r["status"] == "ok"
        assert r["new_stock"] == before + 50

    def test_receive_invalid_qty(self):
        sku_id = search_products("Iodised Salt")["products"][0]["sku_id"]
        r = receive_stock(sku_id=sku_id, qty=-5)
        assert r["error"] == "invalid_qty"

    def test_receive_nonexistent_sku(self):
        r = receive_stock(sku_id=99999, qty=10)
        assert r["error"] == "product_not_found"

    def test_receive_updates_cost_price(self):
        sku_id = search_products("Amul Butter 100g")["products"][0]["sku_id"]
        r = receive_stock(sku_id=sku_id, qty=10, cost_price_override=55.0)
        assert r["status"] == "ok"
        assert r["cost_price"] == 55.0


class TestStockReport:
    def test_full_report(self):
        r = get_stock_report()
        assert r["total_skus"] > 0
        assert "items" in r
        assert all("sku_id" in i for i in r["items"])

    def test_low_stock_only(self):
        """
        Seed a product with stock=0, reorder_level=5 → should appear in low-stock report.
        """
        add_product(
            name="LowStock Item", brand="TestBrand", unit="packet",
            mrp=100, cost_price=80, tax_slab=5, hsn_code="9999",
            reorder_level=5, initial_qty=0,
        )
        r = get_stock_report(low_stock_only=True)
        assert r["low_stock_count"] >= 1
        names = [i["name"] for i in r["items"]]
        assert "LowStock Item" in names


class TestUpdateProductPrice:
    def test_update_mrp(self):
        sku_id = search_products("fortune sunflower")["products"][0]["sku_id"]
        r = update_product_price(sku_id=sku_id, new_mrp=175.0)
        assert r["status"] == "ok"
        assert r["new_mrp"] == 175.0

    def test_update_below_cost_rejected(self):
        """Guardrail: new MRP < cost_price must be refused."""
        sku_id = search_products("fortune sunflower")["products"][0]["sku_id"]
        old = get_product(sku_id)
        # Try to set MRP lower than cost
        r = update_product_price(sku_id=sku_id, new_mrp=old["cost_price"] - 10)
        assert r["error"] == "mrp_below_cost"

    def test_no_fields_provided(self):
        sku_id = search_products("Iodised Salt")["products"][0]["sku_id"]
        r = update_product_price(sku_id=sku_id)
        assert r["error"] == "no_update"
