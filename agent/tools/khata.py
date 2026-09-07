"""
Khata (credit ledger) tools.

Rules enforced at tool layer:
- settle_khata: customer MUST exist — refuses with clear error if not found.
- add_khata_entry: can link to a bill_id for traceability.
- Running balance = SUM(amount) where positive = they owe, negative = payment received.
"""
import logging
from db.connection import get_conn

logger = logging.getLogger(__name__)


def search_customer(name: str) -> dict:
    """Fuzzy search customers by name. Returns matches for disambiguation."""
    conn = get_conn()
    q = f"%{name.strip()}%"
    rows = conn.execute(
        """SELECT customer_id, name, phone,
                  COALESCE((SELECT SUM(amount) FROM khata_entries WHERE customer_id = c.customer_id), 0) AS balance
           FROM customers c
           WHERE name LIKE ?
           ORDER BY name
           LIMIT 10""",
        (q,),
    ).fetchall()
    customers = [dict(r) for r in rows]
    return {
        "count": len(customers),
        "customers": customers,
        "ambiguous": len(customers) > 1,
    }


def add_customer(name: str, phone: str | None = None) -> dict:
    """Register a new customer in the khata system."""
    name = name.strip()
    if not name:
        return {"error": "empty_name"}
    conn = get_conn()
    cursor = conn.execute(
        "INSERT INTO customers (name, phone) VALUES (?, ?)", (name, phone)
    )
    conn.commit()
    return {
        "status": "ok",
        "customer_id": cursor.lastrowid,
        "name": name,
        "message": f"Customer '{name}' added.",
    }


def get_khata_balance(customer_id: int) -> dict:
    """Return full ledger history and running balance for a customer."""
    conn = get_conn()
    customer = conn.execute(
        "SELECT customer_id, name, phone FROM customers WHERE customer_id = ?",
        (customer_id,),
    ).fetchone()
    if not customer:
        return {"error": "customer_not_found", "customer_id": customer_id}

    entries = conn.execute(
        """SELECT entry_id, amount, note, bill_id, created_at
           FROM khata_entries WHERE customer_id = ?
           ORDER BY created_at""",
        (customer_id,),
    ).fetchall()

    running = 0.0
    ledger = []
    for e in entries:
        running += e["amount"]
        ledger.append({
            "entry_id":  e["entry_id"],
            "amount":    e["amount"],
            "running_balance": round(running, 2),
            "note":      e["note"],
            "bill_id":   e["bill_id"],
            "created_at": e["created_at"],
        })

    balance = round(running, 2)
    return {
        "customer_id":   customer["customer_id"],
        "name":          customer["name"],
        "phone":         customer["phone"],
        "balance":       balance,
        "balance_label": (
            f"₹{abs(balance)} credit (they owe you)" if balance > 0
            else f"₹{abs(balance)} advance (you owe them)" if balance < 0
            else "Settled — balance is zero"
        ),
        "entries": ledger,
    }


def add_khata_entry(
    customer_id: int,
    amount: float,
    note: str | None = None,
    bill_id: int | None = None,
) -> dict:
    """
    Add a khata entry.
    amount > 0 : credit extended (they owe more)
    amount < 0 : payment received (balance decreases)
    """
    conn = get_conn()
    customer = conn.execute(
        "SELECT customer_id, name FROM customers WHERE customer_id = ?",
        (customer_id,),
    ).fetchone()
    if not customer:
        return {"error": "customer_not_found", "customer_id": customer_id}

    cursor = conn.execute(
        """INSERT INTO khata_entries (customer_id, amount, note, bill_id)
           VALUES (?, ?, ?, ?)""",
        (customer_id, amount, note, bill_id),
    )
    conn.commit()

    # Updated balance
    balance = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) as bal FROM khata_entries WHERE customer_id = ?",
        (customer_id,),
    ).fetchone()["bal"]

    return {
        "status": "ok",
        "entry_id": cursor.lastrowid,
        "customer_id": customer_id,
        "name": customer["name"],
        "entry_amount": amount,
        "new_balance": round(balance, 2),
        "message": (
            f"{'Credit of' if amount > 0 else 'Payment of'} ₹{abs(amount)} "
            f"recorded for {customer['name']}. New balance: ₹{round(balance, 2)}."
        ),
    }


