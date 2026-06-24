"""RecruiterFinder — LLM tool-calling agent for HR/CEO discovery."""

from __future__ import annotations
import json
import logging
import re
from typing import List, Optional
from datetime import datetime

from ..narration.narrator import say, contact_found, warn
from ..storage.models import HRContact
from ..mcp_clients import call_linkedin_mcp

_logger = logging.getLogger("assistant.agents.recruiter_finder")

# ---------------------------------------------------------------------------
# LLM tool definitions
# ---------------------------------------------------------------------------

_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "check_company_size",
            "description": (
                "Search to determine if a company is a startup/small company (<~500 employees) "
                "or a large enterprise. Look for headcount, employee count, funding stage."
            ),
            "parameters": {
                "type": "object",
                "properties": {"company": {"type": "string"}},
                "required": ["company"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_recruiters",
            "description": (
                "Search the web for HR/Talent Acquisition/recruiter contacts at a company. "
                "If the company has any India presence, set location='India' to find the India HR. "
                "Otherwise leave location empty for global HR."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "company": {"type": "string"},
                    "location": {"type": "string", "description": "'India' or empty"},
                    "query": {"type": "string", "description": "Extra search terms"},
                },
                "required": ["company"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_company_overview",
            "description": "Get general info about a company — products, tech stack, culture.",
            "parameters": {
                "type": "object",
                "properties": {"company": {"type": "string"}},
                "required": ["company"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_email_address",
            "description": (
                "Find and SMTP-verify the email for a named person at a company. "
                "Call once you have a confirmed name from search or LinkedIn."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "first_name": {"type": "string"},
                    "last_name": {"type": "string"},
                    "company": {"type": "string"},
                },
                "required": ["first_name", "last_name", "company"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "verify_person_designation",
            "description": (
                "Search LinkedIn (site:linkedin.com), company website, Crunchbase, and news "
                "to confirm a person currently holds their stated title at the company. "
                "Also searches LinkedIn MCP search_people for confirmation. "
                "Set verified=True only if 2+ independent sources confirm."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "company": {"type": "string"},
                    "title": {"type": "string"},
                },
                "required": ["name", "company", "title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_ceo_or_founder",
            "description": "Search for the CEO or founder of a startup. Only call for startups.",
            "parameters": {
                "type": "object",
                "properties": {"company": {"type": "string"}},
                "required": ["company"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "extract_recruiter_details",
            "description": (
                "Submit all final contact details. Call exactly once at the end "
                "after all research and verification is complete."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "recruiter_name": {"type": "string"},
                    "recruiter_email": {"type": "string"},
                    "recruiter_title": {"type": "string"},
                    "recruiter_linkedin": {"type": "string"},
                    "recruiter_verified": {"type": "boolean"},
                    "recruiter_verification_note": {"type": "string"},
                    "is_startup": {"type": "boolean"},
                    "ceo_name": {"type": "string"},
                    "ceo_email": {"type": "string"},
                    "ceo_title": {"type": "string"},
                    "ceo_linkedin": {"type": "string"},
                    "ceo_verified": {"type": "boolean"},
                    "ceo_verification_note": {"type": "string"},
                    "company_info_summary": {"type": "string"},
                },
                "required": ["company_info_summary", "is_startup"],
            },
        },
    },
]

_SYSTEM = (
    "You are a research assistant finding HR and CEO contacts for internship cold emails.\n"
    "Follow this workflow:\n"
    "1. check_company_size — is it a startup (<~500 employees) or large enterprise? "
    "Does it have an India office?\n"
    "2. search_recruiters — find HR/Talent Acquisition. If company has India presence, "
    "set location='India'. Otherwise leave empty.\n"
    "3. find_email_address — get their email once you have a name.\n"
    "4. verify_person_designation — confirm via LinkedIn, company site, Crunchbase. "
    "Set recruiter_verified=True only if 2+ sources confirm.\n"
    "5. If is_startup=True: call find_ceo_or_founder, then find_email_address for CEO, "
    "then verify_person_designation for CEO.\n"
    "6. get_company_overview — for email personalization.\n"
    "7. extract_recruiter_details — submit everything.\n"
    "For large enterprises (TCS, Infosys, Wipro, Google, Microsoft, etc.) skip step 5.\n"
    "Always produce a company_info_summary."
)


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def _name_grounded(name: str, corpus: str) -> bool:
    """True if the person's name actually appears in the collected web evidence.

    Guards against the LLM inventing plausible-sounding people. Requires every
    significant token of the name (>=3 chars) to appear in the corpus.
    """
    if not name or not corpus:
        return False
    low = corpus.lower()
    tokens = [t for t in re.split(r"\s+", name.strip().lower()) if len(t) >= 3]
    if not tokens:
        return False
    return all(t in low for t in tokens)


def _run_tool(name: str, inputs: dict) -> str:
    from ..contacts.web_search import search_web, format_results
    from ..contacts.email_finder import find_email

    if name == "check_company_size":
        company = inputs["company"]
        results = search_web(f"{company} employees company size headcount", max_results=3)
        results += search_web(f"{company} site:crunchbase.com", max_results=2)
        return format_results(results)

    elif name == "search_recruiters":
        company = inputs["company"]
        location = inputs.get("location", "").strip()
        extra = inputs.get("query", "").strip()
        loc = f"{location} " if location else ""
        queries = [
            f"{company} {loc}HR recruiter talent acquisition internship{' ' + extra if extra else ''}",
            f'"{company}" {loc}recruiter site:linkedin.com',
        ]
        if location:
            queries.append(f"{company} {location} office HR contact careers")
        results = []
        for q in queries:
            results += search_web(q, max_results=3)
        return format_results(results, max_items=8)

    elif name == "get_company_overview":
        results = search_web(
            f"{inputs['company']} company overview products technology internship", max_results=4
        )
        return format_results(results)

    elif name == "find_email_address":
        result = find_email(
            first=inputs["first_name"],
            last=inputs["last_name"],
            company=inputs["company"],
        )
        return json.dumps(result)

    elif name == "verify_person_designation":
        from ..contacts.web_search import search_web, format_results
        person = inputs["name"]
        company = inputs["company"]
        title = inputs.get("title", "")
        results = search_web(f'"{person}" "{company}" site:linkedin.com', max_results=3)
        results += search_web(f'"{person}" {company} {title} -site:linkedin.com', max_results=3)
        web_text = format_results(results, max_items=6)
        mcp_result = call_linkedin_mcp("search_people", {"keywords": f"{person} {company}"})
        mcp_text = ""
        if mcp_result:
            sections = mcp_result.get("sections") or {}
            mcp_text = next(
                (sections[k] for k in ("search_results", "people", "results") if k in sections), ""
            )
        return web_text + ("\n\n[LinkedIn MCP]\n" + mcp_text[:500] if mcp_text else "")

    elif name == "find_ceo_or_founder":
        from ..contacts.web_search import search_web, format_results
        company = inputs["company"]
        results = search_web(f"{company} CEO founder name LinkedIn", max_results=3)
        results += search_web(f"{company} site:crunchbase.com founder CEO people", max_results=3)
        return format_results(results)

    elif name == "extract_recruiter_details":
        return json.dumps(inputs)

    return "Unknown tool"


def _log_tool(name: str, inputs: dict) -> None:
    msgs = {
        "check_company_size": lambda i: f"Checking if {i['company']} is a startup...",
        "search_recruiters": lambda i: f"Searching for HR contacts at {i['company']}" + (f" ({i['location']} office)" if i.get("location") else " (global)") + "...",
        "get_company_overview": lambda i: f"Reading about {i['company']}...",
        "find_email_address": lambda i: f"Finding email for {i['first_name']} {i['last_name']} at {i['company']}...",
        "verify_person_designation": lambda i: f"Verifying {i['name']} is {i.get('title', 'HR')} at {i['company']}...",
        "find_ceo_or_founder": lambda i: f"Searching for CEO/founder of {i['company']}...",
        "extract_recruiter_details": lambda i: "Compiling all contact details...",
    }
    fn = msgs.get(name)
    if fn:
        say(f"  {fn(inputs)}")


# ---------------------------------------------------------------------------
# LLM agent loop
# ---------------------------------------------------------------------------

def _parse_failed_generation(text: str):
    """Recover a tool call from Groq/llama's malformed ``<function=...>`` output.

    Handles a single JSON object as well as the array-of-objects form
    ``<function=foo [{"company": "X"}, {"location": "Y"}]</function>`` by merging
    the objects into one argument dict.
    """
    m = re.search(r"<function=(\w+)", text)
    if not m:
        return None, None
    fn_name = m.group(1)
    rest = text[m.end():]
    # Prefer a single well-formed object if it parses cleanly.
    single = re.search(r"(\{.*\})\s*</function>", rest, re.DOTALL)
    if single:
        try:
            return fn_name, json.loads(single.group(1))
        except json.JSONDecodeError:
            pass
    # Otherwise merge every flat {...} object found (array-style args).
    merged: dict = {}
    for obj in re.findall(r"\{[^{}]*\}", rest, re.DOTALL):
        try:
            parsed = json.loads(obj)
            if isinstance(parsed, dict):
                merged.update(parsed)
        except json.JSONDecodeError:
            continue
    if merged:
        return fn_name, merged
    return None, None


def _run_agent(company: str):
    """Run the tool-calling LLM loop.

    Returns ``(extracted, corpus)`` where ``corpus`` is the concatenation of all
    real search/LinkedIn evidence the agent saw — used afterwards to ground the
    names it proposes (so we can reject anything it invents).
    """
    import groq as groq_sdk
    from groq import Groq
    from config.settings import get_settings
    settings = get_settings()

    client = Groq(api_key=settings.groq_api_key)
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": f"Find recruiter and CEO contact info for: {company}"},
    ]
    extracted = {}
    corpus_parts: List[str] = []
    fake_tc_counter = 0

    for _ in range(10):
        try:
            response = client.chat.completions.create(
                model=settings.groq_model,
                max_tokens=512,
                messages=messages,
                tools=_TOOLS,
                tool_choice="auto",
            )
        except groq_sdk.BadRequestError as exc:
            body = exc.body or {}
            failed_text = (body.get("error") or {}).get("failed_generation", "")
            fn_name, args = _parse_failed_generation(failed_text)
            if fn_name and args:
                _logger.debug("Recovered malformed tool call: %s(%s)", fn_name, args)
                _log_tool(fn_name, args)
                result = _run_tool(fn_name, args)
                if fn_name not in ("extract_recruiter_details", "find_email_address"):
                    corpus_parts.append(result)
                if fn_name == "extract_recruiter_details":
                    extracted = args
                    break
                fake_tc_counter += 1
                fake_id = f"recovered_{fake_tc_counter}"
                messages.append({
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "id": fake_id,
                        "type": "function",
                        "function": {"name": fn_name, "arguments": json.dumps(args)},
                    }],
                })
                messages.append({
                    "role": "tool",
                    "tool_call_id": fake_id,
                    "content": result[:1500],
                })
                continue
            else:
                _logger.warning("Unrecoverable Groq tool_use_failed for %s: %s", company, exc)
                break
        except groq_sdk.APIError as exc:
            # Rate limit / server / connection errors: don't crash the harvest —
            # return whatever evidence we have and let verification/fallback run.
            _logger.warning("Groq API error for %s (%s); stopping agent early.",
                            company, type(exc).__name__)
            warn(f"  LLM unavailable for {company} ({type(exc).__name__}); "
                 "falling back to published company contacts.")
            break

        msg = response.choices[0].message
        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in (msg.tool_calls or [])
            ],
        })

        if response.choices[0].finish_reason == "stop" or not msg.tool_calls:
            break

        for tc in msg.tool_calls:
            name = tc.function.name
            args = json.loads(tc.function.arguments)
            _log_tool(name, args)
            result = _run_tool(name, args)
            if name not in ("extract_recruiter_details", "find_email_address"):
                corpus_parts.append(result)
            if name == "extract_recruiter_details":
                extracted = args
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result[:1500],
            })

        if extracted:
            break

    return extracted, "\n".join(corpus_parts)


