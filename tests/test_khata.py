"""
Tests for khata (credit ledger) tools — Phase (c) verification.
"""
import pytest
from agent.tools.khata import (
    add_customer, search_customer, get_khata_balance,
    add_khata_entry, settle_khata,
)


class TestCustomerSearch:
    def test_search_existing(self):
        add_customer("Ramesh Kumar", phone="9876543210")
        r = search_customer("Ramesh")
        assert r["count"] >= 1
        assert any("Ramesh" in c["name"] for c in r["customers"])

    def test_search_no_results(self):
        r = search_customer("NonExistentXYZ")
        assert r["count"] == 0


class TestAddCustomer:
    def test_add_customer(self):
        r = add_customer("Sunita Devi")
        assert r["status"] == "ok"
        assert r["customer_id"] > 0


class TestKhataBalance:
    def test_balance_zero_for_new_customer(self):
        cust = add_customer("Priya Singh")
        r = get_khata_balance(cust["customer_id"])
        assert r["balance"] == 0.0

    def test_balance_reflects_credit(self):
        cust = add_customer("Mahesh Patel")
        cid = cust["customer_id"]
        add_khata_entry(customer_id=cid, amount=500.0, note="sold goods")
        r = get_khata_balance(cid)
        assert r["balance"] == 500.0

    def test_balance_after_settlement(self):
        cust = add_customer("Geeta Sharma")
        cid = cust["customer_id"]
        add_khata_entry(customer_id=cid, amount=1000.0, note="goods on credit")
        settle_khata(customer_id=cid, amount=400.0, payment_mode="CASH")
        r = get_khata_balance(cid)
        assert r["balance"] == 600.0

    def test_nonexistent_customer(self):
        r = get_khata_balance(99999)
        assert r["error"] == "customer_not_found"


class TestSettleKhata:
    def test_settle_nonexistent_customer_refused(self):
        """
        HARD REQUIREMENT: settle_khata must refuse for a nonexistent customer.
        """
        r = settle_khata(customer_id=99999, amount=500, payment_mode="CASH")
        assert r["error"] == "customer_not_found"
        assert "search" in r["message"].lower() or "found" in r["message"].lower()

    def test_settle_valid_customer(self):
        cust = add_customer("Vikram Joshi")
        cid = cust["customer_id"]
        add_khata_entry(customer_id=cid, amount=800.0)
        r = settle_khata(customer_id=cid, amount=300.0, payment_mode="UPI", payment_ref="UPI123")
        assert r["status"] == "ok"
        assert r["new_balance"] == 500.0

    def test_settle_negative_amount_rejected(self):
        cust = add_customer("Anita Gupta")
        r = settle_khata(customer_id=cust["customer_id"], amount=-100, payment_mode="CASH")
        assert r["error"] == "invalid_amount"

    def test_settle_invalid_payment_mode(self):
        cust = add_customer("Suresh Rao")
        r = settle_khata(customer_id=cust["customer_id"], amount=100, payment_mode="BARTER")
        assert r["error"] == "invalid_payment_mode"


class TestLedgerHistory:
    def test_running_balance_in_history(self):
        cust = add_customer("Raju Bhaiya")
        cid = cust["customer_id"]
        add_khata_entry(customer_id=cid, amount=200.0, note="first credit")
        add_khata_entry(customer_id=cid, amount=300.0, note="second credit")
        add_khata_entry(customer_id=cid, amount=-100.0, note="partial payment")

        r = get_khata_balance(cid)
        assert r["balance"] == 400.0
        # Verify running balance in ledger
        balances = [e["running_balance"] for e in r["entries"]]
        assert balances == [200.0, 500.0, 400.0]
