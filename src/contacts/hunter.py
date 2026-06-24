"""Hunter.io email discovery via its remote MCP server.

The project talks to Hunter's hosted MCP server (https://mcp.hunter.io/mcp) as
an MCP *client* over streamable HTTP, authenticating with the user's Hunter API
key (X-API-KEY). Claude is never an intermediary.

Hunter is used strictly as an extra *verified* source, layered on top of the
free web-search finder. An address Hunter returns is accepted only when:

  * Hunter's own verifier reports ``status == "valid"`` (deliverable), OR
  * the address carries published ``sources`` (it appears on a real page).

Pattern guesses — a low score with no sources and an ``unknown`` / ``accept_all``
verification — are never returned as usable. This preserves the project's
no-hallucination guarantee: we never hand back an address nobody has confirmed.

Free plan = 50 credits/month (Email Finder 1, Domain Search 1, Verifier 0.5),
so callers should try the free finder first and reach for Hunter only as a
fallback.
"""

from __future__ import annotations
import json
import logging
from typing import List, Optional

from ..mcp_clients import call_hunter_mcp, hunter_client

_logger = logging.getLogger("assistant.contacts.hunter")

# Tool names as advertised by Hunter's remote MCP server (verified live via
# session.list_tools — they are PascalCase-hyphenated, not snake_case).
_TOOL_EMAIL_FINDER = "Email-Finder"
_TOOL_DOMAIN_SEARCH = "Domain-Search"

# Generic mailbox local-parts worth surfacing for outreach, best first.
_GENERIC_PRIORITY = (
    "careers", "career", "jobs", "hr", "recruit", "talent",
    "internship", "hiring", "people", "contact", "info", "hello",
)


def is_enabled() -> bool:
    """True when a Hunter API key is configured (so Hunter calls will run)."""
    return hunter_client() is not None


# ---------------------------------------------------------------------------
# Credit-exhaustion guard
# ---------------------------------------------------------------------------
# Once Hunter reports the monthly usage limit is reached we stop calling it for
# the rest of the process — no point spending network round-trips, and it lets
# us tell the user once instead of on every company. A transient per-request
# rate limit (HTTP 403 "rate limit") does NOT trip this; only the plan/credit
# limit (HTTP 429 "usage limit") does.
_credits_exhausted = False


def _serialise(payload) -> str:
    try:
        return json.dumps(payload, default=str).lower()
    except Exception:
        return str(payload).lower()


def _is_usage_limit(payload) -> bool:
    """True when the payload carries Hunter's monthly usage-limit error
    (429: "You have reached your usage limit. Upgrade your plan if necessary.")."""
    s = _serialise(payload)
    return "usage limit" in s or "upgrade your plan" in s


def _call(tool: str, args: dict) -> dict:
    """Call a Hunter tool, but short-circuit (spending nothing) once the monthly
    credit limit has been hit, and surface that to the user exactly once."""
    global _credits_exhausted
    if _credits_exhausted:
        return {}
    payload = call_hunter_mcp(tool, args)
    if _is_usage_limit(payload):
        _credits_exhausted = True
        _logger.warning("Hunter usage limit reached; skipping Hunter for the rest of this run.")
        try:
            from ..narration.narrator import warn
            warn("No more Hunter credits available — falling back to the free "
                 "email finder for the rest of this run.")
        except Exception:
            pass
        return {}
    return payload


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------

def _extract_json(text: str) -> Optional[dict]:
    """Pull the leading JSON object out of a string. Hunter appends a human
    ``\\n\\nSource: Hunter.io`` footer after the JSON, so a plain json.loads
    fails; raw_decode parses the first object and ignores the trailing text."""
    start = text.find("{")
    if start == -1:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(text[start:])
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _data(payload: dict) -> dict:
    """Unwrap Hunter's ``{"data": {...}}`` envelope; tolerate a bare dict.

    Hunter's MCP tools return JSON *text* with a trailing ``Source:`` footer,
    which our generic client couldn't json.loads — it hands back
    ``{"raw": <text>}``. Recover the real JSON from that here.
    """
    if not isinstance(payload, dict):
        return {}
    if isinstance(payload.get("raw"), str):
        parsed = _extract_json(payload["raw"])
        if parsed is not None:
            payload = parsed
    inner = payload.get("data")
    return inner if isinstance(inner, dict) else payload


def _verification_status(obj: dict) -> str:
    v = obj.get("verification")
    if isinstance(v, dict):
        return (v.get("status") or "").lower()
    # email-verifier returns the status at the top level
    return (obj.get("status") or "").lower()


def _is_verified(obj: dict) -> bool:
    """Accept only a deliverable ('valid') address or one with published sources."""
    if _verification_status(obj) == "valid":
        return True
    sources = obj.get("sources")
    return bool(sources)


def _evidence(obj: dict) -> str:
    status = _verification_status(obj)
    sources = obj.get("sources") or []
    if sources:
        uri = ""
        if isinstance(sources[0], dict):
            uri = sources[0].get("uri") or sources[0].get("domain") or ""
        return f"Hunter: published source {uri}".strip()
    if status == "valid":
        return "Hunter: verifier reports deliverable (valid)"
    return "Hunter result"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def find_email(first: str, last: str, company: str,
               domain: Optional[str] = None) -> Optional[dict]:
    """Hunter Email Finder for a named person. Returns a verified result or None.

    Result shape mirrors ``email_finder.find_email`` on success::

        {"email", "status": "verified", "source": "hunter", "domain", "evidence", "score"}
    """
    # Hunter's Email-Finder requires a domain and a full_name — both mandatory.
    if not is_enabled() or not domain:
        return None
    full_name = f"{(first or '').strip()} {(last or '').strip()}".strip()
    if not full_name:
        return None
    args = {"full_name": full_name, "domain": domain}
    try:
        data = _data(_call(_TOOL_EMAIL_FINDER, args))
    except Exception as exc:
        _logger.debug("Hunter email_finder failed for %s %s: %s", first, last, exc)
        return None

    email = (data.get("email") or "").strip().lower()
    if not email or not _is_verified(data):
        return None
    return {
        "email": email,
        "status": "verified",
        "source": "hunter",
        "domain": data.get("domain") or domain,
        "evidence": _evidence(data),
        "score": data.get("score"),
    }


def generic_company_email(company: str, domain: Optional[str] = None) -> Optional[dict]:
    """Hunter Domain Search for a verified generic mailbox (careers@/hr@/...)."""
    # Hunter's Domain-Search requires a domain.
    if not is_enabled() or not domain:
        return None
    args = {"domain": domain, "type": "generic", "limit": 10}
    try:
        data = _data(_call(_TOOL_DOMAIN_SEARCH, args))
    except Exception as exc:
        _logger.debug("Hunter domain_search failed for %s: %s", company or domain, exc)
        return None

    emails: List[dict] = data.get("emails") or []
    verified = [e for e in emails if isinstance(e, dict) and e.get("value") and _is_verified(e)]
    if not verified:
        return None

    def rank(e: dict) -> int:
        local = e["value"].split("@")[0].lower()
        for i, key in enumerate(_GENERIC_PRIORITY):
            if local.startswith(key):
                return i
        return len(_GENERIC_PRIORITY)

    best = min(verified, key=rank)
    return {
        "email": best["value"].strip().lower(),
        "status": "verified",
        "source": "hunter",
        "domain": data.get("domain") or domain,
        "evidence": _evidence(best),
    }
