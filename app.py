"""Reachout: The Cold Mailing Assistant — dynamic control panel.

Run with:
    .venv/Scripts/streamlit run app.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

# Make the project root importable (config.*, src.*) regardless of CWD.
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ui import service, styles  # noqa: E402

st.set_page_config(
    page_title="Reachout: The Cold Mailing Assistant",
    page_icon="✉️",
    layout="wide",
    initial_sidebar_state="collapsed",
)
styles.inject()


# ---------------------------------------------------------------------------
# Settings guard — the whole app needs at least a valid config / GROQ key
# ---------------------------------------------------------------------------

_boot = st.empty()
_boot.markdown(
    "<div class='boot-overlay'>"
    "<div class='boot-spinner'></div>"
    "<div class='boot-text'>Cooking…</div>"
    "</div>",
    unsafe_allow_html=True,
)
try:
    settings = service.get_settings()
    _boot.empty()
except Exception as exc:  # noqa: BLE001
    _boot.empty()
    st.error(
        "Could not load settings. Make sure `.env` exists with a valid "
        f"`GROQ_API_KEY`.\n\n**Details:** {exc}"
    )
    st.stop()


def _df(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows) if rows else pd.DataFrame()


# Full-width API differs by Streamlit version: >=1.49 wants width="stretch",
# older versions (e.g. 1.43) only accept use_container_width=True and crash on
# the string. Pick per-version, with a runtime guard in case the cutoff is off.
_v = tuple(int(p) for p in (st.__version__.split(".") + ["0", "0"])[:2])
_NEW_WIDTH = _v >= (1, 49)


def _wkw() -> dict:
    return {"width": "stretch"} if _NEW_WIDTH else {"use_container_width": True}


def _show_df(data) -> None:
    global _NEW_WIDTH
    try:
        st.dataframe(data, hide_index=True, **_wkw())
    except TypeError:
        _NEW_WIDTH = not _NEW_WIDTH
        st.dataframe(data, hide_index=True, **_wkw())


def _wbtn(container, label: str, **kw) -> bool:
    global _NEW_WIDTH
    try:
        return container.button(label, **_wkw(), **kw)
    except TypeError:
        _NEW_WIDTH = not _NEW_WIDTH
        return container.button(label, **_wkw(), **kw)


# Auto-refresh interval (seconds). 0 = off. Drives every live fragment.
_refresh = st.session_state.get("refresh_secs", 10)
_run_every = f"{_refresh}s" if _refresh else None


# ---------------------------------------------------------------------------
# Hero
# ---------------------------------------------------------------------------

_gmail_ready, _gmail_method = service.gmail_status()
styles.hero(
    "Reachout: The Cold Mailing Assistant",
    "Internship hunting and outreach, on autopilot",
    [
        ("Live" if _run_every else "Live off", bool(_run_every)),
    ],
)


# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------

(tab_dash, tab_review, tab_runs, tab_contacts, tab_drafts,
 tab_roles, tab_companies, tab_profile) = st.tabs(
    ["Dashboard", "Review", "Runs", "Contacts", "Drafts", "Roles", "Companies", "Profile"]
)


# ----------------------------- Dashboard -----------------------------------

with tab_dash:
    _ctrl, _spacer = st.columns([1, 2])
    with _ctrl:
        interval = st.select_slider(
            "Live refresh",
            options=[0, 5, 10, 30, 60],
            value=_refresh,
            format_func=lambda v: "Off" if v == 0 else f"{v}s",
            help="How often the dashboard and counters re-query the database.",
        )
        if interval != _refresh:
            st.session_state["refresh_secs"] = interval
            st.rerun()

    styles.section("Pipeline at a glance")

    @st.fragment(run_every=_run_every)
    def _dashboard():
        counts = service.opportunity_counts()
        estats = service.email_stats()
        styles.metric_cards([
            ("Pending", counts.get("pending_review", 0), ""),
            ("Presented", counts.get("presented", 0), ""),
            ("Drafted", counts.get("drafted", 0), "warm"),
            ("Sent", counts.get("sent", 0), "green"),
        ])
        styles.metric_cards([
            ("Approved", counts.get("approved", 0), ""),
            ("Stored", counts.get("skipped_stored", 0), ""),
            ("Replied", counts.get("replied", 0), "green"),
            ("Emails total", estats.get("total", 0), "warm"),
        ])

        st.write("")
        left, right = st.columns(2)
        with left:
            styles.section("Site health")
            health = service.site_health()
            if health:
                _show_df(_df(health))
            else:
                st.info("No site-health records yet — run a harvest first.")
        with right:
            styles.section("Recent activity")
            actions = service.recent_actions(limit=20)
            if actions:
                _show_df(_df(actions))
            else:
                st.info("No actions logged yet.")
        if _run_every:
            st.caption(f"Auto-refreshing every {_refresh}s.")

    _dashboard()


# ------------------------------- Review ------------------------------------

with tab_review:
    styles.section("Opportunities awaiting your decision")
    st.caption(
        "Approve finds HR contacts, drafts a cold email and queues it. "
        "Store keeps it for later. Forget hides it permanently."
    )

    opps = service.reviewable_opportunities()
    if not opps:
        st.success("You're all caught up — nothing to review. Run a harvest from the Runs tab.")
    else:
        n_priority = sum(1 for o in opps if o.get("priority"))
        st.markdown(f"<span class='chip'>{len(opps)} ready</span>", unsafe_allow_html=True)
        if n_priority:
            st.warning(
                f"⭐ {n_priority} PRIORITY opportunity(s) — 2028-batch internships at your "
                "target companies. These are pinned to the top and shown no matter what."
            )
        for opp in opps:
            fp = opp["fingerprint"]
            with st.expander(styles.opportunity_header(opp)):
                meta = st.columns(3)
                loc = opp.get("location") or "N/A"
                if opp.get("remote"):
                    loc += " (Remote)"
                meta[0].markdown(f"**Location**\n\n{loc}")
                meta[1].markdown(f"**Stipend**\n\n{opp.get('stipend_label', 'not stated')}")
                tags = []
                if opp.get("priority"):
                    tags.append("⭐ PRIORITY")
                if opp.get("batch_2028"):
                    tags.append("2028 batch")
                if opp.get("is_target_company") and not opp.get("priority"):
                    tags.append("target company")
                if opp.get("is_startup"):
                    tags.append("startup")
                if opp.get("ppo_flag"):
                    tags.append("PPO")
                if opp.get("fte_flag"):
                    tags.append("FTE")
                meta[2].markdown(f"**Flags**\n\n{', '.join(tags) or '—'}")
                if opp.get("company_category"):
                    st.caption(f"Category: {opp['company_category']}")

                explanation = opp.get("match_explanation") or []
                if explanation:
                    st.markdown("**Why it fits**")
                    for item in explanation:
                        st.markdown(f"- {item}")

                for url in (opp.get("source_urls") or []):
                    st.markdown(f"[{url}]({url})")

                st.divider()
                b = st.columns(3)
                if b[0].button("Approve & draft", key=f"approve_{fp}", type="primary"):
                    with st.spinner("Finding HR contacts and drafting the email…"):
                        try:
                            res = service.approve_opportunity(fp)
                        except Exception as exc:  # noqa: BLE001
                            st.error(f"Failed: {exc}")
                            res = None
                    if res:
                        if res["trust_verdict"] == "low_trust":
                            st.warning(
                                "Low-trust startup signals: "
                                + "; ".join(res["trust_reasons"][:2])
                            )
                        if res["needs_manual_recipient"]:
                            st.warning(
                                "No verified recipient email found — draft queued without a "
                                "To: address. Use push-drafts (Runs tab) or add it in Gmail."
                            )
                        else:
                            st.success(f"Draft queued for: {', '.join(res['to_addrs'])}")
                        if res.get("llm_fallback"):
                            st.info(
                                "Groq was unreachable, so this is a template draft. "
                                "Review and personalize it before sending."
                            )
                        st.markdown(f"**Subject:** {res['subject']}")
                        st.text_area("Draft body", res["body"], height=240, key=f"body_{fp}")
                        if res["contacts"]:
                            st.caption("Contacts found")
                            _show_df(_df(res["contacts"]))

                if b[1].button("Store for later", key=f"store_{fp}"):
                    service.store_opportunity(fp)
                    st.toast(f"Stored {opp['company']}")
                    st.rerun()

                if b[2].button("Forget", key=f"forget_{fp}"):
                    service.forget_opportunity(fp)
                    st.toast(f"Forgot {opp['company']}")
                    st.rerun()


# -------------------------------- Runs -------------------------------------

with tab_runs:
    styles.section("Run a job")
    st.caption(
        "These launch the same commands as the CLI and stream their live "
        "narration below. Harvest opens real browsers and can take a while."
    )

    if not _gmail_ready:
        st.warning(
            f"Gmail not connected ({_gmail_method}). Run `python -m src gmail-auth` "
            "once to authorize draft creation via the Gmail API (one-time browser "
            "sign-in). Needs `gmail_credentials.json` in the project root."
        )

    job = st.columns(4)
    cmd = None
    if _wbtn(job[0], "Harvest", type="primary"):
        cmd = ["harvest"]
    if _wbtn(job[1], "Watch companies"):
        cmd = ["watch-companies"]
    if _wbtn(job[2], "Watch replies"):
        cmd = ["watch"]
    if _wbtn(job[3], "Push drafts"):
        cmd = ["push-drafts"]

    if cmd:
        st.markdown(f"**Running:** `python -m src {' '.join(cmd)}`")
        placeholder = st.empty()
        lines: list[str] = []
        for line in service.stream_command(cmd):
            lines.append(line)
            placeholder.code("\n".join(lines[-500:]), language="text")
        st.toast("Job finished")


# ------------------------------ Contacts -----------------------------------

with tab_contacts:
    styles.section("HR / recruiter contacts")

    @st.fragment(run_every=_run_every)
    def _contacts():
        contacts = service.list_contacts()
        if contacts:
            only_verified = st.checkbox("Show only verified emails", value=False)
            if only_verified:
                contacts = [c for c in contacts if c.get("verified")]
            _show_df(_df(contacts))
        else:
            st.info("No contacts discovered yet — approve an opportunity in the Review tab.")

    _contacts()


# ------------------------------- Drafts ------------------------------------

with tab_drafts:
    styles.section("Email drafts & sent")

    @st.fragment(run_every=_run_every)
    def _drafts():
        estats = service.email_stats()
        styles.metric_cards([
            ("Total", estats["total"], ""),
            ("Draft", estats["draft"], "warm"),
            ("Sent", estats["sent"], "green"),
            ("Replied", estats["replied"], "green"),
        ])
        st.write("")
        emails = service.list_emails()
        if emails:
            _show_df(_df(emails))
        else:
            st.info("No drafts yet.")

    _drafts()
    st.caption(
        "Drafts without a verified recipient stay queued. Run Push drafts "
        "in the Runs tab to enrich them and create Gmail drafts."
    )


# ------------------------------- Roles -------------------------------------

with tab_roles:
    styles.section("Target internship roles")
    st.caption("Leave empty to let harvest use keywords from your resume.")

    roles = service.get_roles()
    if roles:
        for r in roles:
            row = st.columns([6, 1])
            row[0].markdown(f"<span class='chip'>{r}</span>", unsafe_allow_html=True)
            if row[1].button("Remove", key=f"rmrole_{r}"):
                service.remove_role(r)
                st.rerun()
    else:
        st.info("No custom roles set — using resume keywords.")

    with st.form("add_role_form", clear_on_submit=True):
        new_role = st.text_input("Add a role", placeholder="e.g. ML Engineer")
        if st.form_submit_button("Add") and new_role.strip():
            service.add_role(new_role.strip())
            st.rerun()

    if roles and st.button("Clear all roles"):
        service.clear_roles()
        st.rerun()


# ------------------------------ Companies ----------------------------------

with tab_companies:
    watch = service.get_company_watch()
    styles.section(f"Target companies watched for {watch['grad_year']}-batch internships")
    st.caption(
        "Their India career sites are scanned every harvest (and via the "
        "**Watch companies** button in Runs). Any internship open to the "
        f"{watch['grad_year']} batch is flagged ⭐ PRIORITY and surfaced in Review "
        "no matter what — it bypasses the stipend and recency filters."
    )

    comps = watch["companies"]
    st.markdown(
        f"<span class='chip'>{len(comps)} companies</span> "
        f"<span class='chip'>India offices</span> "
        f"<span class='chip'>batch {watch['grad_year']}</span>",
        unsafe_allow_html=True,
    )

    with st.form("add_company_form", clear_on_submit=True):
        c = st.columns([4, 2])
        new_company = c[0].text_input("Add a company", placeholder="e.g. Stripe")
        new_cat = c[1].text_input("Category", placeholder="e.g. Fintech")
        if st.form_submit_button("Add") and new_company.strip():
            service.add_company(new_company.strip(), new_cat.strip() or "Custom")
            st.rerun()

    # Group by category.
    by_cat: dict[str, list] = {}
    for cmp in comps:
        by_cat.setdefault(cmp.get("category") or "Other", []).append(cmp)

    for cat in sorted(by_cat):
        with st.expander(f"{cat}  ·  {len(by_cat[cat])}"):
            for cmp in sorted(by_cat[cat], key=lambda x: x["name"].lower()):
                row = st.columns([5, 1])
                url = cmp.get("careers_url")
                label = f"**{cmp['name']}**"
                if url:
                    label += f" · [careers]({url})"
                elif cmp.get("domain"):
                    label += f" · {cmp['domain']}"
                row[0].markdown(label)
                if row[1].button("Remove", key=f"rmco_{cmp['name']}"):
                    service.remove_company(cmp["name"])
                    st.rerun()


# ------------------------------ Profile ------------------------------------

with tab_profile:
    styles.section("Your resume profile")
    prof = service.profile_summary()
    if not prof:
        st.warning(
            "No resume parsed yet. Drop a `.pdf` or `.docx` resume in the project "
            "root and refresh."
        )
    else:
        styles.metric_cards([
            ("Name", prof["full_name"], ""),
            ("Batch", prof["batch"], ""),
            ("Graduation", prof["graduation_year"], "warm"),
            ("Year", prof["current_year"], "green"),
        ])
        st.write("")
        st.markdown(f"**Resume:** `{prof['resume_path']}`")

        st.markdown("**Skills**")
        st.caption(
            "Pulled from your resume. Add anything the parser missed — manually "
            "added skills feed match-scoring and the email draft exactly like "
            "parsed ones do."
        )
        manual = service.manual_skill_set()
        if prof["skills"]:
            for s in prof["skills"]:
                row = st.columns([6, 1])
                badge = " <span class='chip'>added</span>" if s.lower() in manual else ""
                row[0].markdown(f"<span class='chip'>{s}</span>{badge}", unsafe_allow_html=True)
                if row[1].button("Remove", key=f"rmskill_{s}"):
                    service.remove_skill(s)
                    st.rerun()
        else:
            st.info("No skills detected yet — add some below or drop a richer resume.")

        with st.form("add_skill_form", clear_on_submit=True):
            new_skill = st.text_input("Add a skill", placeholder="e.g. Kubernetes")
            if st.form_submit_button("Add") and new_skill.strip():
                service.add_skill(new_skill.strip())
                st.toast(f"Added skill: {new_skill.strip()}")
                st.rerun()
        if prof["domains"]:
            st.markdown("**Domains**")
            st.markdown(" ".join(f"<span class='chip'>{d}</span>" for d in prof["domains"]),
                        unsafe_allow_html=True)
        if prof["preferred_roles"]:
            st.markdown("**Preferred roles**: " + ", ".join(prof["preferred_roles"]))
        if prof["keywords_for_search"]:
            st.markdown("**Search keywords**: " + ", ".join(prof["keywords_for_search"]))
        if prof["projects"]:
            st.markdown("**Projects**")
            for p in prof["projects"]:
                st.markdown(f"- {p}")
