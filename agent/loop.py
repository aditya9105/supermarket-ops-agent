"""
Agent control loop — Gemini function-calling implementation.

Flow per turn:
  1. Load preferences from DB → inject into system prompt.
  2. Build Gemini tool declarations from the existing TOOL_SCHEMAS registry.
  3. Send user message + conversation history to Gemini with all tool declarations.
  4. Gemini returns either:
     (a) a final text response (no function_call parts) → send to user.
     (b) one or more function_call parts → execute each tool handler
         → feed function_response parts back → repeat until (a).
  5. Repeat up to MAX_TOOL_ROUNDS times.

Rate limiting:
  Gemini free tier allows ~15 RPM. Transient 429 errors are retried with
  exponential backoff (up to 4 retries: 4 s → 8 s → 16 s → 32 s, ±20% jitter).

Artifact handling:
  If a tool result contains "bytes" (PDF or PPTX), the loop extracts it and
  returns it separately via the `artifacts` list so the Telegram layer can
  send it as a document.
"""
import json
import logging
import os
import random
import time
from typing import Any

from google import genai
from google.genai import types

from agent.system_prompt import build_system_prompt
from agent.tools import TOOL_SCHEMAS, TOOL_HANDLERS
from agent.tools.preferences import load_preferences_for_prompt

logger = logging.getLogger(__name__)

MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
MAX_TOOL_ROUNDS = 12   # safety cap to prevent runaway loops

# Retry config for rate-limit errors (Gemini free tier: ~15 RPM)
_RETRY_BASE_SECONDS = 4
_RETRY_MAX_ATTEMPTS = 4


# ─── Schema conversion ─────────────────────────────────────────────────────────

def _strip_unsupported_keys(schema: dict) -> dict:
    """
    Recursively remove JSON Schema keys that Gemini's validator rejects.
    Currently strips "default" from property definitions (not part of the
    OpenAPI subset Gemini accepts).
    """
    result = {}
    for k, v in schema.items():
        if k == "default":
            continue
        if isinstance(v, dict):
            result[k] = _strip_unsupported_keys(v)
        elif isinstance(v, list):
            result[k] = [
                _strip_unsupported_keys(item) if isinstance(item, dict) else item
                for item in v
            ]
        else:
            result[k] = v
    return result


def _build_gemini_tools() -> list[types.Tool]:
    """
    Convert TOOL_SCHEMAS (Anthropic-format dicts with "input_schema" key) into
    a single Gemini Tool containing a list of FunctionDeclarations.

    Anthropic format:
        {"name": "...", "description": "...", "input_schema": {JSON Schema}}

    Gemini format:
        types.FunctionDeclaration(name=..., description=..., parameters_json_schema={JSON Schema})

    The JSON Schema object inside "input_schema" is structurally identical to
    what parameters_json_schema expects — only the wrapper key differs.
    """
    declarations = []
    for schema in TOOL_SCHEMAS:
        raw_params = schema.get("input_schema", {"type": "object", "properties": {}})
        clean_params = _strip_unsupported_keys(raw_params)
        declarations.append(
            types.FunctionDeclaration(
                name=schema["name"],
                description=schema.get("description", ""),
                parameters_json_schema=clean_params,
            )
        )
    return [types.Tool(function_declarations=declarations)]


# ─── Rate-limit retry ──────────────────────────────────────────────────────────

def _call_with_retry(client: genai.Client, **kwargs) -> Any:
    """
    Call client.models.generate_content(**kwargs) with exponential backoff on
    HTTP 429 (rate limit) errors from Gemini's free tier.

    Raises the underlying exception after MAX_RETRY_ATTEMPTS failures.
    """
    last_exc = None
    for attempt in range(_RETRY_MAX_ATTEMPTS):
        try:
            return client.models.generate_content(**kwargs)
        except Exception as exc:
            # google-genai raises ClientError or APIError; check message for 429
            exc_str = str(exc)
            is_rate_limit = (
                "429" in exc_str
                or "RESOURCE_EXHAUSTED" in exc_str
                or "quota" in exc_str.lower()
            )
            if not is_rate_limit:
                raise  # non-retryable, propagate immediately

            last_exc = exc
            delay = _RETRY_BASE_SECONDS * (2 ** attempt)
            jitter = delay * random.uniform(-0.2, 0.2)
            wait = delay + jitter
            logger.warning(
                f"Gemini rate limit hit (attempt {attempt + 1}/{_RETRY_MAX_ATTEMPTS}). "
                f"Retrying in {wait:.1f}s…"
            )
            time.sleep(wait)

    raise last_exc  # exhausted retries


# ─── History trimming ─────────────────────────────────────────────────────────

def _is_function_response_turn(content: types.Content) -> bool:
    """
    Return True if this Content is a "user" turn that carries only
    function_response parts (i.e. a tool-result turn, not a real user message).
    """
    return (
        content.role == "user"
        and bool(content.parts)
        and all(p.function_response is not None for p in content.parts)
    )