def settle_khata(
    customer_id: int,
    amount: float,
    payment_mode: str,
    payment_ref: str | None = None,
) -> dict:
    """
    Record a khata settlement (payment received from customer).
    GUARDRAIL: customer MUST exist — refuses if customer_id not found.
    amount should be positive (will be stored as negative = payment received).
    """
    if amount <= 0:
        return {"error": "invalid_amount", "message": "Amount must be positive."}

    payment_mode = payment_mode.upper()
    if payment_mode not in ("CASH", "UPI", "CARD"):
        return {"error": "invalid_payment_mode", "allowed": ["CASH", "UPI", "CARD"]}

    conn = get_conn()
    # ── Guardrail: customer must exist ────────────────────────────────────────
    customer = conn.execute(
        "SELECT customer_id, name FROM customers WHERE customer_id = ?",
        (customer_id,),
    ).fetchone()
    if not customer:
        return {
            "error": "customer_not_found",
            "customer_id": customer_id,
            "message": (
                f"No customer with ID {customer_id} found. "
                "Use search_customer to find the correct customer first."
            ),
        }

    note = f"Settlement via {payment_mode}" + (f" ref:{payment_ref}" if payment_ref else "")
    cursor = conn.execute(
        """INSERT INTO khata_entries (customer_id, amount, note)
           VALUES (?, ?, ?)""",
        (customer_id, -amount, note),
    )
    conn.commit()

    balance = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) as bal FROM khata_entries WHERE customer_id = ?",
        (customer_id,),
    ).fetchone()["bal"]

    return {
        "status": "ok",
        "entry_id": cursor.lastrowid,
        "customer_id": customer_id,
        "name": customer["name"],
        "settled_amount": amount,
        "payment_mode": payment_mode,
        "new_balance": round(balance, 2),
        "message": (
            f"Received ₹{amount} from {customer['name']} via {payment_mode}. "
            f"Remaining balance: ₹{round(balance, 2)}."
        ),
    }


# ─── Tool schemas ─────────────────────────────────────────────────────────────

KHATA_TOOLS = [
    {
        "name": "search_customer",
        "description": (
            "Search customers by name. Always call this before settle_khata or add_khata_entry "
            "to get the correct customer_id. Returns balance alongside each match."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Customer name or partial name."},
            },
            "required": ["name"],
        },
    },
    {
        "name": "add_customer",
        "description": "Register a new customer in the khata (credit ledger) system.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name":  {"type": "string"},
                "phone": {"type": "string", "description": "Optional phone number."},
            },
            "required": ["name"],
        },
    },
    {
        "name": "get_khata_balance",
        "description": "Get the full ledger history and current balance for a customer.",
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_id": {"type": "integer"},
            },
            "required": ["customer_id"],
        },
    },
    {
        "name": "add_khata_entry",
        "description": (
            "Add a khata entry manually. "
            "Use positive amount to extend credit (they owe more), "
            "negative to record a payment. "
            "For standard settlements use settle_khata instead."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_id": {"type": "integer"},
                "amount":      {"type": "number", "description": "+ve = credit, -ve = payment."},
                "note":        {"type": "string"},
                "bill_id":     {"type": "integer", "description": "Link to a bill if relevant."},
            },
            "required": ["customer_id", "amount"],
        },
    },
    {
        "name": "settle_khata",
        "description": (
            "Record a payment received from a khata customer. "
            "Will refuse if the customer does not exist — search first. "
            "amount is the positive amount received."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_id":  {"type": "integer"},
                "amount":       {"type": "number", "description": "Amount received (positive)."},
                "payment_mode": {"type": "string", "enum": ["CASH","UPI","CARD"]},
                "payment_ref":  {"type": "string", "description": "UPI ref or card last-4."},
            },
            "required": ["customer_id", "amount", "payment_mode"],
        },
    },
]

KHATA_HANDLERS: dict[str, callable] = {
    "search_customer":  lambda args: search_customer(**args),
    "add_customer":     lambda args: add_customer(**args),
    "get_khata_balance": lambda args: get_khata_balance(**args),
    "add_khata_entry":  lambda args: add_khata_entry(**args),
    "settle_khata":     lambda args: settle_khata(**args),
}
