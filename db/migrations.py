"""
Database schema migrations.
Run once on startup; idempotent (CREATE TABLE IF NOT EXISTS).
"""
import logging
from db.connection import get_conn

logger = logging.getLogger(__name__)

SCHEMA_SQL = """
-- ── Products / SKU catalogue ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS products (
    sku_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL,
    brand           TEXT    NOT NULL DEFAULT '',
    unit            TEXT    NOT NULL CHECK(unit IN ('kg','g','litre','ml','packet','dozen','piece')),
    mrp             REAL    NOT NULL,
    cost_price      REAL    DEFAULT NULL,   -- NULL until first stock receipt; never invented
    stock_qty       REAL    NOT NULL DEFAULT 0,
    reorder_level   REAL    NOT NULL DEFAULT 0,
    tax_slab        INTEGER NOT NULL DEFAULT 0 CHECK(tax_slab IN (0,5,12,18)),
    hsn_code        TEXT    NOT NULL,
    is_loose        INTEGER NOT NULL DEFAULT 0,   -- 1 = sold by weight / measure (no sealed packing)
    active          INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- ── Customers ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS customers (
    customer_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL,
    phone           TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- ── Bills (header) ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS bills (
    bill_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_name   TEXT,
    customer_id     INTEGER REFERENCES customers(customer_id),
    status          TEXT    NOT NULL DEFAULT 'DRAFT'
                            CHECK(status IN ('DRAFT','PAID','CANCELLED')),
    payment_mode    TEXT    CHECK(payment_mode IN ('CASH','UPI','CARD')),
    payment_ref     TEXT,
    subtotal        REAL,       -- pre-tax sum, set on finalize
    cgst_total      REAL,
    sgst_total      REAL,
    grand_total     REAL,
    bill_date       TEXT,       -- ISO date string, set on finalize
    close_date      TEXT,       -- set during daily_close
    idempotency_key TEXT        UNIQUE,
    created_at      TEXT        NOT NULL DEFAULT (datetime('now'))
);

-- ── Bill line items ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS bill_items (
    line_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    bill_id         INTEGER NOT NULL REFERENCES bills(bill_id) ON DELETE CASCADE,
    sku_id          INTEGER NOT NULL REFERENCES products(sku_id),
    qty             REAL    NOT NULL CHECK(qty > 0),
    unit_price      REAL    NOT NULL,   -- actual sell price per unit
    tax_slab        INTEGER NOT NULL,
    hsn_code        TEXT    NOT NULL,
    taxable_amt     REAL    NOT NULL DEFAULT 0,
    cgst_amt        REAL    NOT NULL DEFAULT 0,
    sgst_amt        REAL    NOT NULL DEFAULT 0,
    line_total      REAL    NOT NULL DEFAULT 0
);

-- ── Khata (credit ledger) entries ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS khata_entries (
    entry_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id     INTEGER NOT NULL REFERENCES customers(customer_id),
    amount          REAL    NOT NULL,   -- +ve = credit extended; -ve = payment received
    note            TEXT,
    bill_id         INTEGER REFERENCES bills(bill_id),
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- ── Stock movements audit log ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS stock_movements (
    movement_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    sku_id          INTEGER NOT NULL REFERENCES products(sku_id),
    delta           REAL    NOT NULL,   -- +ve = received in; -ve = sold / adjusted out
    reason          TEXT    NOT NULL CHECK(reason IN ('SALE','RECEIVE','ADJUSTMENT')),
    bill_id         INTEGER REFERENCES bills(bill_id),
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- ── Idempotency keys ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS idempotency_keys (
    idem_key        TEXT    PRIMARY KEY,
    bill_id         INTEGER REFERENCES bills(bill_id),
    result_json     TEXT    NOT NULL,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- ── Owner preferences ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS preferences (
    pref_key        TEXT    PRIMARY KEY,
    pref_value      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- ── Indexes ───────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_products_name    ON products(name);
CREATE INDEX IF NOT EXISTS idx_products_brand   ON products(brand);
CREATE INDEX IF NOT EXISTS idx_products_active  ON products(active);
CREATE INDEX IF NOT EXISTS idx_bill_items_bill  ON bill_items(bill_id);
CREATE INDEX IF NOT EXISTS idx_bill_items_sku   ON bill_items(sku_id);
CREATE INDEX IF NOT EXISTS idx_khata_customer   ON khata_entries(customer_id);
CREATE INDEX IF NOT EXISTS idx_bills_status     ON bills(status);
CREATE INDEX IF NOT EXISTS idx_bills_date       ON bills(bill_date);
CREATE INDEX IF NOT EXISTS idx_movements_sku    ON stock_movements(sku_id);
CREATE INDEX IF NOT EXISTS idx_customers_name   ON customers(name);
"""