# ---------------------------------------------------------------------------
# ContactFinder class
# ---------------------------------------------------------------------------

class ContactFinder:

    def __init__(self, db_session, settings):
        self.session = db_session
        self.settings = settings

    def find_contacts(self, company: str, is_startup: bool = False,
                      posting_country: str = "India") -> List[HRContact]:
        say(f"Finding HR contacts for {company}...")

        cached = self._load_from_db(company)
        if cached:
            say(f"Using {len(cached)} cached contact(s) for {company}.")
            return cached

        # Discover contacts via the web-search agent, grounding every name in
        # real evidence and only ever accepting verified email addresses.
        try:
            extracted, corpus = _run_agent(company)
        except Exception as exc:
            _logger.warning("Web search agent failed for %s: %s", company, exc)
            extracted, corpus = {}, ""
        contacts = self._extracted_to_contacts(extracted, company, corpus)

        # If we still have no contact with a *verified* email, try a real,
        # published role mailbox (careers@/hr@) so the email can still be sent.
        if not any(c.email and c.verified for c in contacts):
            self._append_generic_mailbox(contacts, company, extracted_domain=None)

        for c in contacts:
            self.session.add(c)
        self.session.commit()

        verified = [c for c in contacts if c.email and c.verified]
        if verified:
            for c in verified:
                contact_found(c.name or "Hiring Team", c.designation or "HR", company, True)
        else:
            warn(
                f"No VERIFIED email found for {company}. To avoid sending to a "
                "wrong or non-existent address, no recipient was set — check the "
                "job posting / company careers page manually."
            )

        return contacts

    def _append_generic_mailbox(self, contacts: List[HRContact], company: str,
                                extracted_domain) -> None:
        from ..contacts.email_finder import find_generic_company_email
        try:
            res = find_generic_company_email(company, domain=extracted_domain)
        except Exception as exc:
            _logger.debug("generic mailbox lookup failed: %s", exc)
            return
        if res.get("email") and res.get("status") == "verified":
            say(f"  Using published company mailbox {res['email']} (no named "
                "contact could be verified).")
            contacts.append(HRContact(
                company=company,
                name=None,
                designation="Hiring Team",
                email=res["email"],
                source="published_role_mailbox",
                verified=True,
                verification_evidence=res.get("evidence"),
                last_checked=datetime.utcnow(),
            ))

    def _extracted_to_contacts(self, data: dict, company: str,
                               corpus: str = "") -> List[HRContact]:
        """Build contacts, but ground every name in real evidence and recompute
        each email via verification. The LLM's own email value is ignored — it
        was the source of hallucinated addresses."""
        contacts = []

        for name_key, title_key, li_key, src, default_title in [
            ("recruiter_name", "recruiter_title", "recruiter_linkedin",
             "agent_web_search", "HR"),
            ("ceo_name", "ceo_title", "ceo_linkedin",
             "agent_web_search_founder", "CEO/Founder"),
        ]:
            name = (data.get(name_key) or "").strip()
            if not name:
                continue
            if not _name_grounded(name, corpus):
                _logger.warning(
                    "Dropping ungrounded %s '%s' for %s — not found in any search "
                    "evidence (likely hallucinated).", default_title, name, company)
                say(f"  Discarding unverified name '{name}' (no evidence found).")
                continue
            contact = self._verify_contact(
                name, data.get(title_key) or default_title,
                data.get(li_key), company, src)
            if contact:
                contacts.append(contact)

        return contacts

    def _verify_contact(self, name: str, title: str, linkedin: Optional[str],
                        company: str, source: str) -> Optional[HRContact]:
        from ..contacts.email_finder import find_email
        now = datetime.utcnow()
        parts = name.split()
        first = parts[0] if parts else ""
        last = parts[-1] if len(parts) > 1 else ""

        email = None
        verified = False
        evidence = "Name grounded in search evidence; "
        try:
            res = find_email(first, last, company)
            if res.get("status") == "verified" and res.get("email"):
                email = res["email"]
                verified = True
                evidence += f"email {res['source']}: {res.get('evidence', '')}"
            else:
                evidence += f"no verified email ({res.get('evidence', 'unverified')})"
        except Exception as exc:
            _logger.debug("find_email failed for %s: %s", name, exc)
            evidence += "email lookup error"

        return HRContact(
            company=company,
            name=name,
            designation=title or None,
            email=email,            # only ever a verified address, else None
            profile_url=linkedin or None,
            source=source,
            verified=verified,
            verification_evidence=evidence,
            last_checked=now,
        )

    def _load_from_db(self, company: str) -> List[HRContact]:
        from sqlmodel import select
        stmt = select(HRContact).where(HRContact.company == company)
        rows = self.session.exec(stmt).all()
        return [r for r in rows
                if r.last_checked and (datetime.utcnow() - r.last_checked).days < 7]