def _trim_history(messages: list[types.Content], max_turns: int) -> list[types.Content]:
    """
    Trim conversation history to at most *max_turns* entries while preserving
    Gemini's required turn structure:

      1. Every model turn that contains function_call parts MUST be immediately
         followed by a user turn that contains the matching function_response
         parts.  A naive tail-slice can sever this pair, leaving an orphaned
         function_response at the start of history → 400 INVALID_ARGUMENT.

      2. The first turn in the trimmed history MUST be a real "user" message
         (not a function_response turn), because Gemini requires conversations
         to begin with a user turn and does not accept a function_response as
         the opening message.

    Strategy:
      - If the list is short enough, return it unchanged.
      - Otherwise take the last *max_turns* entries, then scan forward from
        index 0 and drop any leading turns until we land on a clean user turn
        that is NOT a function_response.  This guarantees both invariants.
    """
    if len(messages) <= max_turns:
        return messages

    trimmed = messages[-max_turns:]

    # Walk forward until we reach a clean user turn (not a function_response).
    # This ensures we never start mid-pair and never start with a stray
    # function_response whose matching function_call was sliced away.
    start = 0
    while start < len(trimmed):
        turn = trimmed[start]
        if turn.role == "user" and not _is_function_response_turn(turn):
            break
        # This turn is either a model turn (possibly with function_calls whose
        # paired response was already included) or a function_response whose
        # function_call was cut off — drop it.
        start += 1

    return trimmed[start:]


# ─── Main agent loop ───────────────────────────────────────────────────────────

# Build tool declarations once at module load (schemas are static)
_GEMINI_TOOLS = _build_gemini_tools()


def run_agent_turn(
    user_message: str,
    conversation_history: list,
) -> dict[str, Any]:
    """
    Run one user turn through the Gemini agent loop.

    Args:
        user_message: The raw text from the owner.
        conversation_history: List of prior turns as types.Content objects
                              (role "user" / "model"). Empty list on first turn.

    Returns:
        {
            "text":      str   — the agent's final reply to show the owner,
            "history":   list  — updated conversation_history (types.Content list),
            "artifacts": list  — [{"filename": str, "bytes": bytes}, ...] if any files generated,
        }
    """
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    # Fresh preferences on every turn (cross-session memory enforcement)
    try:
        prefs_section = load_preferences_for_prompt()
    except Exception:
        prefs_section = ""

    system_instruction = build_system_prompt(prefs_section)

    # Append the new user message to history
    messages: list[types.Content] = list(conversation_history) + [
        types.Content(role="user", parts=[types.Part.from_text(text=user_message)])
    ]

    artifacts: list[dict] = []
    reply_text = ""

    for round_num in range(MAX_TOOL_ROUNDS):
        logger.debug(f"Agent loop round {round_num + 1}")

        response = _call_with_retry(
            client,
            model=MODEL,
            contents=messages,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                tools=_GEMINI_TOOLS,
                automatic_function_calling=types.AutomaticFunctionCallingConfig(
                    disable=True  # we handle tool calls manually for full control
                ),
                max_output_tokens=4096,
            ),
        )

        # Gemini response structure: response.candidates[0].content.parts
        candidate = response.candidates[0]
        response_content = candidate.content  # types.Content(role="model", parts=[...])

        # Append this model turn to messages
        messages.append(response_content)

        # Collect any function_call parts and any text parts
        function_call_parts = [
            p for p in response_content.parts if p.function_call is not None
        ]
        text_parts = [
            p for p in response_content.parts
            if p.text is not None and p.text.strip()
        ]

        if not function_call_parts:
            # No tool calls → this is the final text response
            for part in text_parts:
                reply_text += part.text
            break

        # ── Process tool calls ────────────────────────────────────────────────
        function_response_parts: list[types.Part] = []

        for part in function_call_parts:
            fc = part.function_call
            tool_name = fc.name
            # fc.args is a dict-like Struct; convert to plain dict
            tool_args = dict(fc.args) if fc.args else {}

            logger.info(
                f"Tool call: {tool_name}({json.dumps(tool_args, default=str)[:200]})"
            )

            handler = TOOL_HANDLERS.get(tool_name)
            if handler is None:
                result = {"error": "unknown_tool", "tool": tool_name}
            else:
                try:
                    result = handler(tool_args)
                except Exception as exc:
                    logger.error(f"Tool {tool_name} raised: {exc}", exc_info=True)
                    result = {"error": "tool_exception", "detail": str(exc)}

            # Extract binary artifacts before serializing to JSON
            file_bytes = result.pop("bytes", None) if isinstance(result, dict) else None
            filename   = result.get("filename") if isinstance(result, dict) else None
            if file_bytes and filename:
                artifacts.append({"filename": filename, "bytes": file_bytes})
                result["bytes_sent_as_document"] = True

            function_response_parts.append(
                types.Part.from_function_response(
                    name=tool_name,
                    response={"result": json.dumps(result, default=str)},
                )
            )

        # Append tool results as a "user" turn (Gemini convention)
        messages.append(
            types.Content(role="user", parts=function_response_parts)
        )
        continue

    else:
        reply_text = "⚠️ I hit my processing limit. Please break your request into smaller steps."

    # Trim history to last 30 turns for memory efficiency.
    # Use _trim_history instead of a raw slice to guarantee that:
    #   (a) function_call / function_response pairs are never split across the
    #       trim boundary (which causes Gemini's 400 INVALID_ARGUMENT error), and
    #   (b) the trimmed history always starts on a clean user (non-function-
    #       response) turn.
    MAX_HISTORY = 30
    updated_history = _trim_history(messages, MAX_HISTORY)

    return {
        "text":      reply_text or "Done.",
        "history":   updated_history,
        "artifacts": artifacts,
    }