def _migrate_cost_price_nullable():
    """
    One-time migration: relax the NOT NULL constraint on products.cost_price.

    SQLite does not support ALTER COLUMN, so we use the recommended 12-step
    table-rebuild.  This is idempotent — it checks PRAGMA table_info first
    and skips the rebuild when cost_price is already nullable (notnull == 0).

    Why: cost_price must be NULL-able so that a new SKU can be catalogued
    (name, MRP, GST) before any stock has ever been received.  The field is
    set to the real cost price only when receive_stock() is called.  Storing
    0 or an invented value would poison margin calculations.
    """
    conn = get_conn()
    col_info = conn.execute("PRAGMA table_info(products)").fetchall()
    cost_col = next((c for c in col_info if c["name"] == "cost_price"), None)
    if cost_col is None or cost_col["notnull"] == 0:
        # Already nullable (or column missing) — nothing to do.
        return

    logger.info("Migrating products.cost_price to nullable…")
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("BEGIN IMMEDIATE")
    try:
        # 1. Create replacement table with cost_price nullable
        conn.execute("""
            CREATE TABLE IF NOT EXISTS products_new (
                sku_id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name            TEXT    NOT NULL,
                brand           TEXT    NOT NULL DEFAULT '',
                unit            TEXT    NOT NULL CHECK(unit IN ('kg','g','litre','ml','packet','dozen','piece')),
                mrp             REAL    NOT NULL,
                cost_price      REAL    DEFAULT NULL,
                stock_qty       REAL    NOT NULL DEFAULT 0,
                reorder_level   REAL    NOT NULL DEFAULT 0,
                tax_slab        INTEGER NOT NULL DEFAULT 0 CHECK(tax_slab IN (0,5,12,18)),
                hsn_code        TEXT    NOT NULL,
                is_loose        INTEGER NOT NULL DEFAULT 0,
                active          INTEGER NOT NULL DEFAULT 1,
                created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
                updated_at      TEXT    NOT NULL DEFAULT (datetime('now'))
            )
        """)
        # 2. Copy all rows; keep cost_price values as-is (0 is treated as
        #    "not yet set" by the application layer — leave as 0 for existing rows
        #    so margin reports aren't broken; only new rows will be NULL).
        conn.execute("INSERT INTO products_new SELECT * FROM products")
        # 3. Swap
        conn.execute("DROP TABLE products")
        conn.execute("ALTER TABLE products_new RENAME TO products")
        # 4. Re-create indexes (IF NOT EXISTS is safe)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_products_name   ON products(name)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_products_brand  ON products(brand)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_products_active ON products(active)")
        conn.commit()
        logger.info("Migration complete: products.cost_price is now nullable.")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def run_migrations():
    """Apply schema (idempotent). Call once at startup."""
    conn = get_conn()
    try:
        conn.executescript(SCHEMA_SQL)
        conn.commit()
        logger.info("Database migrations applied successfully.")
    except Exception as e:
        logger.error(f"Migration failed: {e}")
        raise
    # Relax NOT NULL on cost_price for existing databases created before this fix.
    _migrate_cost_price_nullable()


