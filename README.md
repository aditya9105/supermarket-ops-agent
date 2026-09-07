# Supermarket Ops Agent

**Live Bot:** [@adityas_supermarket_bot](https://t.me/adityas_supermarket_bot)

A conversational Telegram bot that lets an Indian kirana (grocery) store owner manage their entire shop through natural-language chat — billing, inventory, credit (khata), daily close, GST-correct PDF invoices, and PPTX analysis decks. No web dashboard, no forms. The chat is the product.

---

## Harness Choice: Google Gemini (google-genai SDK)

This project uses the **Google `google-genai` Python SDK** (Gemini 3.5 Flash Lite) as the agent runtime.

**Why Gemini instead of Claude?** Budget and capability. Gemini Flash Lite models are available on Google AI Studio's free tier with no billing account required — making this viable for a small kirana operator who shouldn't need to spend on AI infrastructure. This satisfies the brief's "or equivalent" clause: the system uses genuine LLM-driven tool orchestration (observe → reason → act), not a hardcoded router.

| Requirement | Why Gemini SDK satisfies it |
|---|---|
| Observe → Reason → Act loop | Gemini's `function_call` parts in the response natively chain multiple tool calls within one turn — the model sees each tool result and decides what to call next, without any orchestration framework |
| Grounding (no hallucinated prices) | Tool schemas use strict JSON Schema input definitions; the model can only reference products that `search_products` returned — it has no other way to get a sku_id or price |
| Reliable guardrails | Gemini follows explicit refusal instructions combined with tool-layer enforcement (defense-in-depth) |
| Structured output | Tool result JSON is parsed and validated before being fed back to the model, preventing prompt injection from corrupted data |
| Rate-limit resilience | Free tier (~15 RPM) is handled with exponential backoff (up to 4 retries: 4 s → 8 s → 16 s → 32 s) |

No LangChain, no LlamaIndex — just the raw `google-genai` SDK and a clean control loop in `agent/loop.py`.

---

## How the Control Loop Works

```
User message (Telegram)
        │
        ▼
agent/loop.py: run_agent_turn()
        │
        ├─ 1. Load preferences from DB → inject into system prompt
        │
        ├─ 2. Send messages + tool declarations to Gemini API
        │        (with exponential backoff on 429 rate-limit errors)
        │
        ├─ 3. Gemini returns function_call parts
        │        │
        │        └─ For each function_call part:
        │               • Route to TOOL_HANDLERS[name](args)
        │               • Extract any binary artifacts (PDF/PPTX bytes)
        │               • Append function_response parts
        │
        ├─ 4. Loop back to step 2 (up to 12 rounds)
        │
        └─ 5. Gemini returns text parts only → final text reply
                │
                ▼
        Telegram: send text reply + any document artifacts
```

The loop is **stateless across restarts** — all durable state lives in SQLite. The in-memory `conversation_histories` dict only holds the current session's message context (trimmed to last 30 turns).

---

## Tool / Skill Design

Tools are thin Python functions, each doing **one database operation or file generation**. The model composes them. There are no if/elif intent routers.

### Inventory (6 tools)
| Tool | Purpose |
|---|---|
| `search_products` | Fuzzy search — always the first call for any product reference |
| `get_product` | Fetch single SKU by ID after disambiguation |
| `add_product` | Insert new SKU with validation |
| `receive_stock` | Atomically increment stock (BEGIN IMMEDIATE) |
| `get_stock_report` | Full inventory or low-stock-only list |
| `update_product_price` | Update MRP/cost with guardrail |

### Billing (7 tools)
| Tool | Purpose |
|---|---|
| `start_bill` | Create draft, idempotent |
| `add_bill_item` | Add line to draft (no stock change) |
| `edit_bill_item` | Update line qty/price |
| `remove_bill_item` | Remove a line |
| `get_bill_preview` | Full GST preview, no side effects |
| `finalize_bill` | Atomic stock decrement + payment record + idempotency |
| `cancel_bill` | Cancel draft only |

### Khata / Credit (5 tools)
| Tool | Purpose |
|---|---|
| `search_customer` | Fuzzy customer search |
| `add_customer` | Register new customer |
| `get_khata_balance` | Full ledger + running balance |
| `add_khata_entry` | Manual debit/credit entry |
| `settle_khata` | Payment received (refuses nonexistent customer) |

### Reports (2 tools)
| Tool | Purpose |
|---|---|
| `daily_close` | Day summary, stamps bills |
| `get_sales_report` | Aggregated sales by day/product/mode |

### Artifacts (2 tools)
| Tool | Purpose |
|---|---|
| `generate_invoice_pdf` | reportlab PDF with GST table |
| `generate_analysis_deck` | matplotlib charts in python-pptx |

### Preferences (2 tools)
| Tool | Purpose |
|---|---|
| `get_preferences` | Load all owner prefs from DB |
| `set_preference` | Save a pref (persists across sessions) |

---

## Hard Requirements — Enforcement Map

### 1. Grounding
**How it's enforced:** `agent/tools/inventory.py:search_products()` is the only way to get a `sku_id`. Every tool that touches a product (`add_bill_item`, `receive_stock`, etc.) requires `sku_id` as input — the model cannot call them with just a name. The system prompt explicitly instructs: *"ALWAYS call `search_products` before referencing any product."* The tool layer validates the `sku_id` against the DB on every call.

### 2. Oversell Guard
**How it's enforced:** `agent/tools/billing.py:finalize_bill()` — inside a `BEGIN IMMEDIATE` transaction, each line executes:
```sql
UPDATE products
SET stock_qty = stock_qty - ?
WHERE sku_id = ? AND stock_qty >= ? AND active = 1
```
If `rowcount == 0` (stock insufficient), the **entire transaction is rolled back** — no stock changes, bill stays DRAFT. This is a DB-layer constraint, not a prompt instruction. `add_bill_item` also has a soft pre-check to catch obvious errors early.

### 3. GST Correctness
**How it's enforced:** `agent/tools/billing.py:_calc_line()`:
```python
taxable = round(unit_price * qty, 2)
rate    = tax_slab / 100 / 2          # half-slab for CGST, same for SGST
cgst    = round(taxable * rate, 2)
sgst    = round(taxable * rate, 2)    # always equals CGST (intra-state)
```
Grand total = `round(sum(line_totals))` — rounded to **nearest rupee** (standard Indian kirana billing; per-line stored to 2dp). Tax slab per SKU is stored in the DB and fetched at bill time — never inferred by the model.

### 4. Multi-Turn Bills
**How it's enforced:** `start_bill` creates a `DRAFT` bill row. `add/edit/remove_bill_item` only modify `bill_items` rows. **Stock is untouched until `finalize_bill`.** The draft persists in SQLite so the owner can add items across multiple messages, come back later, and still finalize correctly.

### 5. Idempotency
**How it's enforced:** `agent/tools/billing.py:finalize_bill()` — first action before any mutation:
```python
existing = conn.execute(
    "SELECT result_json FROM idempotency_keys WHERE idem_key = ?", (key,)
).fetchone()
if existing:
    return json.loads(existing["result_json"])  # replay stored result, no-op
```
The idempotency key + serialized result are written inside the **same `BEGIN IMMEDIATE` transaction** as the stock decrements. Telegram's `update_id` is hashed to derive the key (`bot.py:_make_idem_key`).

### 6. Concurrency Safety
**How it's enforced:** All write operations use `conn.execute("BEGIN IMMEDIATE")` which serializes writers on SQLite. WAL journal mode (`PRAGMA journal_mode=WAL`) allows concurrent readers without blocking. Per-line stock decrements use `stock_qty >= qty` in the WHERE clause — if two bills race for the same stock, the second gets `rowcount=0` and rolls back cleanly.

### 7. Guardrails
**How it's enforced at the tool layer (not prompt):**
- **Below cost:** `add_bill_item` / `edit_bill_item` return `{"error": "below_cost"}` unless `confirm_below_cost=True` is explicitly passed.
- **Nonexistent khata customer:** `settle_khata` does a DB lookup for `customer_id` before any insert — returns `{"error": "customer_not_found"}` if not found.
- **MRP below cost:** `add_product` and `update_product_price` check `mrp >= cost_price` and refuse.
- **No deletion:** There is no delete tool. Products can only be set `active=0`. The system prompt confirms this.
- **Cancel paid bill:** `cancel_bill` checks status and refuses with `{"error": "cannot_cancel_paid"}`.

### 8. Real Artifacts
**How it's enforced:** `agent/tools/artifacts.py`:
- **PDF:** `generate_invoice_pdf` uses `reportlab.platypus` to build an actual A4 document with a `Table` widget showing per-item HSN, taxable amount, CGST %, CGST ₹, SGST %, SGST ₹, line total, plus a per-slab GST summary section and a grand-total box. Returns raw bytes.
- **PPTX:** `generate_analysis_deck` calls `get_sales_report` three times (day/product/mode) and `get_stock_report`, renders four real `matplotlib` figures (line chart, horizontal bar, pie, stock health bar), saves each to a PNG buffer, and embeds them in slides via `python-pptx`. Returns raw bytes.
Both are sent as Telegram `send_document` calls by the bot layer.

### 9. Cross-Session Memory
**How it's enforced:** `agent/tools/preferences.py:load_preferences_for_prompt()` is called at the **start of every `run_agent_turn()` call** in `agent/loop.py`. It reads the `preferences` table fresh from SQLite and returns a formatted string injected into the system prompt. This means preferences apply even after a bot restart or a new Telegram session — they never depend on conversation history being present.

---

## Project Structure

```
supermarket-ops-agent/
├── bot.py                  # Telegram bot entry point
├── requirements.txt
├── Procfile                # Railway: web: python bot.py
├── railway.toml
├── .env.example            # All required env vars documented
├── agent/
│   ├── loop.py             # Gemini function-calling control loop
│   ├── system_prompt.py    # Static + dynamic system prompt
│   └── tools/
│       ├── __init__.py     # Tool registry (TOOL_SCHEMAS, TOOL_HANDLERS)
│       ├── inventory.py    # 6 inventory tools
│       ├── billing.py      # 7 billing tools (GST engine, oversell, idempotency)
│       ├── khata.py        # 5 khata tools
│       ├── reports.py      # 2 report tools
│       ├── artifacts.py    # PDF + PPTX generators
│       └── preferences.py  # 2 preference/memory tools
├── db/
│   ├── connection.py       # Thread-local SQLite + WAL mode
│   └── migrations.py       # Schema + 22 seed SKUs
└── tests/
    ├── conftest.py         # In-memory DB fixture
    ├── test_inventory.py
    ├── test_billing.py     # Oversell, idempotency, GST, concurrency
    └── test_khata.py
```

---

## Setup & Running

```bash
# 1. Clone and install
pip install -r requirements.txt

# 2. Configure environment
cp .env.example .env
# Edit .env: set TELEGRAM_TOKEN, GEMINI_API_KEY, SHOP_NAME, SHOP_GSTIN
# Get a free GEMINI_API_KEY from: https://aistudio.google.com/apikey

# 3. Run locally
python bot.py

# 4. Run tests
pytest tests/ -v
```

---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `TELEGRAM_TOKEN` | ✅ | From @BotFather |
| `GEMINI_API_KEY` | ✅ | Free-tier key from [Google AI Studio](https://aistudio.google.com/apikey) |
| `SHOP_NAME` | ✅ | Appears on PDF invoices |
| `SHOP_GSTIN` | ✅ | Your GST registration number |
| `SHOP_ADDRESS` | optional | Shop address for invoices |
| `SHOP_PHONE` | optional | Shop phone for invoices |
| `DB_PATH` | optional | SQLite file path (default: `data/supermarket.db`) |
| `GEMINI_MODEL` | optional | Model to use (default: `gemini-3.5-flash-lite`) |
| `ALLOWED_USER_IDS` | optional | Comma-separated Telegram user IDs for access control |
| `LOG_LEVEL` | optional | `INFO` (default), `DEBUG`, `WARNING` |

---

## Deploying to Railway

1. Push this repo to GitHub.
2. Create a new Railway project → "Deploy from GitHub repo".
3. Add all environment variables from the table above in Railway's Variables tab.
4. Railway auto-detects the `Procfile` and runs `python bot.py`.
5. The SQLite DB persists in Railway's ephemeral disk; for production use Railway's persistent volume or migrate to PostgreSQL.

---

## Example Conversations

```
Owner: got 50 packs maggi today
Bot:   ✅ Received 50 packets of Maggi Masala Noodles 70g. Stock: 170.

Owner: make a bill, 3 amul butter 100g and 2 surf excel 500g
Bot:   Bill #4 started. Added items:
       • 3 × Amul Butter 100g @ ₹60 = ₹180 (+12% GST)
       • 2 × Surf Excel 500g @ ₹110 = ₹220 (+18% GST)
       Preview: Subtotal ₹400, CGST ₹22.20, SGST ₹22.20 → Total ₹445
       Payment mode?

Owner: upi, ref 4567
Bot:   ✅ Bill #4 finalized. ₹445 via UPI (ref: 4567). Generate invoice?

Owner: yes
Bot:   📎 [sends invoice_4_2026-09-06.pdf]

Owner: ramu paid 300
Bot:   Searching for customer 'ramu'... Found: Ramu Lal (balance ₹750).
       ✅ Recorded ₹300 payment. Remaining balance: ₹450.

Owner: day close
Bot:   📊 Day close for 2026-09-06:
       Bills: 12 | Total sales: ₹8,450
       CGST: ₹245.80 | SGST: ₹245.80
       Cash: ₹3,200 | UPI: ₹4,800 | Card: ₹450
       Top item: Maggi Masala Noodles (42 packs, ₹588)
```

---

## GST Reference (as implemented)

| Category | Examples | HSN | GST% |
|---|---|---|---|
| Loose staples | loose atta, rice, sugar, dal | 1006, 1101, 1701, 0713 | 0% |
| Packaged staples | sealed atta, packaged rice | 1101, 1006 | 5% |
| Edible oil | Fortune Sunflower Oil | 1512 | 5% |
| Tea / coffee | Tata Tea, Red Label | 0902 | 5% |
| Dairy | Amul Butter | 0405 | 12% |
| Noodles | Maggi | 1902 | 12% |
| Biscuits | Parle-G, Marie | 1905 | 18% |
| Detergents | Surf Excel | 3402 | 18% |
| Salt | Tata Salt | 2501 | 0% |
