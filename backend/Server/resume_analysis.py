"""Resume review, comparison, and an updated file in the uploaded format.

Gemini 2.5 Flash writes the review. The result replaces one Firestore
document, users/{uid}/resumeAnalysis/current, and is reused when the
same resume text is analyzed again.
"""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from datetime import datetime, timezone
from io import BytesIO
from xml.sax.saxutils import escape

from Server.firebase import get_firestore_client

MODEL = "gemini-3.8-flash"
# Free-tier Flash models, newest first. A busy or retired model is skipped.
_FALLBACK_MODELS = (
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.1-flash-lite",
    "gemini-flash-latest",
    "gemini-2.5-flash",
)
_working_model = ""
_MAX_TEXT = 24_000

# Tests set this to a dict. The API leaves it empty and uses Firestore.
_analysis_memory: dict | None = None

_CHECKLIST = (
    "Contact details",
    "Summary",
    "Experience",
    "Skills",
    "Education",
    "Projects",
    "Keywords for applications",
    "Length and formatting",
)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _clean_text(text: str) -> str:
    cleaned = re.sub(r"[ \t]+\n", "\n", str(text or ""))
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()[:_MAX_TEXT]


def _collection(uid: str):
    return (
        get_firestore_client()
        .collection("users")
        .document(uid)
        .collection("resumeAnalysis")
    )


def _document(uid: str):
    return _collection(uid).document("resume-analysis")


def _read(uid: str) -> dict:
    if _analysis_memory is not None:
        stored = _analysis_memory.get(uid) or {}
        return dict(stored)
    snapshot = _document(uid).get()
    if not snapshot.exists:
        legacy = _collection(uid).document("current").get()
        if not legacy.exists:
            return {}
        payload = legacy.to_dict() or {}
        if isinstance(payload, dict):
            _document(uid).set(payload)
        legacy.reference.delete()
        return payload if isinstance(payload, dict) else {}
    payload = snapshot.to_dict() or {}
    return payload if isinstance(payload, dict) else {}


def _write(uid: str, payload: dict) -> None:
    if _analysis_memory is not None:
        _analysis_memory[uid] = dict(payload)
        return
    _document(uid).set(payload)


def remember_resume(uid: str, text: str, filename: str, file_type: str) -> None:
    """Keep the extracted text for this user. A new resume replaces the old review."""
    uid = str(uid or "").strip()
    source = _clean_text(text)
    if not uid or len(source) < 20:
        return
    digest = _hash(source)
    current = _read(uid)
    file_format = str(file_type or "PDF").upper()
    if current.get("sourceHash") == digest:
        current["filename"] = filename
        current["format"] = file_format
        current["fullText"] = True
        _write(uid, current)
        return
    previous = current.get("analysis") if isinstance(current.get("analysis"), dict) else current.get("previousAnalysis")
    _write(uid, {
        "sourceText": source,
        "sourceHash": digest,
        "filename": filename,
        "format": file_format,
        "fullText": True,
        "model": MODEL,
        "analyzedAt": "",
        "analysis": None,
        "previousAnalysis": previous if isinstance(previous, dict) else None,
        "improvement": None,
        "comparison": None,
        "rewrite": None,
    })


def _composed_text(profile: dict) -> str:
    lines = []
    for label, field in (
        ("Name", "fullName"),
        ("Email", "email"),
        ("Phone", "phone"),
        ("Headline", "headline"),
        ("Title", "currentTitle"),
        ("Company", "currentCompany"),
        ("Experience", "totalExperience"),
        ("Summary", "summary"),
        ("Education", "education"),
        ("LinkedIn", "linkedin"),
        ("GitHub", "github"),
        ("Portfolio", "portfolio"),
    ):
        value = str(profile.get(field) or "").strip()
        if value:
            lines.append(f"{label}: {value}")
    skills = [str(item).strip() for item in profile.get("skills") or [] if str(item).strip()]
    if skills:
        lines.append("Skills: " + ", ".join(skills))
    roles = [str(item).strip() for item in profile.get("targetRoles") or [] if str(item).strip()]
    if roles:
        lines.append("Target roles: " + ", ".join(roles))
    for job in profile.get("experienceHistory") or []:
        if not isinstance(job, dict):
            continue
        heading = " | ".join(
            part for part in (job.get("title"), job.get("company"), job.get("startDate"), job.get("endDate"), job.get("location"))
            if str(part or "").strip()
        )
        if heading:
            lines.append(heading)
        detail = str(job.get("description") or "").strip()
        if detail:
            lines.append(detail)
    return _clean_text("\n".join(lines))