def seed_products():
    """
    Insert a baseline catalogue of real Indian kirana SKUs if the table is empty.
    HSN codes and tax slabs are accurate per Indian GST schedule (as of FY 2024-25).
    """
    conn = get_conn()
    count = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    if count > 0:
        logger.info(f"Products table already has {count} rows — skipping seed.")
        return

    # (name, brand, unit, mrp, cost_price, stock_qty, reorder_level,
    #  tax_slab, hsn_code, is_loose)
    SEED_PRODUCTS = [
        # ── Atta / Flour ────────────────────────────────────────────────────
        # Packaged atta → HSN 1101, 5% GST
        ("Atta 5kg",        "Aashirvaad", "packet", 280.0, 245.0, 50, 10, 5,  "1101",  0),
        ("Atta 10kg",       "Fortune",    "packet", 480.0, 420.0, 20, 5,  5,  "1101",  0),
        # Loose atta → 0% GST
        ("Atta (loose)",    "",           "kg",      52.0,  44.0,  100, 20, 0, "1101",  1),

        # ── Salt ─────────────────────────────────────────────────────────────
        # HSN 2501, 0% GST (salt is exempt)
        ("Iodised Salt 1kg","Tata",       "packet",  24.0,  19.0,  80, 15, 0,  "2501",  0),
        ("Salt (loose)",    "",           "kg",      18.0,  14.0,  50, 10, 0,  "2501",  1),

        # ── Dairy ─────────────────────────────────────────────────────────────
        # Amul Butter → HSN 0405, 12% GST
        ("Butter 100g",     "Amul",       "packet", 60.0,  52.0,  30, 5,  12, "0405",  0),
        ("Butter 500g",     "Amul",       "packet", 275.0, 240.0, 15, 3,  12, "0405",  0),

        # ── Edible Oil ───────────────────────────────────────────────────────
        # HSN 1512, 5% GST
        ("Sunflower Oil 1L","Fortune",    "litre",  165.0, 148.0, 40, 10, 5,  "1512",  0),
        ("Sunflower Oil 5L","Fortune",    "packet", 780.0, 700.0, 20, 5,  5,  "1512",  0),

        # ── Noodles / Instant food ───────────────────────────────────────────
        # HSN 1902, 12% GST
        ("Noodles 70g",       "Maggi",   "packet",  14.0,  11.0, 120, 20, 12, "1902",  0),

        # ── Biscuits ─────────────────────────────────────────────────────────
        # HSN 1905, 18% GST
        ("Parle-G 100g",    "Parle",      "packet",  10.0,   8.0, 200, 30, 18, "1905",  0),
        ("Marie Light 200g","Britannia",  "packet",  25.0,  21.0,  80, 15, 18, "1905",  0),

        # ── Detergent ────────────────────────────────────────────────────────
        # HSN 3402, 18% GST
        ("Surf Excel 500g", "Surf Excel", "packet", 110.0,  93.0,  40, 8,  18, "3402",  0),
        ("Surf Excel 1kg",  "Surf Excel", "packet", 210.0, 178.0,  25, 5,  18, "3402",  0),

        # ── Rice ─────────────────────────────────────────────────────────────
        # Loose rice → 0%; packaged → 5%
        ("Basmati Rice (loose)","",       "kg",      85.0,  74.0, 100, 20, 0,  "1006",  1),
        ("Basmati Rice 5kg","India Gate", "packet", 420.0, 370.0,  30, 5,  5,  "1006",  0),

        # ── Dal / Pulses ──────────────────────────────────────────────────────
        # Loose dal → 0%; packaged → 5%
        ("Toor Dal (loose)","",           "kg",      95.0,  84.0,  80, 15, 0,  "0713",  1),
        ("Moong Dal (loose)","",          "kg",     105.0,  92.0,  60, 10, 0,  "0713",  1),

        # ── Sugar ────────────────────────────────────────────────────────────
        # HSN 1701; packaged sugar 5%, loose sugar 0%
        ("Sugar (loose)",   "",           "kg",      46.0,  40.0, 150, 25, 0,  "1701",  1),
        ("Sugar 1kg",       "Madhur",     "packet",  52.0,  46.0,  60, 10, 5,  "1701",  0),

        # ── Tea ──────────────────────────────────────────────────────────────
        # HSN 0902, 5% GST
        ("Dust Tea 250g",   "Tata Tea",   "packet",  88.0,  76.0,  45, 8,  5,  "0902",  0),
        ("Premium Tea 250g","Red Label",  "packet",  95.0,  82.0,  30, 5,  5,  "0902",  0),
    ]

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.executemany(
            """INSERT INTO products
               (name, brand, unit, mrp, cost_price, stock_qty, reorder_level,
                tax_slab, hsn_code, is_loose)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            SEED_PRODUCTS,
        )
        conn.commit()
        logger.info(f"Seeded {len(SEED_PRODUCTS)} products into catalogue.")
    except Exception:
        conn.rollback()
        raise
