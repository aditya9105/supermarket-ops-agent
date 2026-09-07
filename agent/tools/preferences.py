"""
Preferences / cross-session memory tools.

All owner preferences are stored in the `preferences` table (key-value).
They are loaded fresh at the start of EVERY agent turn and injected into the
system prompt — so they persist across bot restarts and new Telegram sessions.

Supported keys:
  default_payment_mode       → CASH | UPI | CARD
  preferred_brand_atta       → e.g. Aashirvaad
  preferred_brand_oil        → e.g. Fortune
  preferred_brand_salt       → e.g. Tata
  shop_name                  → overrides SHOP_NAME env var
  shop_gstin                 → overrides SHOP_GSTIN env var
  shop_address               → overrides SHOP_ADDRESS env var
  shop_phone                 → overrides SHOP_PHONE env var
"""
import logging
from db.connection import get_conn

logger = logging.getLogger(__name__)

ALLOWED_KEYS = {
    "default_payment_mode",
    "preferred_brand_atta",
    "preferred_brand_oil",
    "preferred_brand_salt",
    "preferred_brand_tea",
    "preferred_brand_rice",
    "preferred_brand_dal",
    "preferred_brand_sugar",
    "shop_name",
    "shop_gstin",
    "shop_address",
    "shop_phone",
}


def get_preferences() -> dict:
    """Return all stored preferences as a flat dict."""
    conn = get_conn()
    rows = conn.execute("SELECT pref_key, pref_value FROM preferences").fetchall()
    prefs = {r["pref_key"]: r["pref_value"] for r in rows}
    return {"preferences": prefs}


def set_preference(key: str, value: str) -> dict:
    """Upsert a preference. Rejects unknown keys."""
    if key not in ALLOWED_KEYS:
        return {
            "error": "unknown_preference_key",
            "allowed_keys": sorted(ALLOWED_KEYS),
            "message": f"'{key}' is not a recognised preference key.",
        }
    conn = get_conn()
    conn.execute(
        """INSERT INTO preferences (pref_key, pref_value, updated_at)
           VALUES (?, ?, datetime('now'))
           ON CONFLICT(pref_key) DO UPDATE
           SET pref_value = excluded.pref_value,
               updated_at = datetime('now')""",
        (key, str(value).strip()),
    )
    conn.commit()
    logger.info(f"Preference set: {key}={value}")
    return {"status": "ok", "key": key, "value": value, "message": f"Preference '{key}' saved."}


def load_preferences_for_prompt() -> str:
    """
    Called by the agent loop to inject preferences into the system prompt.
    Returns a formatted string section, or empty string if no prefs set.
    """
    prefs = get_preferences()["preferences"]
    if not prefs:
        return ""
    lines = ["## Owner Preferences (from database — always up to date)"]
    for k, v in sorted(prefs.items()):
        lines.append(f"- **{k}**: {v}")
    return "\n".join(lines)


# ─── Tool schemas ─────────────────────────────────────────────────────────────

PREFERENCE_TOOLS = [
    {
        "name": "get_preferences",
        "description": (
            "Retrieve all stored owner preferences (default payment mode, "
            "preferred brands, shop details). These persist across sessions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "set_preference",
        "description": (
            "Save an owner preference to the database so it persists across sessions. "
            "Examples: default_payment_mode=UPI, preferred_brand_atta=Aashirvaad, "
            "shop_name='Sharma General Store', shop_gstin='27AABCU9603R1ZX'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "key":   {"type": "string", "description": "Preference key (see allowed keys in description)."},
                "value": {"type": "string", "description": "Value to store."},
            },
            "required": ["key", "value"],
        },
    },
]

PREFERENCE_HANDLERS: dict[str, callable] = {
    "get_preferences": lambda args: get_preferences(**args),
    "set_preference":  lambda args: set_preference(**args),
}