def _source(uid: str) -> tuple[dict, dict]:
    from Server.server import read_profile

    profile = read_profile(uid)
    stored = _read(uid)
    resume = profile.get("resume") if isinstance(profile.get("resume"), dict) else {}
    text = _clean_text(stored.get("sourceText") or "")
    full_text = bool(stored.get("fullText")) and len(text) >= 20
    if len(text) < 20:
        text = _composed_text(profile)
        full_text = False
    if len(text) < 20:
        raise ValueError("Upload a resume on Skills & Profile first.")
    meta = {
        "filename": stored.get("filename") or resume.get("filename") or "Resume",
        "format": str(stored.get("format") or resume.get("type") or "PDF").upper(),
        "uploadedAt": resume.get("uploadedAt") or "",
        "fullText": full_text,
        "sourceHash": _hash(text),
        "sourceText": text,
    }
    return stored, meta


def _parse_json(text: str) -> dict:
    raw = str(text or "").strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        raw = raw[start:end + 1]
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("The review could not be read. Try again.") from exc
    if not isinstance(data, dict):
        raise ValueError("The review could not be read. Try again.")
    return data


def _strings(value, limit: int = 8) -> list[str]:
    if not isinstance(value, list):
        return []
    items = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in items:
            items.append(text[:400])
        if len(items) >= limit:
            break
    return items


def _pairs(value, limit: int = 8) -> list[dict]:
    if not isinstance(value, list):
        return []
    items = []
    for item in value:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()[:80]
        detail = str(item.get("detail") or "").strip()[:500]
        if title and detail:
            items.append({"title": title, "detail": detail})
        if len(items) >= limit:
            break
    return items


def _checklist(value) -> list[dict]:
    found = {}
    if isinstance(value, list):
        for item in value:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            status = str(item.get("status") or "").strip().lower()
            if status not in {"good", "weak", "missing"}:
                status = "weak"
            detail = str(item.get("detail") or "").strip()[:500]
            if title:
                found[title.casefold()] = {"title": title, "status": status, "detail": detail}
    items = []
    for title in _CHECKLIST:
        items.append(found.get(title.casefold()) or {
            "title": title,
            "status": "weak",
            "detail": "Not covered in this review.",
        })
    return items


def _analysis(raw: dict) -> dict:
    try:
        score = int(raw.get("score") or 0)
    except (TypeError, ValueError):
        score = 0
    return {
        "score": max(0, min(100, score)),
        "headline": str(raw.get("headline") or "").strip()[:240],
        "strengths": _strings(raw.get("strengths")),
        "improvements": _pairs(raw.get("improvements")),
        "checklist": _checklist(raw.get("checklist")),
        "skills": _strings(raw.get("skills"), 24),
    }


def _comparison(raw: dict, other_name: str, other_hash: str) -> dict:
    return {
        "otherName": other_name,
        "otherHash": other_hash,
        "summary": str(raw.get("summary") or "").strip()[:600],
        "goodInYours": _strings(raw.get("goodInYours")),
        "goodInTheirs": _strings(raw.get("goodInTheirs")),
        "improveYours": _pairs(raw.get("improveYours")),
        "doNotCopy": _strings(raw.get("doNotCopy"), 4),
    }


def _rewrite(raw: dict) -> dict:
    experience = []
    for item in raw.get("experience") or []:
        if not isinstance(item, dict):
            continue
        heading = str(item.get("heading") or "").strip()[:160]
        bullets = _strings(item.get("bullets"), 6)
        if heading or bullets:
            experience.append({
                "heading": heading,
                "meta": str(item.get("meta") or "").strip()[:160],
                "bullets": bullets,
            })
        if len(experience) >= 8:
            break
    education = []
    projects = []
    for key, bucket in (("education", education), ("projects", projects)):
        for item in raw.get(key) or []:
            if not isinstance(item, dict):
                continue
            heading = str(item.get("heading") or "").strip()[:160]
            detail = str(item.get("detail") or "").strip()[:400]
            if heading or detail:
                bucket.append({"heading": heading, "detail": detail})
            if len(bucket) >= 6:
                break
    return {
        "name": str(raw.get("name") or "").strip()[:120],
        "headline": str(raw.get("headline") or "").strip()[:160],
        "contact": str(raw.get("contact") or "").strip()[:240],
        "summary": str(raw.get("summary") or "").strip()[:1200],
        "skills": _strings(raw.get("skills"), 24),
        "experience": experience,
        "education": education,
        "projects": projects,
    }


