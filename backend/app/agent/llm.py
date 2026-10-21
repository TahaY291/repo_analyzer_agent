import asyncio
import json
import logging
import re
from functools import lru_cache

import groq
from langchain_core.messages import HumanMessage
from langchain_groq import ChatGroq
from pydantic import BaseModel

from app.core.config import settings

log = logging.getLogger(__name__)


class LLMBusy(Exception):
    """The model provider kept rate-limiting us. Not a RuntimeError, so node 2 won't swallow it."""


def get_llm(max_output_tokens: int | None = None) -> ChatGroq:
    if not settings.groq_api_key:
        raise RuntimeError("GROQ_API_KEY is not set. Add it to your .env file.")
    return ChatGroq(
        model=settings.groq_model,
        api_key=settings.groq_api_key,
        temperature=0,
        max_tokens=max_output_tokens or settings.llm_max_output_tokens,
        max_retries=2,
        timeout=60,
    )


# ---------- rate limits: wait as long as the provider asks, then retry ----------

_RETRY_IN = re.compile(r"try again in ([\d.]+)\s*(ms|s|m)\b", re.I)


def _wait_seconds(e: groq.RateLimitError) -> float:
    """How long Groq says to wait: the retry-after header first, then the message text."""
    header = e.response.headers.get("retry-after")
    if header:
        try:
            return float(header)
        except ValueError:
            pass
    match = _RETRY_IN.search(str(e))
    if match:
        value, unit = float(match.group(1)), match.group(2).lower()
        return value / 1000 if unit == "ms" else value * 60 if unit == "m" else value
    return 15.0


async def _call_with_wait(make_call, max_attempts: int = 4, max_wait: float = 60.0):
    """Run an LLM call. On a 429, sleep the suggested time and try again."""
    for attempt in range(max_attempts):
        try:
            return await make_call()
        except groq.RateLimitError as e:
            wait = _wait_seconds(e) + 1   # +1s safety margin
            if attempt == max_attempts - 1 or wait > max_wait:
                raise LLMBusy("The AI service is busy right now. Please try again in a minute.") from e
            log.warning("Groq rate limit reached, waiting %.1fs (attempt %s)", wait, attempt + 1)
            await asyncio.sleep(wait)


# ---------- plain JSON output (nodes 3 and 4), no tool calls ----------

@lru_cache
def _json_llm(max_output_tokens: int | None):
    return get_llm(max_output_tokens)


def _extract_json(text: str) -> str:
    """Models sometimes wrap JSON in ```json fences or add a sentence before it."""
    start, end = text.find("{"), text.rfind("}")
    return text[start : end + 1] if start != -1 and end > start else text


async def ainvoke_json(
    messages: list, schema: type[BaseModel], max_output_tokens: int | None = None, retries: int = 2
) -> dict:
    """Returns {"raw": message or None, "parsed": schema instance or None}."""
    instruction = HumanMessage(
        "Reply with ONLY one JSON object that matches this JSON schema. "
        "No markdown, no extra text, and do not call any tool.\n" + json.dumps(schema.model_json_schema())
    )
    convo = [*messages, instruction]
    raw = None
    for attempt in range(retries + 1):
        try:
            raw = await _call_with_wait(lambda: _json_llm(max_output_tokens).ainvoke(convo))
            return {"raw": raw, "parsed": schema.model_validate_json(_extract_json(str(raw.content)))}
        except ValueError as e:   # pydantic's ValidationError is a ValueError
            log.warning("JSON invalid (attempt %s): %s", attempt + 1, str(e)[:200])
            fix = f"Your last reply was not valid: {str(e)[:300]} Reply again with ONLY the JSON object."
        except groq.BadRequestError as e:
            log.warning("Groq rejected the request (attempt %s): %s", attempt + 1, str(e)[:200])
            fix = "Reply again with ONLY the JSON object. Do not call any tool."
        # At temperature 0 the same input gives the same output, so the retry must change the input.
        convo = [*messages, instruction, HumanMessage(fix)]
    return {"raw": raw, "parsed": None}


# ---------- tool calls (node 2) ----------

async def ainvoke_tool_call(llm, messages: list, retries: int = 2):
    """Call a tool-bound LLM. If Groq rejects a call to a tool that does not exist, retry with a reminder."""
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        msgs = messages if attempt == 0 else [
            *messages,
            HumanMessage("Reminder: call exactly one of the tools you were given, using its exact name and argument names."),
        ]
        try:
            return await _call_with_wait(lambda: llm.ainvoke(msgs))
        except groq.BadRequestError as e:
            last_error = e
            log.warning("Tool call rejected (attempt %s): %s", attempt + 1, str(e)[:200])
    raise RuntimeError(f"The model kept making invalid tool calls: {str(last_error)[:200]}")