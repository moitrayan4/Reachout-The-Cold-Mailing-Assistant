"""ProfileManager — resume parsing plus a background file watcher."""

from __future__ import annotations
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, List

from langchain_core.messages import HumanMessage, SystemMessage
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from ..narration.narrator import say, warn
from ..llm.groq_client import get_llm

_logger = logging.getLogger("assistant.profile")


# ---------------------------------------------------------------------------
# Profile dataclass
# ---------------------------------------------------------------------------

@dataclass
class OwnerProfile:
    full_name: str = ""
    contact: str = ""
    graduation_year: int = 2028
    batch: str = "2027-28"
    current_year: str = "3rd year"
    skills: List[str] = None
    domains: List[str] = None      # e.g. ["AI/ML", "Web", "Data"]
    projects: List[str] = None
    experience: List[str] = None
    preferred_roles: List[str] = None
    keywords_for_search: List[str] = None
    resume_path: str = ""

    def __post_init__(self):
        for f in ["skills", "domains", "projects", "experience", "preferred_roles", "keywords_for_search"]:
            if getattr(self, f) is None:
                setattr(self, f, [])


# ---------------------------------------------------------------------------
# Text extraction helpers
# ---------------------------------------------------------------------------

def _extract_text_pdf(path: Path) -> str:
    import pdfplumber
    text = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            t = page.extract_text()
            if t:
                text.append(t)
    return "\n".join(text)


def _extract_text_docx(path: Path) -> str:
    from docx import Document
    doc = Document(path)
    return "\n".join(p.text for p in doc.paragraphs)


def _file_hash(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


# ---------------------------------------------------------------------------
# LLM structuring prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """You are a resume parser. Extract structured information from the resume text below.
Return ONLY a valid JSON object with these fields:
{
  "full_name": "string",
  "contact": "email or phone",
  "graduation_year": integer (e.g. 2028),
  "batch": "e.g. 2027-28 or 2028",
  "current_year": "e.g. 3rd year or pre-final",
  "skills": ["skill1", "skill2", ...],
  "domains": ["AI/ML", "Web", "Data Science", ...],
  "projects": ["Project Name: brief description", ...],
  "experience": ["Role at Company: description", ...],
  "preferred_roles": ["Software Engineer Intern", "ML Engineer Intern", ...],
  "keywords_for_search": ["python", "machine learning", "internship", ...]
}
Return ONLY the JSON, no explanation."""


def _parse_resume_with_llm(text: str) -> dict:
    llm = get_llm()
    msgs = [
        SystemMessage(content=_SYSTEM_PROMPT),
        HumanMessage(content=f"Resume text:\n\n{text[:8000]}"),
    ]
    resp = llm.invoke(msgs)
    raw = resp.content.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        _logger.warning("LLM returned invalid JSON for profile; using empty profile")
        return {}


# ---------------------------------------------------------------------------
# ProfileManager
# ---------------------------------------------------------------------------

class ProfileManager:
    def __init__(self, resume_dir: Path, db_session, settings):
        self.resume_dir = resume_dir
        self.session = db_session
        self.settings = settings
        self._profile: Optional[OwnerProfile] = None
        self._observer: Optional[Observer] = None

    # --- Public API --------------------------------------------------------

    def load_or_parse(self) -> OwnerProfile:
        """Load from DB if the resume hash is unchanged, otherwise re-parse."""
        resume_path = self._find_resume()
        if resume_path is None:
            warn("No resume (.pdf/.docx) found in the project folder. Some features will be limited.")
            return OwnerProfile(
                full_name=self.settings.owner_name,
                graduation_year=self.settings.owner_graduation_year,
                batch=self.settings.owner_batch,
            )

        current_hash = _file_hash(resume_path)
        from ..storage.models import Profile as ProfileModel
        from sqlmodel import select
        stmt = select(ProfileModel).where(ProfileModel.resume_hash == current_hash)
        row = self.session.exec(stmt).first()

        if row:
            _logger.info("Resume unchanged; using cached profile.")
            data = row.get_parsed()
        else:
            say(f"Reading your resume: {resume_path.name}...")
            text = self._extract_text(resume_path)
            data = _parse_resume_with_llm(text)
            self._save_to_db(current_hash, data)
            say("Resume processed — I've updated your skill profile.")

        profile = self._build_profile(data, resume_path)
        self._profile = profile
        self._validate(profile)
        return profile

    def get_profile(self) -> Optional[OwnerProfile]:
        return self._profile

    def start_watcher(self) -> None:
        """Start a background thread watching the root folder for resume changes."""
        handler = _ResumeChangeHandler(self)
        self._observer = Observer()
        self._observer.schedule(handler, str(self.resume_dir), recursive=False)
        self._observer.start()
        _logger.info("File watcher started on %s", self.resume_dir)

    def stop_watcher(self) -> None:
        if self._observer:
            self._observer.stop()
            self._observer.join()

    # --- Private helpers ---------------------------------------------------

    def _find_resume(self) -> Optional[Path]:
        candidates = list(self.resume_dir.glob("*.pdf")) + list(self.resume_dir.glob("*.docx"))
        # Exclude project files that look like resumes but aren't
        candidates = [p for p in candidates if "plan" not in p.name.lower()]
        if not candidates:
            return None
        return max(candidates, key=lambda p: p.stat().st_mtime)

    def _extract_text(self, path: Path) -> str:
        if path.suffix.lower() == ".pdf":
            return _extract_text_pdf(path)
        return _extract_text_docx(path)

    def _build_profile(self, data: dict, path: Path) -> OwnerProfile:
        # Layer manual dashboard edits on top of the parsed skills, so anything
        # the resume parser missed (or got wrong) is reflected everywhere the
        # profile is used — scoring and email drafting included.
        from ..skills import merge_skills
        skills = merge_skills(data.get("skills", []), self.settings)
        return OwnerProfile(
            full_name=data.get("full_name", self.settings.owner_name),
            contact=data.get("contact", ""),
            graduation_year=int(data.get("graduation_year", self.settings.owner_graduation_year)),
            batch=data.get("batch", self.settings.owner_batch),
            current_year=data.get("current_year", "3rd year"),
            skills=skills,
            domains=data.get("domains", []),
            projects=data.get("projects", []),
            experience=data.get("experience", []),
            preferred_roles=data.get("preferred_roles", []),
            keywords_for_search=data.get("keywords_for_search", []),
            resume_path=str(path),
        )

    def _save_to_db(self, resume_hash: str, data: dict) -> None:
        from ..storage.models import Profile as ProfileModel
        from datetime import datetime
        from sqlmodel import select
        stmt = select(ProfileModel)
        existing = self.session.exec(stmt).all()
        for row in existing:
            self.session.delete(row)
        row = ProfileModel(resume_hash=resume_hash, parsed_json=json.dumps(data), updated_at=datetime.utcnow())
        self.session.add(row)
        self.session.commit()

    def _validate(self, profile: OwnerProfile) -> None:
        if profile.graduation_year != 2028:
            warn(
                f"Your resume says graduation year {profile.graduation_year}, "
                "but the system is configured for the 2028 batch. "
                "Please check the resume or update the settings."
            )


class _ResumeChangeHandler(FileSystemEventHandler):
    def __init__(self, manager: ProfileManager):
        self._mgr = manager

    def on_modified(self, event):
        if not event.is_directory and Path(event.src_path).suffix.lower() in {".pdf", ".docx"}:
            say("Detected resume update — re-reading your profile...")
            time.sleep(1)  # brief wait for file write to complete
            self._mgr.load_or_parse()

    def on_created(self, event):
        self.on_modified(event)