def _can_switch(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(hint in message for hint in (
        "404", "not_found", "no longer available", "not found", "not supported",
        "503", "unavailable", "429", "resource_exhausted", "high demand",
        "overloaded", "try again later",
    ))


def _ordered(names) -> list[str]:
    seen = set()
    ordered = []
    for name in names:
        cleaned = str(name or "").strip().removeprefix("models/")
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            ordered.append(cleaned)
    return ordered


def _listed_flash_models(client) -> list[str]:
    try:
        listed = client.models.list()
    except Exception:
        return []
    names = []
    for item in listed or []:
        name = str(getattr(item, "name", "") or "")
        lowered = name.lower()
        if "flash" not in lowered:
            continue
        if any(word in lowered for word in ("image", "tts", "live", "embedding", "audio")):
            continue
        names.append(name)
    return _ordered(names)


def _generate(client, prompt: str) -> dict:
    global _working_model
    from Server.server import event

    last_error = None
    tried = set()
    for model in _ordered([_working_model, *_FALLBACK_MODELS]):
        tried.add(model)
        try:
            response = client.models.generate_content(model=model, contents=prompt)
        except Exception as exc:
            if not _can_switch(exc):
                raise
            last_error = exc
            event("resume", "warning", f"gemini model skipped {model}")
            continue
        _working_model = model
        return _parse_json(getattr(response, "text", "") or "")
    for model in _listed_flash_models(client):
        if model in tried:
            continue
        try:
            response = client.models.generate_content(model=model, contents=prompt)
        except Exception as exc:
            if not _can_switch(exc):
                raise
            last_error = exc
            event("resume", "warning", f"gemini model skipped {model}")
            continue
        _working_model = model
        return _parse_json(getattr(response, "text", "") or "")
    raise ValueError("Every free Gemini model is busy right now. Try again in a minute.") from last_error


def _rules() -> str:
    return (
        "Use only facts that appear in the resume text. "
        "Do not invent employers, dates, degrees, projects, or skills. "
        "Reply with one JSON object and no markdown."
    )


def _view(stored: dict, meta: dict, cached: bool) -> dict:
    analysis = stored.get("analysis") if isinstance(stored.get("analysis"), dict) else None
    comparison = stored.get("comparison") if isinstance(stored.get("comparison"), dict) else None
    rewrite = stored.get("rewrite") if isinstance(stored.get("rewrite"), dict) else None
    if comparison and comparison.get("sourceHash") != meta["sourceHash"]:
        comparison = None
    if rewrite and stored.get("rewriteHash") != meta["sourceHash"]:
        rewrite = None
    return {
        "model": MODEL,
        "cached": cached,
        "analyzedAt": stored.get("analyzedAt") or "",
        "resume": {
            "filename": meta["filename"],
            "format": meta["format"],
            "uploadedAt": meta["uploadedAt"],
            "fullText": meta["fullText"],
        },
        "analysis": analysis,
        "comparison": comparison,
        "improvement": stored.get("improvement") if isinstance(stored.get("improvement"), dict) else None,
        "rewriteReady": bool(rewrite and rewrite.get("text")),
    }


def analysis_view(uid: str) -> dict:
    uid = str(uid or "").strip()
    if not uid:
        raise ValueError("sign in is required")
    try:
        stored, meta = _source(uid)
    except ValueError:
        return {
            "model": MODEL,
            "cached": False,
            "analyzedAt": "",
            "resume": None,
            "analysis": None,
            "comparison": None,
            "rewriteReady": False,
        }
    cached = bool(stored.get("analysis")) and stored.get("sourceHash") == meta["sourceHash"]
    return _view(stored, meta, cached)


def _improvement(previous: dict | None, current: dict) -> dict | None:
    if not isinstance(previous, dict):
        return None
    try:
        old_score = int(previous.get("score"))
        new_score = int(current.get("score"))
    except (TypeError, ValueError):
        return None
    before = {
        item.get("title"): item.get("status")
        for item in previous.get("checklist") or []
        if isinstance(item, dict)
    }
    changes = []
    for item in current.get("checklist") or []:
        if not isinstance(item, dict):
            continue
        title = item.get("title")
        status = item.get("status")
        prior = before.get(title)
        if prior and prior != status:
            changes.append(f"{title} moved from {prior} to {status}.")
    delta = new_score - old_score
    if delta > 0:
        summary = f"Score improved from {old_score} to {new_score}."
    elif delta < 0:
        summary = f"Score moved from {old_score} to {new_score}."
    else:
        summary = f"Score stayed at {new_score}."
    return {
        "previousScore": old_score,
        "score": new_score,
        "delta": delta,
        "summary": summary,
        "changes": changes[:8],
    }


def analyze_resume(uid: str, client, force: bool = False) -> dict:
    uid = str(uid or "").strip()
    stored, meta = _source(uid)
    if not force and stored.get("analysis") and stored.get("sourceHash") == meta["sourceHash"]:
        return _view(stored, meta, True)
    previous = stored.get("analysis") if stored.get("sourceHash") == meta["sourceHash"] else None
    if not isinstance(previous, dict):
        previous = stored.get("previousAnalysis") if isinstance(stored.get("previousAnalysis"), dict) else None
    prompt = (
        f"{_rules()}\n"
        "Review this resume for a software or IT role. "
        "Score it from 0 to 100. "
        "checklist must include these titles exactly: "
        + ", ".join(_CHECKLIST)
        + ". status is good, weak, or missing.\n"
        "JSON keys: score, headline, strengths, improvements, checklist, skills. "
        "improvements and checklist items use title and detail. strengths and skills are strings.\n\n"
        f"Resume:\n{meta['sourceText']}"
    )
    analysis = _analysis(_generate(client, prompt))
    payload = {
        "sourceText": meta["sourceText"],
        "sourceHash": meta["sourceHash"],
        "filename": meta["filename"],
        "format": meta["format"],
        "fullText": meta["fullText"],
        "model": MODEL,
        "analyzedAt": _now(),
        "analysis": analysis,
        "previousAnalysis": None,
        "improvement": _improvement(previous, analysis),
        "comparison": stored.get("comparison") if stored.get("sourceHash") == meta["sourceHash"] else None,
        "rewrite": stored.get("rewrite") if stored.get("rewriteHash") == meta["sourceHash"] else None,
        "rewriteHash": stored.get("rewriteHash") if stored.get("rewriteHash") == meta["sourceHash"] else "",
    }
    _write(uid, payload)
    return _view(payload, meta, False)


def analyze_if_enabled(uid: str) -> None:
    """Run a review after a profile upload when the user left auto analyse on."""
    from Server.server import read_preferences

    uid = str(uid or "").strip()
    if not uid or not read_preferences(uid).get("auto_analyse_resume", True):
        return
    if _analysis_memory is not None:
        return
    snapshot = (
        get_firestore_client()
        .collection("users")
        .document(uid)
        .collection("settings")
        .document("preferences")
        .get()
    )
    stored = snapshot.to_dict() if snapshot.exists else {}
    key = str((stored or {}).get("geminiApiKey") or (stored or {}).get("apiKey") or "").strip()
    if not key:
        return
    from google import genai

    analyze_resume(uid, genai.Client(api_key=key), force=True)


def compare_resumes(uid: str, client, filename: str, content: bytes) -> dict:
    from Server.server import _resume_text

    uid = str(uid or "").strip()
    stored, meta = _source(uid)
    suffix = "." + str(filename or "").rsplit(".", 1)[-1].casefold()
    if suffix not in {".pdf", ".docx", ".txt"}:
        raise ValueError("The second resume must be a PDF, DOCX, or TXT file.")
    other = _clean_text(_resume_text(content, suffix))
    if len(other) < 20:
        raise ValueError("Could not read enough text from the second resume.")
    other_hash = _hash(other)
    previous = stored.get("comparison") if isinstance(stored.get("comparison"), dict) else {}
    if (
        stored.get("sourceHash") == meta["sourceHash"]
        and previous.get("otherHash") == other_hash
        and previous.get("sourceHash") == meta["sourceHash"]
    ):
        return _view(stored, meta, True)
    other_name = filename or "Second resume"
    prompt = (
        f"{_rules()}\n"
        "Compare resume A, which belongs to the user, with resume B. "
        "Say what is good in each. Say what A should change so it is clearer and more specific, using B only as an example. "
        "doNotCopy lists things from B the user must not add unless they are already true.\n"
        "JSON keys: summary, goodInYours, goodInTheirs, improveYours, doNotCopy. "
        "improveYours items use title and detail. The other keys are strings or lists of strings.\n\n"
        f"Resume A ({meta['filename']}):\n{meta['sourceText']}\n\n"
        f"Resume B ({other_name}):\n{other}"
    )
    comparison = _comparison(_generate(client, prompt), other_name, other_hash)
    comparison["sourceHash"] = meta["sourceHash"]
    payload = dict(stored)
    payload.update({
        "sourceText": meta["sourceText"],
        "sourceHash": meta["sourceHash"],
        "filename": meta["filename"],
        "format": meta["format"],
        "fullText": meta["fullText"],
        "model": MODEL,
        "comparison": comparison,
    })
    _write(uid, payload)
    return _view(payload, meta, False)


def rewrite_resume(uid: str, client) -> dict:
    uid = str(uid or "").strip()
    stored, meta = _source(uid)
    saved_rewrite = stored.get("rewrite") if isinstance(stored.get("rewrite"), dict) else {}
    if saved_rewrite.get("text") and stored.get("rewriteHash") == meta["sourceHash"]:
        return _view(stored, meta, True)
    notes_payload = {}
    analysis = stored.get("analysis") if isinstance(stored.get("analysis"), dict) else None
    if analysis:
        notes_payload["weak"] = [
            {"title": item.get("title"), "detail": item.get("detail")}
            for item in analysis.get("checklist") or []
            if isinstance(item, dict) and item.get("status") in {"weak", "missing"}
        ]
        notes_payload["improvements"] = analysis.get("improvements") or []
    comparison = stored.get("comparison") if isinstance(stored.get("comparison"), dict) else None
    if comparison and comparison.get("sourceHash") == meta["sourceHash"]:
        notes_payload["improveYours"] = comparison.get("improveYours") or []
        notes_payload["doNotCopy"] = comparison.get("doNotCopy") or []
    notes = json.dumps(notes_payload, ensure_ascii=False) if notes_payload else ""
    prompt = (
        f"{_rules()}\n"
        "Edit the resume. Return JSON with one key, text. "
        "The text value is the complete resume, with the same headings, order, dates, schools, jobs, projects, and contact line. "
        "Change only the lines that the notes say are weak. Leave every other line as it is. "
        "Do not drop a section. Do not turn contact details into JSON. Do not add facts that are not already in the resume.\n\n"
        f"Notes:\n{notes or 'Tighten weak wording only. Keep every section.'}\n\n"
        f"Resume:\n{meta['sourceText']}"
    )
    edited = str((_generate(client, prompt) or {}).get("text") or "").strip()
    if len(edited) < max(40, int(len(meta["sourceText"]) * 0.7)):
        raise ValueError("The update dropped parts of the original resume. Try again.")
    rewrite = {"text": edited[:_MAX_TEXT]}
    payload = dict(stored)
    payload.update({
        "sourceText": meta["sourceText"],
        "sourceHash": meta["sourceHash"],
        "filename": meta["filename"],
        "format": meta["format"],
        "fullText": meta["fullText"],
        "model": MODEL,
        "rewrite": rewrite,
        "rewriteHash": meta["sourceHash"],
    })
    _write(uid, payload)
    return _view(payload, meta, False)


def _resume_plain(resume: dict) -> str:
    if isinstance(resume, dict) and str(resume.get("text") or "").strip():
        return str(resume["text"]).strip()
    return "\n\n".join(_blocks(resume))


def _is_heading(line: str) -> bool:
    text = line.strip()
    if not text or len(text) > 42 or text.endswith((".", ",")):
        return False
    if text[:1] in "-•*":
        return False
    known = {
        "summary", "skills", "education", "experience", "projects", "project",
        "internship", "internships", "certificates", "certification", "certifications",
        "workshop and seminar", "workshops", "extra circular activities",
        "extracurricular activities", "achievements", "contact",
    }
    return text.casefold() in known or (len(text.split()) <= 4 and text.isupper())


def _resume_html(text: str) -> str:
    parts = ["<div>"]
    first = True
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        safe = escape(line)
        if first:
            parts.append(f"<h1>{safe}</h1>")
            first = False
            continue
        if "@" in line or line.casefold().startswith(("email", "phone", "linkedin")):
            parts.append(f"<p class='contact'>{safe}</p>")
            continue
        if _is_heading(line):
            parts.append(f"<h2>{safe}</h2>")
            continue
        if line[:1] in "-•*":
            parts.append(f"<p class='bullet'>{safe}</p>")
            continue
        parts.append(f"<p>{safe}</p>")
    parts.append("</div>")
    return "".join(parts)


def _blocks(resume: dict) -> list[str]:
    blocks = []
    if resume.get("name"):
        blocks.append(resume["name"])
    if resume.get("headline"):
        blocks.append(resume["headline"])
    if resume.get("contact"):
        blocks.append(resume["contact"])
    if resume.get("summary"):
        blocks.append("Summary\n" + resume["summary"])
    if resume.get("skills"):
        blocks.append("Skills\n" + ", ".join(resume["skills"]))
    for item in resume.get("experience") or []:
        body = "\n".join(part for part in (item.get("heading"), item.get("meta")) if part)
        bullets = "\n".join(f"- {bullet}" for bullet in item.get("bullets") or [])
        blocks.append("\n".join(part for part in (body, bullets) if part))
    for label, key in (("Education", "education"), ("Projects", "projects")):
        rows = []
        for item in resume.get(key) or []:
            rows.append("\n".join(part for part in (item.get("heading"), item.get("detail")) if part))
        if rows:
            blocks.append(label + "\n" + "\n\n".join(rows))
    return [block for block in blocks if block.strip()]


def _latin(text: str) -> str:
    return text.encode("latin-1", "replace").decode("latin-1")


def _pdf_bytes(resume: dict) -> bytes:
    import pymupdf

    css = (
        "h1 { font-family: sans-serif; font-size: 16px; text-align: center; margin: 0 0 4px; }"
        ".contact { font-family: sans-serif; font-size: 9px; text-align: center; margin: 0 0 8px; }"
        "h2 { font-family: sans-serif; font-size: 12px; border-bottom: 1px solid #222; margin: 10px 0 3px; }"
        "p { font-family: sans-serif; font-size: 10px; margin: 1px 0; }"
        ".bullet { margin-left: 12px; }"
    )
    html = _resume_html(_resume_plain(resume))
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_htmlbox(pymupdf.Rect(46, 42, 566, 760), html, css=css)
    return doc.tobytes()


def _docx_bytes(resume: dict) -> bytes:
    paragraphs = []
    for line in _resume_plain(resume).splitlines():
        if not line.strip():
            paragraphs.append("<w:p/>")
            continue
        bold = "<w:b/>" if _is_heading(line) or line == _resume_plain(resume).splitlines()[0] else ""
        paragraphs.append(
            f"<w:p><w:r><w:rPr>{bold}</w:rPr><w:t xml:space=\"preserve\">{escape(line)}</w:t></w:r></w:p>"
        )
    document = (
        "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>"
        "<w:document xmlns:w=\"http://schemas.openxmlformats.org/wordprocessingml/2006/main\">"
        f"<w:body>{''.join(paragraphs)}</w:body></w:document>"
    )
    content_types = (
        "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>"
        "<Types xmlns=\"http://schemas.openxmlformats.org/package/2006/content-types\">"
        "<Default Extension=\"rels\" ContentType=\"application/vnd.openxmlformats-package.relationships+xml\"/>"
        "<Default Extension=\"xml\" ContentType=\"application/xml\"/>"
        "<Override PartName=\"/word/document.xml\" "
        "ContentType=\"application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml\"/>"
        "</Types>"
    )
    rels = (
        "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>"
        "<Relationships xmlns=\"http://schemas.openxmlformats.org/package/2006/relationships\">"
        "<Relationship Id=\"rId1\" "
        "Type=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument\" "
        "Target=\"word/document.xml\"/>"
        "</Relationships>"
    )
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", rels)
        archive.writestr("word/document.xml", document)
    return buffer.getvalue()


def _txt_bytes(resume: dict) -> bytes:
    return (_resume_plain(resume) + "\n").encode("utf-8")


def download_resume(uid: str) -> tuple[bytes, str, str]:
    uid = str(uid or "").strip()
    stored, meta = _source(uid)
    rewrite = stored.get("rewrite") if isinstance(stored.get("rewrite"), dict) else None
    if not rewrite or not rewrite.get("text") or stored.get("rewriteHash") != meta["sourceHash"]:
        raise ValueError("Update the resume before downloading it.")
    file_format = meta["format"]
    stem = re.sub(r"\s+", "-", str(meta["filename"] or "resume"))
    stem = re.sub(r"\.(pdf|docx|txt)$", "", stem, flags=re.I) or "resume"
    if file_format == "DOCX":
        return _docx_bytes(rewrite), f"updated-{stem}.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if file_format == "TXT":
        return _txt_bytes(rewrite), f"updated-{stem}.txt", "text/plain; charset=utf-8"
    return _pdf_bytes(rewrite), f"updated-{stem}.pdf", "application/pdf"
