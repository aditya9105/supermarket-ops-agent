# agent/tools/__init__.py
"""
Tool registry — exports TOOL_SCHEMAS (list of tool dicts, converted to
Gemini FunctionDeclarations in agent/loop.py) and TOOL_HANDLERS (name → callable).
"""
from .inventory import INVENTORY_TOOLS, INVENTORY_HANDLERS
from .billing import BILLING_TOOLS, BILLING_HANDLERS
from .khata import KHATA_TOOLS, KHATA_HANDLERS
from .reports import REPORT_TOOLS, REPORT_HANDLERS
from .artifacts import ARTIFACT_TOOLS, ARTIFACT_HANDLERS
from .preferences import PREFERENCE_TOOLS, PREFERENCE_HANDLERS

TOOL_SCHEMAS: list[dict] = (
    INVENTORY_TOOLS
    + BILLING_TOOLS
    + KHATA_TOOLS
    + REPORT_TOOLS
    + ARTIFACT_TOOLS
    + PREFERENCE_TOOLS
)

TOOL_HANDLERS: dict[str, callable] = {
    **INVENTORY_HANDLERS,
    **BILLING_HANDLERS,
    **KHATA_HANDLERS,
    **REPORT_HANDLERS,
    **ARTIFACT_HANDLERS,
    **PREFERENCE_HANDLERS,
}

__all__ = ["TOOL_SCHEMAS", "TOOL_HANDLERS"]
