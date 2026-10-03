"""Connect to public job APIs and print openings in the terminal.

Examples (from the project root):
  python backend/Server/server.py --query engineer --where Bengaluru
  python backend/Server/server.py --source linkedin --query python --where Bengaluru
  python backend/Server/server.py --source greenhouse --boards groww --limit 5
  python backend/Server/server.py                    (start the jobs API for the web app)
"""

from __future__ import annotations

import argparse
import base64
import binascii
import io
import json
import logging
import re
import sys
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from xml.etree import ElementTree
import os

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from Server.firebase import get_firestore_client, uid_from_authorization
from Server.gemini_service import get_user_gemini_client

_LOGGER = logging.getLogger("fetchjobs")
_LOG_ENABLED = False


def enable() -> None:
    """Send INFO, WARNING, and ERROR lines to stdout. Safe to call twice.

    Stays off for the terminal CLI so printed JSON is unchanged. A line looks like:
    2026-10-01 02:20:01 INFO [fourdayweek] start window=15
    """
    global _LOG_ENABLED
    _LOG_ENABLED = True
    if _LOGGER.handlers:
        return
    _LOGGER.setLevel(logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    )
    _LOGGER.addHandler(handler)
    _LOGGER.propagate = False


def event(portal: str, level: str, message: str) -> None:
    """Log one portal lifecycle line. No-op until enable() runs."""
    if not _LOG_ENABLED:
        return
    numeric = getattr(logging, (level or "info").upper(), logging.INFO)
    _LOGGER.log(numeric, "[%s] %s", portal or "-", message)


from Server.api import (
    POSTED_WINDOW_DAYS,
    Job,
    apply_posting_facts,
    clean,
    is_tech_role,
    keeps_india_hybrid_or_remote,
    within_days,
)
from Server.control import arm, cancel, held
from Server.feeds import FeedCoordinator
from connectors.Adzuna import Adzuna
from connectors.Arbeitnow import Arbeitnow
from connectors.Ashby import Ashby
from connectors.Greenhouse import Greenhouse
from connectors.Lever import Lever
from connectors.RemoteOK import RemoteOK
from connectors.Remotive import Remotive
from connectors.LinkedIn import LinkedIn
from connectors.Unstop import Unstop
from connectors.FourDayWeek import FourDayWeek
from connectors.Himalayas import Himalayas
from connectors.Instahyre import Instahyre
from connectors.Jobicy import Jobicy
from connectors.Indeed import Indeed
from connectors.Naukri import Naukri
from connectors.Shine import Shine
from connectors.TheMuse import TheMuse
from connectors.WeWorkRemotely import WeWorkRemotely
from connectors.WorkingNomads import WorkingNomads

SLUG_SOURCES = {"greenhouse", "lever", "ashby"}
# Used only when a user has not saved portal toggles. A saved list replaces this.
DEFAULT_PAUSED_PORTALS = ("instahyre", "naukri")
PAUSED_SIDECAR_KEYS = set()
CONNECTORS = [
    Greenhouse,
    Lever,
    Ashby,
    Remotive,
    RemoteOK,
    Arbeitnow,
    Adzuna,
    LinkedIn,
    Unstop,
    Shine,
    Indeed,
    Naukri,
    Instahyre,
    Himalayas,
    Jobicy,
    TheMuse,
    WorkingNomads,
    FourDayWeek,
    WeWorkRemotely,
]
BY_KEY = {connector.key: connector for connector in CONNECTORS}

WINDOW_DAYS = POSTED_WINDOW_DAYS
JOBS_FILE = Path(__file__).resolve().parents[1] / "data" / "jobs.json"
_jobs_lock = threading.Lock()
_progress = {
    "running": False,
    "generation": 0,
    "jobs": [],
    "index": {},
    "notes": [],
    "note_keys": set(),
    "credits": [],
    "error": "",
    "fetchedAt": "",
    "last_write": 0.0,
    "last_cloud": 0.0,
    "stopped": False,
    "owner": "",
    "pausedPortals": [],
}

CREDITS = {
    "remotive": "Credit: jobs from Remotive — https://remotive.com",
    "remoteok": "Credit: jobs from Remote OK — https://remoteok.com (link each job URL)",
    "adzuna": "Credit: Jobs by Adzuna — https://www.adzuna.com",
    "himalayas": "Jobs sourced from Himalayas — https://himalayas.app",
    "jobicy": "Jobs sourced from Jobicy — https://jobicy.com",
    "themuse": "Jobs sourced from The Muse — https://www.themuse.com",
    "fourdayweek": "Jobs sourced from 4 Day Week — https://4dayweek.io",
    "weworkremotely": "Jobs sourced from We Work Remotely — https://weworkremotely.com",
    "indeed": "Jobs sourced from Indeed — https://in.indeed.com",
    "naukri": "Jobs sourced from Naukri — https://www.naukri.com",
}
SIDECAR_SOURCES = [cls for cls in CONNECTORS if getattr(cls, "persist", "") == "sidecar"]
FEED_COORDINATOR = FeedCoordinator(SIDECAR_SOURCES, WINDOW_DAYS, paused=PAUSED_SIDECAR_KEYS)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fetch jobs from public, legal APIs and print them.")
    parser.add_argument(
        "--source",
        default="all",
        help="one of: all, " + ", ".join(BY_KEY),
    )
    parser.add_argument("--query", default="", help="keywords matched in title, company, or location")
    parser.add_argument("--where", default="", help="city or location text; LinkedIn uses this as the city")
    parser.add_argument("--limit", type=int, default=5, help="jobs to print per source")
    parser.add_argument(
        "--boards",
        default="",
        help="comma-separated company slugs for Greenhouse, Lever, and Ashby",
    )
    parser.add_argument("--country", default="in", help="Adzuna country code, for example in or gb")
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="print the LinkedIn search URL without opening the browser",
    )
    parser.add_argument(
        "--sort",
        default="",
        help=(
            "comma-separated keys: salary (desc), date (desc, added on), exp (asc). "
            "Override with _asc or _desc, for example salary_asc,date_asc or exp_desc"
        ),
    )
    parser.add_argument("--list", action="store_true", help="print source names and exit")
    parser.add_argument(
        "--serve",
        action="store_true",
        help="start the jobs API (this is also what happens when no search flags are given)",
    )
    parser.add_argument(
        "--host",
        default=os.getenv("HOST", "0.0.0.0"),
        help="interface for the jobs API",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("PORT", "8001")),
        help="port for the jobs API",
    )
    return parser


def selected_sources(name: str) -> list[type]:
    key = name.strip().casefold()
    if key == "all":
        return list(CONNECTORS)
    connector = BY_KEY.get(key)
    if connector is None:
        known = ", ".join(["all", *BY_KEY])
        raise SystemExit(f"Unknown source '{name}'. Choose from: {known}")
    return [connector]


def parse_boards(raw: str) -> list[str] | None:
    boards = [part.strip() for part in raw.split(",") if part.strip()]
    return boards or None


def make_connector(cls: type, country: str):
    if cls is Adzuna:
        return cls(country=country)
    return cls()


def run_connector(
    cls: type,
    query: str,
    where: str,
    limit: int,
    boards: list[str] | None,
    country: str,
    open_browser: bool,
    posted_within_days: int | None = None,
    publish=None,
):
    started = time.perf_counter()
    event(cls.key, "info", "start")
    try:
        connector, jobs = _run_connector(
            cls, query, where, limit, boards, country, open_browser, posted_within_days, publish,
        )
    except Exception as exc:
        event(cls.key, "error", f"fetch failed: {exc}")
        raise
    for warning in getattr(connector, "warnings", []):
        event(cls.key, "warning", warning)
    elapsed = round(time.perf_counter() - started, 1)
    event(cls.key, "info", f"done jobs={len(jobs)} seconds={elapsed}")
    return connector, jobs


def _run_connector(
    cls: type,
    query: str,
    where: str,
    limit: int,
    boards: list[str] | None,
    country: str,
    open_browser: bool,
    posted_within_days: int | None = None,
    publish=None,
):
    connector = make_connector(cls, country)
    site_boards = boards if cls.key in SLUG_SOURCES else None
    streamed = False

    def on_batch(batch: list[Job]) -> None:
        nonlocal streamed
        streamed = True
        batch = [
            job
            for job in batch
            if is_tech_role(job.title) and keeps_india_hybrid_or_remote(job.location)
        ]
        if publish is not None and batch:
            publish(connector, batch)

    kwargs = {
        "query": query,
        "where": where,
        "limit": limit,
        "boards": site_boards,
    }
    if posted_within_days and (
        cls.key in SLUG_SOURCES or getattr(cls, "persist", "") == "sidecar"
    ):
        kwargs["posted_within_days"] = posted_within_days
    if publish is not None and (
        cls.key in SLUG_SOURCES or getattr(cls, "persist", "") == "sidecar"
    ):
        kwargs["on_batch"] = on_batch
    if cls.key == "linkedin":
        jobs = connector.fetch(**kwargs, open_browser=open_browser)
    else:
        jobs = connector.fetch(**kwargs)
    jobs = [
        job
        for job in jobs
        if is_tech_role(job.title) and keeps_india_hybrid_or_remote(job.location)
    ]
    if posted_within_days:
        jobs = [
            job
            for job in jobs
            if not job.posted_at or within_days(job.posted_at, posted_within_days)
        ]
    if publish is not None:
        publish(connector, [] if streamed else jobs)
    return connector, jobs


def run_sources(
    sources: list[type],
    query: str,
    where: str,
    limit: int,
    boards: list[str] | None,
    country: str,
    open_browser: bool,
    posted_within_days: int | None = None,
    publish=None,
) -> list[tuple]:
    results = []
    with ThreadPoolExecutor(max_workers=min(6, len(sources))) as pool:
        futures = {
            pool.submit(
                run_connector,
                cls,
                query,
                where,
                limit,
                boards,
                country,
                open_browser,
                posted_within_days,
                publish,
            ): cls
            for cls in sources
        }
        for future in as_completed(futures):
            cls = futures[future]
            try:
                results.append((cls.key, future.result()))
            except Exception as exc:
                connector = make_connector(cls, country)
                connector.warnings = [str(exc)]
                if publish is not None:
                    publish(connector, [])
                results.append((cls.key, (connector, [])))
    order = {cls.key: index for index, cls in enumerate(sources)}
    return [item[1] for item in sorted(results, key=lambda item: order[item[0]])]


_SORT_FIELDS = {
    "salary": "salary",
    "date": "date",
    "added": "date",
    "addedon": "date",
    "added_on": "date",
    "exp": "exp",
    "experience": "exp",
}
_SORT_DESC_BY_DEFAULT = {"salary": True, "date": True, "exp": False}
_EXP_LEVELS = (
    ("intern", 0),
    ("junior", 1),
    ("bachelor", 2),
    ("master", 4),
    ("phd", 6),
    ("senior", 5),
    ("lead", 6),
    ("staff", 8),
    ("principal", 10),
    ("director", 12),
    ("head", 12),
)


def parse_sort(raw: str) -> list[tuple[str, bool]]:
    specs: list[tuple[str, bool]] = []
    for part in raw.split(","):
        token = part.strip().casefold().replace(" ", "").replace("-", "_")
        if not token:
            continue
        descending = None
        if token.endswith("_asc"):
            token = token[:-4]
            descending = False
        elif token.endswith("_desc"):
            token = token[:-5]
            descending = True
        field = _SORT_FIELDS.get(token)
        if field is None:
            raise SystemExit(
                f"Unknown sort key '{part.strip()}'. Use salary, date, or exp "
                "(optional _asc or _desc)."
            )
        if descending is None:
            descending = _SORT_DESC_BY_DEFAULT[field]
        specs.append((field, descending))
    return specs


def _amount(raw: str) -> float:
    text = raw.strip()
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", text):
        return float(text.replace(".", ""))
    if re.fullmatch(r"\d{1,3}(?:,\d{3})+", text):
        return float(text.replace(",", ""))
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
        return float(text)
    return float(text.replace(",", ""))


def _salary_value(text: str) -> float | None:
    if not text:
        return None
    numbers: list[float] = []
    for match in re.finditer(r"(\d[\d.,]*)\s*([kK])?", text):
        token = match.group(1).strip(".,")
        if not token or not re.search(r"\d", token):
            continue
        value = _amount(token)
        if match.group(2):
            value *= 1000
        numbers.append(value)
    if not numbers:
        return None
    return max(numbers)


def _experience_value(text: str) -> float | None:
    if not text:
        return None
    years = re.search(
        r"(\d+(?:\.\d+)?)\s*(?:\+|plus)?(?:\s*(?:-|–|to)\s*\d+(?:\.\d+)?)?\s*years?",
        text,
        re.I,
    )
    if years:
        return float(years.group(1))
    folded = text.casefold()
    for word, rank in _EXP_LEVELS:
        if word in folded:
            return float(rank)
    return None


def _sort_value(job: Job, field: str):
    if field == "salary":
        return _salary_value(job.salary)
    if field == "date":
        return job.posted_at or None
    return _experience_value(job.experience)


def sort_jobs(jobs: list[Job], specs: list[tuple[str, bool]]) -> list[Job]:
    ordered = list(jobs)
    for field, descending in reversed(specs):
        def key(job: Job, field: str = field, descending: bool = descending):
            value = _sort_value(job, field)
            missing = value is None
            if descending:
                return (not missing, "" if missing else value)
            return (missing, "" if missing else value)

        ordered.sort(key=key, reverse=descending)
    return ordered


def job_record(job: Job, include_portal: bool, portal_key: str = "") -> dict:
    job = apply_posting_facts(job)
    record = {
        "company": job.company,
        "role": job.title,
        "experience": job.experience,
        "skill": job.skill,
        "salary": job.salary,
        "added on": job.posted_at,
        "location": job.location,
        "description": {
            "about company": job.about_company,
            "job description": job.job_description,
            **({"posted by": job.posted_by} if job.posted_by else {}),
            **({"email": job.poster_email} if job.poster_email else {}),
            **({"openings": job.openings} if job.openings else {}),
            **({"applicants": job.applicants} if job.applicants else {}),
        },
        "link": job.url,
        "apply": job.apply_url or job.url,
    }
    if include_portal:
        record = {"portal": job.source, "portalKey": portal_key, **record}
    return record


def print_jobs(jobs: list[Job], include_portal: bool) -> None:
    for job in jobs:
        print(json.dumps(job_record(job, include_portal), ensure_ascii=False, indent=2))


def print_source(connector, jobs: list[Job]) -> None:
    print()
    print(f"== {connector.label} ({len(jobs)}) ==")
    credit = CREDITS.get(connector.key)
    if credit and jobs:
        print(credit)
    for warning in connector.warnings:
        print(f"  note: {warning}")
    search = getattr(connector, "search_url", "")
    if connector.key == "linkedin" and search:
        print("CTA: open LinkedIn job search (you stay signed in as yourself)")
        print(search)
    elif search:
        print(search)
    if not jobs and not search:
        print("  No jobs matched.")
        return
    print_jobs(jobs, include_portal=False)


_TERMINAL_FLAGS = (
    "--source",
    "--query",
    "--where",
    "--limit",
    "--boards",
    "--sort",
    "--no-open",
    "--country",
    "--list",
)


def wants_terminal_output(argv: list[str]) -> bool:
    for arg in argv:
        name = arg.split("=", 1)[0]
        if name in _TERMINAL_FLAGS:
            return True
    return False


def main(argv: list[str] | None = None) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    given = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(given)
    if args.list:
        for connector in CONNECTORS:
            print(connector.key)
        return
    if args.limit < 1:
        raise SystemExit("--limit must be at least 1")

    if args.serve or not wants_terminal_output(given):
        serve(args.host, args.port, args.country)
        return

    sources = selected_sources(args.source)
    sort_specs = parse_sort(args.sort)
    boards = parse_boards(args.boards)
    open_browser = not args.no_open
    print(
        f"fetchJobsForMe  source={args.source}  query={args.query or '-'}  "
        f"where={args.where or '-'}  limit={args.limit}  sort={args.sort or '-'}"
    )

    ordered_results = run_sources(
        sources,
        args.query,
        args.where,
        args.limit,
        boards,
        args.country,
        open_browser,
    )
    if args.source.strip().casefold() == "all":
        print_all_portals(ordered_results, sort_specs)
        return
    for connector, jobs in ordered_results:
        print_source(connector, sort_jobs(jobs, sort_specs))


def print_all_portals(results: list[tuple], sort_specs: list[tuple[str, bool]]) -> None:
    jobs: list[Job] = []
    print()
    print("== all portals ==")
    for connector, portal_jobs in results:
        credit = CREDITS.get(connector.key)
        if credit and portal_jobs:
            print(credit)
        for warning in connector.warnings:
            print(f"  note: {warning}")
        search = getattr(connector, "search_url", "")
        if connector.key == "linkedin" and search:
            print("CTA: open LinkedIn job search (you stay signed in as yourself)")
            print(search)
        jobs.extend(portal_jobs)
    jobs = sort_jobs(jobs, sort_specs)
    print(f"jobs: {len(jobs)}")
    if not jobs:
        print("  No jobs matched.")
        return
    print_jobs(jobs, include_portal=True)


def read_jobs_file() -> dict | None:
    try:
        payload = json.loads(JOBS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
        return None
    return payload


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    body = json.dumps(payload, ensure_ascii=False)
    for attempt in range(5):
        try:
            temporary.write_text(body, encoding="utf-8")
            temporary.replace(path)
            return
        except OSError:
            time.sleep(0.3 * (attempt + 1))
    try:
        path.write_text(body, encoding="utf-8")
    except OSError as exc:
        event("api", "error", f"could not save {path.name}: {exc}")


def write_jobs_file(payload: dict) -> None:
    _write_json(JOBS_FILE, payload)


_saved_lock = threading.Lock()
# Tests set this to a dict. The API leaves it empty and uses Firestore.
_saved_memory: dict | None = None
_SAVED_FIELDS = (
    "portal", "portalKey", "company", "role", "experience",
    "skill", "salary", "added on", "location", "link", "apply",
)


def _saved_collection(uid: str):
    uid = str(uid or "").strip()
    if not uid:
        raise ValueError("sign in is required")
    return (
        get_firestore_client()
        .collection("users")
        .document(uid)
        .collection("savedJobs")
    )


def _saved_document(uid: str):
    """One document per user. Starring or removing a job replaces this document."""
    return _saved_collection(uid).document("saved_jobs")


def _jobs_in_saved_document(payload: dict) -> list[dict]:
    jobs = payload.get("jobs")
    if isinstance(jobs, list):
        return [item for item in jobs if isinstance(item, dict) and item.get("link")]
    if payload.get("link"):
        return [payload]
    return []


def _load_saved_jobs(uid: str) -> list[dict]:
    """Read users/{uid}/savedJobs/saved_jobs. Older documents are folded in, then deleted."""
    document = _saved_document(uid)
    current = document.get()
    legacy = []
    if current.exists:
        jobs = _jobs_in_saved_document(current.to_dict() or {})
    else:
        jobs = []
        previous = _saved_collection(uid).document("current").get()
        if previous.exists:
            jobs = _jobs_in_saved_document(previous.to_dict() or {})
            legacy.append(previous.reference)
        else:
            for old in _saved_collection(uid).stream():
                if old.id in {"saved_jobs", "current"}:
                    continue
                item = old.to_dict() or {}
                found = _jobs_in_saved_document(item)
                if found:
                    jobs.extend(found)
                    legacy.append(old.reference)
    if legacy:
        jobs.sort(key=lambda item: item.get("saved at") or "", reverse=True)
        document.set({"jobs": jobs})
        for old in legacy:
            old.delete()
    return jobs


def read_saved(uid: str) -> list[dict]:
    """Saved jobs for one signed-in user. The list lives in one replaceable document."""
    uid = str(uid or "").strip()
    if not uid:
        raise ValueError("sign in is required")
    if _saved_memory is not None:
        return [dict(item) for item in _saved_memory.get(uid, []) if item.get("link")]
    try:
        jobs = _load_saved_jobs(uid)
    except Exception as exc:
        event("saved", "error", f"read failed: {exc}")
        raise ValueError("could not read saved jobs") from exc
    jobs.sort(key=lambda item: item.get("saved at") or "", reverse=True)
    return jobs


def _saved_record(record: dict) -> dict:
    if not isinstance(record, dict):
        raise ValueError("job must be an object")
    link = str(record.get("link") or "").strip()
    if not link.startswith(("http://", "https://")):
        raise ValueError("job needs an http(s) link")
    clean = {field: str(record.get(field) or "") for field in _SAVED_FIELDS}
    clean["link"] = link
    apply = str(record.get("apply") or "").strip()
    clean["apply"] = apply if apply.startswith(("http://", "https://")) else link
    description = record.get("description") if isinstance(record.get("description"), dict) else {}
    clean["description"] = {
        "about company": str(description.get("about company") or ""),
        "job description": str(description.get("job description") or ""),
    }
    for field in ("openings", "applicants", "posted by", "email"):
        value = str(description.get(field) or "").strip()
        if value:
            clean["description"][field] = value
    return with_portal_key(clean)


def save_job(uid: str, record: dict) -> dict:
    uid = str(uid or "").strip()
    if not uid:
        raise ValueError("sign in is required")
    job = _saved_record(record)
    with _saved_lock:
        if _saved_memory is not None:
            bucket = _saved_memory.setdefault(uid, [])
            existing = next((item for item in bucket if item["link"] == job["link"]), None)
            if existing is not None:
                event("saved", "info", f"already saved {job['role']} — {job['company']}")
                return dict(existing)
            job["saved at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            bucket.insert(0, job)
        else:
            document = _saved_document(uid)
            try:
                jobs = _load_saved_jobs(uid)
                existing = next((item for item in jobs if item.get("link") == job["link"]), None)
                if existing is not None:
                    event("saved", "info", f"already saved {job['role']} — {job['company']}")
                    return existing
                job["saved at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                jobs.insert(0, job)
                document.set({"jobs": jobs})
            except Exception as exc:
                event("saved", "error", f"save failed: {exc}")
                raise ValueError("could not save job") from exc
    event(job.get("portalKey") or "saved", "info", f"saved {job['role']} — {job['company']}")
    return job


def unsave_job(uid: str, link: str) -> bool:
    uid = str(uid or "").strip()
    if not uid:
        raise ValueError("sign in is required")
    with _saved_lock:
        if _saved_memory is not None:
            bucket = _saved_memory.get(uid, [])
            remaining = [item for item in bucket if item["link"] != link]
            if len(remaining) == len(bucket):
                return False
            _saved_memory[uid] = remaining
        else:
            document = _saved_document(uid)
            try:
                jobs = _load_saved_jobs(uid)
                remaining = [item for item in jobs if item.get("link") != link]
                if len(remaining) == len(jobs):
                    return False
                document.set({"jobs": remaining})
            except Exception as exc:
                event("saved", "error", f"remove failed: {exc}")
                raise ValueError("could not remove saved job") from exc
    event("saved", "info", f"removed {link}")
    return True


# Tests set this to a dict. The API leaves it empty and uses the same Firestore
# document the Settings page already writes: users/{uid}/settings/preferences.
_preferences_memory: dict | None = None
_preferences_lock = threading.Lock()
_POSTED_DAYS = {"today": 1, "yesterday": 2, "7days": 7, "15days": 15}
_EXP_TEXT = (
    (re.compile(r"\b(?:intern|internship|fresher|freshers|graduate|entry[- ]level|trainee)\b", re.I), 0),
    (re.compile(r"\bjunior\b", re.I), 1),
    (re.compile(r"\b(?:mid[- ]level|intermediate)\b", re.I), 2),
    (re.compile(r"\bsenior\b", re.I), 3),
    (re.compile(r"\b(?:lead|staff|principal|architect|manager)\b", re.I), 5),
)


def _preferences_document(uid: str):
    uid = str(uid or "").strip()
    if not uid:
        raise ValueError("sign in is required")
    return (
        get_firestore_client()
        .collection("users")
        .document(uid)
        .collection("settings")
        .document("preferences")
    )


def _empty_preferences() -> dict:
    return {
        "experience": None,
        "posted": "all",
        "roles": [],
        "pausedPortals": list(DEFAULT_PAUSED_PORTALS),
        "auto_analyse_resume": True,
        "updatedAt": "",
    }


def _paused_portals(payload: dict | None) -> list[str]:
    """Portal keys that should not be fetched. A missing list keeps Instahyre and Naukri paused."""
    if not isinstance(payload, dict) or "pausedPortals" not in payload:
        return list(DEFAULT_PAUSED_PORTALS)
    raw = payload.get("pausedPortals")
    if not isinstance(raw, list):
        return list(DEFAULT_PAUSED_PORTALS)
    known = set(BY_KEY)
    return list(dict.fromkeys(
        str(item).strip() for item in raw if str(item).strip() in known
    ))


def _experience_setting(value):
    from Server.feeds import experience_cap

    if value is None or str(value).strip().casefold() in {"", "all", "select"}:
        return None
    cap = experience_cap(value)
    if cap is None:
        raise ValueError("experience must be Select or a number from 0 to 5")
    return cap


def _posted_setting(value: str) -> str:
    posted = str(value or "all").strip().casefold()
    if posted in {"", "select", "all"}:
        return "all"
    if posted not in _POSTED_DAYS:
        raise ValueError("posted must be Select, today, yesterday, 7days, or 15days")
    return posted


def _preferences_from_payload(payload: dict) -> dict:
    """Search fields only. The Gemini key stays on the same document and is not returned."""
    result = _empty_preferences()
    if not isinstance(payload, dict):
        return result
    try:
        result["experience"] = _experience_setting(payload.get("experience"))
    except ValueError:
        result["experience"] = None
    try:
        result["posted"] = _posted_setting(payload.get("posted"))
    except ValueError:
        result["posted"] = "all"
    roles = payload.get("roles")
    if isinstance(roles, list):
        result["roles"] = list(dict.fromkeys(
            clean(str(role))[:80] for role in roles if clean(str(role))
        ))[:20]
    updated = payload.get("updatedAt")
    result["updatedAt"] = "" if updated is None else str(updated)
    result["pausedPortals"] = _paused_portals(payload)
    if "auto_analyse_resume" in payload:
        result["auto_analyse_resume"] = bool(payload.get("auto_analyse_resume"))
    return result


def read_preferences(uid: str) -> dict:
    uid = str(uid or "").strip()
    if not uid:
        return _empty_preferences()
    if _preferences_memory is not None:
        return _preferences_from_payload(_preferences_memory.get(uid) or {})
    try:
        snapshot = _preferences_document(uid).get()
        payload = snapshot.to_dict() if snapshot.exists else {}
    except Exception as exc:
        event("preferences", "error", f"read failed: {exc}")
        return _empty_preferences()
    return _preferences_from_payload(payload if isinstance(payload, dict) else {})


def update_preferences(uid: str, changes: dict) -> dict:
    if not isinstance(changes, dict):
        raise ValueError("preferences must be an object")
    uid = str(uid or "").strip()
    if not uid:
        raise ValueError("sign in is required")
    experience = _experience_setting(changes.get("experience"))
    posted = _posted_setting(changes.get("posted"))
    roles = changes.get("roles")
    if not isinstance(roles, list):
        raise ValueError("roles must be a list")
    preferences = {
        "experience": experience,
        "posted": posted,
        "roles": list(dict.fromkeys(
            clean(str(role))[:80] for role in roles if clean(str(role))
        ))[:20],
        "pausedPortals": _paused_portals(changes),
        "auto_analyse_resume": bool(changes.get("auto_analyse_resume", True)),
        "updatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
    }
    with _preferences_lock:
        if _preferences_memory is not None:
            _preferences_memory[uid] = dict(preferences)
        else:
            try:
                # Merge keeps the Gemini key that Settings stores on this same document.
                _preferences_document(uid).set(preferences, merge=True)
            except Exception as exc:
                event("preferences", "error", f"update failed: {exc}")
                raise ValueError("could not update preferences") from exc
    event("preferences", "info", f"updated roles={len(preferences['roles'])}")
    return preferences


def preference_days(preferences: dict) -> int:
    return _POSTED_DAYS.get(str(preferences.get("posted") or ""), WINDOW_DAYS)


def _feed_preferences(preferences: dict) -> dict:
    from Server.feeds import experience_cap

    return {
        "windowDays": preference_days(preferences),
        "experience": experience_cap(preferences.get("experience")),
        "roles": preferences.get("roles") or [],
        "paused": list(preferences.get("pausedPortals") or []),
    }


def _minimum_experience(record: dict) -> int | None:
    text = clean(f"{record.get('experience') or ''} {record.get('role') or ''}")
    experience = clean(record.get("experience"))
    numbers = [int(value) for value in re.findall(r"\d{1,2}", experience)]
    if numbers:
        return min(numbers)
    for pattern, years in _EXP_TEXT:
        if pattern.search(text):
            return years
    return None


def _matches_preferences(record: dict, preferences: dict) -> bool:
    if (record.get("portalKey") or "") in set(preferences.get("pausedPortals") or []):
        return False
    posted = str(record.get("added on") or "")
    days = preference_days(preferences)
    if posted and not within_days(posted, days):
        return False
    from Server.feeds import experience_cap, role_matches

    minimum = _minimum_experience(record)
    cap = experience_cap(preferences.get("experience"))
    if cap is not None and minimum is not None and minimum > cap:
        return False
    return role_matches(record.get("role") or "", record.get("skill") or "", preferences.get("roles") or [])


def apply_preferences(payload: dict, preferences: dict) -> dict:
    result = dict(payload)
    result["jobs"] = [
        record for record in payload.get("jobs") or []
        if _matches_preferences(record, preferences)
    ]
    result["count"] = len(result["jobs"])
    paused = set(preferences.get("pausedPortals") or [])
    result["portals"] = [
        {**portal, "status": "paused", "warnings": [], "error": ""}
        if portal.get("key") in paused
        else portal
        for portal in result.get("portals") or []
    ]
    result["preferences"] = preferences
    return result


# Candidate profile and resume. The profile is deliberately provider-neutral so
# recommendations, applications, and future agents can all consume one schema.

MAX_RESUME_BYTES = 8 * 1024 * 1024
_profile_lock = threading.Lock()
_PROFILE_TEXT_FIELDS = (
    "fullName", "email", "phone", "headline", "summary", "currentTitle",
    "currentCompany", "totalExperience", "noticePeriod", "expectedSalary",
    "salaryCurrency", "linkedin", "github", "portfolio", "workAuthorization",
    "education",
)
_PROFILE_LIST_FIELDS = (
    "skills", "targetRoles", "preferredLocations", "workModes", "employmentTypes",
)
_PROFILE_BOOL_FIELDS = ("openToWork", "willingToRelocate")
_SKILL_TERMS = (
    "Python", "Java", "JavaScript", "TypeScript", "React", "Angular", "Vue",
    "Node.js", "Express", "Django", "Flask", "FastAPI", "Spring Boot", ".NET",
    "C", "C++", "C#", "Go", "Rust", "Kotlin", "Swift", "PHP", "Ruby",
    "SQL", "MySQL", "PostgreSQL", "MongoDB", "Redis", "Oracle", "Snowflake",
    "AWS", "Azure", "GCP", "Docker", "Kubernetes", "Terraform", "Jenkins",
    "Git", "Linux", "REST", "GraphQL", "Kafka", "Spark", "Hadoop", "Airflow",
    "Machine Learning", "Deep Learning", "NLP", "TensorFlow", "PyTorch",
    "Pandas", "NumPy", "Data Analysis", "Power BI", "Tableau", "Selenium",
    "Cypress", "Playwright", "API Testing", "Agile", "Scrum", "DevOps",
    "Microservices", "System Design", "Data Structures", "Algorithms",
)


def _empty_profile() -> dict:
    profile = {field: "" for field in _PROFILE_TEXT_FIELDS}
    profile.update({field: [] for field in _PROFILE_LIST_FIELDS})
    profile.update({field: False for field in _PROFILE_BOOL_FIELDS})
    profile.update({
        "resume": None,
        "updatedAt": "",
    })
    return profile


def _profile_completion(profile: dict) -> int:
    important = (
        "fullName", "email", "phone", "headline", "summary", "currentTitle",
        "totalExperience", "skills", "targetRoles", "preferredLocations",
        "workModes", "education", "experienceHistory", "resume",
    )
    complete = sum(bool(profile.get(field)) for field in important)
    return round(complete * 100 / len(important))

def _profile_document(uid: str):
    uid = str(uid or "").strip()

    if not uid:
        raise ValueError("authenticated user UID is required")

    return (
        get_firestore_client()
        .collection("users")
        .document(uid)
        .collection("jobProfile")
        .document("default")
    )

def read_profile(uid: str) -> dict:
    profile = _empty_profile()

    try:
        snapshot = _profile_document(uid).get()
        payload = snapshot.to_dict() if snapshot.exists else {}
    except Exception as exc:
        event(
            "profile",
            "error",
            f"profile read failed: {exc}",
        )
        raise ValueError("could not read profile") from exc

    if not isinstance(payload, dict):
        payload = {}

    for field in _PROFILE_TEXT_FIELDS:
        value = payload.get(field)
        if isinstance(value, str):
            profile[field] = value

    for field in _PROFILE_LIST_FIELDS:
        value = payload.get(field)

        if isinstance(value, list):
            profile[field] = list(
                dict.fromkeys(
                    str(item).strip()
                    for item in value
                    if str(item).strip()
                )
            )[:100]

    for field in _PROFILE_BOOL_FIELDS:
        profile[field] = bool(payload.get(field, False))

    resume = payload.get("resume")

    if isinstance(resume, dict):
        profile["resume"] = resume

    profile["experienceHistory"] = _normalize_jobs(
        payload.get("experienceHistory"),
        profile.get("currentTitle", ""),
        profile.get("currentCompany", ""),
    )
    profile["updatedAt"] = str(payload.get("updatedAt") or "")
    profile["completion"] = _profile_completion(profile)

    return profile

def update_profile(uid: str, changes: dict) -> dict:
    if not isinstance(changes, dict):
        raise ValueError("profile must be an object")

    uid = str(uid or "").strip()

    if not uid:
        raise ValueError("authenticated user UID is required")

    with _profile_lock:
        profile = read_profile(uid)

        for field in _PROFILE_TEXT_FIELDS:
            if field in changes:
                profile[field] = str(
                    changes[field] or ""
                ).strip()[:10_000]

        for field in _PROFILE_LIST_FIELDS:
            if field not in changes:
                continue

            value = changes[field]

            if not isinstance(value, list):
                raise ValueError(f"{field} must be a list")

            profile[field] = list(
                dict.fromkeys(
                    str(item).strip()[:120]
                    for item in value
                    if str(item).strip()
                )
            )[:100]

        if "experienceHistory" in changes:
            jobs = _normalize_jobs(changes.get("experienceHistory"))
            profile["experienceHistory"] = jobs
            if jobs and jobs[0].get("title"):
                profile["currentTitle"] = jobs[0]["title"]
            if jobs and jobs[0].get("company"):
                profile["currentCompany"] = jobs[0]["company"]

        for field in _PROFILE_BOOL_FIELDS:
            if field in changes:
                profile[field] = bool(changes[field])

        profile["updatedAt"] = datetime.now(
            timezone.utc
        ).isoformat()

        profile.pop("completion", None)

        try:
            _profile_document(uid).set(profile)
        except Exception as exc:
            event(
                "profile",
                "error",
                f"profile update failed: {exc}",
            )
            raise ValueError("could not update profile") from exc

    event("profile", "info", "profile updated")

    return read_profile(uid)

def _resume_text(content: bytes, suffix: str) -> str:
    suffix = suffix.casefold()

    if suffix == ".txt":
        return content.decode("utf-8", errors="replace")

    if suffix == ".docx":
        return _docx_resume_text(content)

    if suffix == ".pdf":
        return _pdf_resume_text(content)

    raise ValueError("resume must be a PDF, DOCX, or TXT file")

def _docx_resume_text(content: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            xml = archive.read("word/document.xml")

        root = ElementTree.fromstring(xml)

    except (
        KeyError,
        zipfile.BadZipFile,
        ElementTree.ParseError,
    ) as exc:
        raise ValueError("invalid DOCX resume") from exc

    namespace = (
        "{http://schemas.openxmlformats.org/"
        "wordprocessingml/2006/main}"
    )

    paragraphs = []

    for paragraph in root.iter(f"{namespace}p"):
        text = "".join(
            node.text or ""
            for node in paragraph.iter(f"{namespace}t")
        ).strip()

        if text:
            paragraphs.append(text)

    extracted = "\n".join(paragraphs).strip()

    if len(extracted) < 20:
        raise ValueError(
            "could not extract enough text from the DOCX resume"
        )

    return extracted

def _pdf_resume_text(content: bytes) -> str:
    errors = []

    # First parser: pypdf
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(content), strict=False)

        if reader.is_encrypted:
            decrypt_result = reader.decrypt("")

            if decrypt_result == 0:
                raise ValueError(
                    "password-protected PDF is not supported"
                )

        pages = []

        for page in reader.pages:
            try:
                text = page.extract_text(
                    extraction_mode="layout"
                ) or ""
            except TypeError:
                # For older pypdf versions without extraction_mode.
                text = page.extract_text() or ""

            if text.strip():
                pages.append(text)

        extracted = "\n".join(pages).strip()

        if len(extracted) >= 20:
            return extracted

        errors.append("pypdf returned insufficient text")

    except Exception as exc:
        errors.append(f"pypdf: {exc}")

    # Second parser: PyMuPDF
    try:
        import pymupdf

        document = pymupdf.open(
            stream=content,
            filetype="pdf",
        )

        pages = []

        for page in document:
            blocks = page.get_text("blocks")
            blocks.sort(key=lambda block: (block[1], block[0]))

            page_text = "\n".join(
                str(block[4]).strip()
                for block in blocks
                if len(block) > 4 and str(block[4]).strip()
            )

            if page_text:
                pages.append(page_text)

        document.close()

        extracted = "\n".join(pages).strip()

        if len(extracted) >= 20:
            return extracted

        errors.append("PyMuPDF returned insufficient text")

    except Exception as exc:
        errors.append(f"PyMuPDF: {exc}")

    event(
        "profile",
        "error",
        "PDF extraction failed: " + "; ".join(errors),
    )

    raise ValueError(
        "This PDF contains insufficient selectable text. "
        "Upload a text-based PDF or DOCX file. "
        "A scanned or image-only PDF requires OCR."
    )


import re
from typing import Optional


# Canonical headings supported by the parser.
_SECTION_HEADINGS = {
    "summary": {
        "summary",
        "profile",
        "professional summary",
        "career summary",
        "executive summary",
        "about me",
    },
    "skills": {
        "skills",
        "skill summary",
        "skills summary",
        "technical skills",
        "core skills",
        "core competencies",
        "technologies",
        "technical expertise",
    },
    "experience": {
        "experience",
        "work experience",
        "professional experience",
        "employment",
        "employment history",
        "work history",
        "career history",
    },
    "certifications": {
        "certification",
        "certifications",
        "certifications and achievements",
        "certifications & achievements",
        "achievements",
        "awards",
        "awards and achievements",
        "awards & achievements",
    },
    "education": {
        "education",
        "academic background",
        "academic qualifications",
        "qualifications",
        "educational qualifications",
    },
    "projects": {
        "projects",
        "project experience",
        "personal projects",
        "academic projects",
    },
}

def _normalize_heading(value: str) -> str:
    value = str(value or "").strip()

    value = re.sub(r"^[\s#*_•\-–—:|]+", "", value)
    value = re.sub(r"[\s#*_•\-–—:|]+$", "", value)
    value = re.sub(r"\s*&\s*", " and ", value)
    value = re.sub(r"\s+", " ", value)

    return value.casefold().strip()

def _heading_type(line: str) -> Optional[str]:
    normalized = _normalize_heading(line)

    if not normalized or len(normalized) > 80:
        return None

    for section_type, headings in _SECTION_HEADINGS.items():
        normalized_headings = {
            _normalize_heading(heading)
            for heading in headings
        }

        if normalized in normalized_headings:
            return section_type

    return None

def _clean_resume_lines(text: str) -> list[str]:
    text = str(text or "").replace("\x00", " ")
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    cleaned = []

    for raw_line in text.splitlines():
        line = re.sub(r"[ \t]+", " ", raw_line).strip()

        line = re.sub(r"^\*+\s*", "", line)
        line = re.sub(r"\s*\*+$", "", line)

        if line:
            cleaned.append(line)

    return cleaned

_BULLET = re.compile(
    r"^\s*(?:[•●▪◦‣·∙○■□►▶✓✔➢➤❖\uf0b7\uf0a7\uf076\uf0d8\uf0fc*]|[-–—](?=\s))\s*"
)
_BULLET_CHARS = "•●▪◦‣·∙○■□►▶✓✔➢➤❖\uf0b7\uf0a7\uf076\uf0d8\uf0fc"

# Checked in this order, so "Project Experience" is a project section
# and "Coursework / Skills" is a skills section.
_HEADING_KEYWORDS = (
    ("projects", ("project",)),
    ("skills", ("skill", "technolog", "competenc", "tech stack", "tools", "expertise", "proficienc")),
    ("education", ("education", "academic", "qualification", "schooling")),
    ("certifications", ("certific", "award", "achievement", "honor", "honour", "accomplishment")),
    ("summary", ("summary", "profile", "objective", "about me", "overview")),
    ("experience", ("experience", "employment", "work history", "career history", "internship")),
    ("other", (
        "training", "course", "workshop", "seminar", "activit", "interest", "hobb",
        "language", "publication", "volunteer", "reference", "leadership",
        "responsibilit", "declaration", "personal", "strength", "extra", "curricular",
        "contact", "links",
    )),
)

_ROLE_WORDS = re.compile(
    r"\b(?:developer|engineer|intern|internship|analyst|manager|consultant|designer|tester|"
    r"lead|architect|administrator|admin|specialist|associate|scientist|trainee|executive|"
    r"officer|programmer|director|coordinator|assistant|devops|sre|qa|sde|technician|"
    r"representative|researcher|instructor|teacher|freelancer|founder|co-founder|president|"
    r"head|support|owner|member|fellow|apprentice|contractor)s?\b",
    re.I,
)

_COMPANY_WORDS = re.compile(
    r"\b(?:pvt|private|ltd|limited|inc|llc|llp|corp|corporation|company|co\.|technologies|"
    r"technology|solutions|labs|systems|services|consulting|consultancy|software|softech|"
    r"infotech|group|bank|university|college|institute|studio|ventures|global)\b",
    re.I,
)


def _resume_rows(text: str) -> list[str]:
    """Lines with their column gaps kept. Blank lines are dropped."""
    text = str(text or "").replace("\x00", " ").replace("\r\n", "\n").replace("\r", "\n")
    return [line.replace("\t", "    ").rstrip() for line in text.splitlines() if line.strip()]


def _columns(row: str) -> list[str]:
    parts = re.split(r"\s{3,}|\s+\|\s+", row.strip())
    return [part.strip(" |") for part in parts if part.strip(" |")]


def _section_heading(row: str) -> Optional[str]:
    """Section type for a heading row, or None when the row is content."""
    known = _heading_type(row)
    if known:
        return known
    line = row.strip()
    if _BULLET.match(line) or len(_columns(line)) > 1:
        return None
    line = re.sub(r"^[\s#*_\-–—|]+|[\s#*_\-–—|:]+$", "", line)
    if not line or len(line) > 45 or len(line.split()) > 6:
        return None
    if re.search(r"\d|@|:|\.$|,", line) or _ROLE_WORDS.search(re.sub(r"internships?", "", line, flags=re.I)):
        return None
    folded = line.casefold()
    for section, keywords in _HEADING_KEYWORDS:
        if any(re.search(rf"\b{re.escape(word)}", folded) for word in keywords):
            return section
    return None


def _resume_sections(text: str) -> dict[str, list[str]]:
    """Rows grouped by section. Repeated sections of one type are joined."""
    sections: dict[str, list[str]] = {}
    current = "header"
    for row in _resume_rows(text):
        kind = _section_heading(row)
        if kind:
            current = kind
            sections.setdefault(current, [])
            continue
        sections.setdefault(current, []).append(row)
    return sections


def _section_text(rows: list[str]) -> str:
    lines = []
    for row in rows:
        parts = [re.sub(r"\s*[-–—|,]+$", "", part).strip() for part in _columns(row)]
        line = " · ".join(part for part in parts if part)
        if line:
            lines.append(line)
    return "\n".join(lines)[:10_000]


def _extract_section(lines: list[str], section_name: str) -> str:
    return _section_text(_resume_sections("\n".join(lines)).get(section_name, []))


def _looks_like_contact_line(line: str) -> bool:
    folded = line.casefold()

    return bool(
        "@" in line
        or "linkedin" in folded
        or "github" in folded
        or re.search(r"\+?\d[\d\s\-()]{8,}", line)
    )


def _extract_name(lines: list[str]) -> str:
    for line in lines[:10]:
        if _heading_type(line):
            break

        if _looks_like_contact_line(line):
            continue

        candidate = re.sub(
            r"\b(?:email|mobile|phone|linkedin)\s*:.*$",
            "",
            line,
            flags=re.I,
        ).strip()

        words = candidate.split()

        if (
            2 <= len(words) <= 5
            and len(candidate) <= 70
            and not re.search(r"\d", candidate)
        ):
            return candidate.title() if candidate.isupper() else candidate

    return ""

def _extract_current_employment(
    experience_text: str,
) -> tuple[str, str]:

    lines = [
        re.sub(r"\s+", " ", line).strip()
        for line in experience_text.splitlines()
        if line.strip()
    ]

    if not lines:
        return "", ""

    title = ""
    company = ""

    for line in lines[:5]:

        if line.lower().startswith("client:"):
            continue

        if line.startswith(("•", "-", "*")):
            continue

        header = line

        match = re.match(
            r"^(?P<title>.+?)\s*(?:—|–|-| at )\s*(?P<company>.+)$",
            header,
            re.I,
        )

        if match:
            title = match.group("title").strip()
            company_text = match.group("company").strip()

            company = company_text.split(",")[0].strip()
            break

        parts = [p.strip() for p in header.split(",") if p.strip()]

        if len(parts) >= 2:
            title = parts[0]
            company = parts[1]
            break

    return title[:150], company[:150]


_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
_YEAR = r"(?:19|20)\d{2}"
_DATE_POINT = (
    rf"(?:(?:\d{{1,2}}(?:st|nd|rd|th)?[\s\-/]*)?{_MONTH}[\s\-/,']*{_YEAR}"
    rf"|(?:0?[1-9]|1[0-2])\s*[/\-.\s]\s*{_YEAR}"
    rf"|{_YEAR})"
)
_DATE_RANGE = re.compile(
    rf"(?P<start>{_DATE_POINT})\s*(?:-|–|—|to|till|until)\s*(?P<end>{_DATE_POINT}|present|current|now|ongoing|today)",
    re.I,
)
_DATE_SINGLE = re.compile(rf"^(?:{_DATE_POINT}|present)$", re.I)


def _clean_job(item: dict) -> dict:
    end = str(item.get("endDate") or "").strip()
    if end.casefold() in {"present", "current", "now"}:
        end = "Present"
    return {
        "title": str(item.get("title") or "").strip()[:150],
        "company": str(item.get("company") or "").strip()[:150],
        "startDate": str(item.get("startDate") or "").strip()[:40],
        "endDate": end[:40],
        "location": str(item.get("location") or "").strip()[:80],
        "description": str(item.get("description") or "").strip()[:4000],
    }


def _job_has_content(job: dict) -> bool:
    return any(job.get(key) for key in ("title", "company", "startDate", "endDate", "location", "description"))


def _normalize_jobs(value, title: str = "", company: str = "") -> list[dict]:
    """One entry per role. A legacy paragraph becomes a single role."""
    jobs = []
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                jobs.append(_clean_job(item))
            elif str(item or "").strip():
                jobs.append(_clean_job({"description": item}))
    elif str(value or "").strip():
        jobs.append(_clean_job({
            "title": title,
            "company": company,
            "description": value,
        }))
    return [job for job in jobs if _job_has_content(job)][:12]


def _is_bullet(line: str) -> bool:
    return bool(_BULLET.match(line)) and bool(_BULLET.sub("", line, count=1).strip())


def _split_outside_parens(text: str, separator: str = ",") -> list[str]:
    parts, depth, current = [], 0, ""
    for char in text:
        depth += char in "([{"
        depth -= char in ")]}" and depth > 0
        if char == separator and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += char
    parts.append(current)
    return [part.strip() for part in parts if part.strip()]


def _looks_like_location(text: str) -> bool:
    text = text.strip()
    if not text or len(text) > 45 or _ROLE_WORDS.search(text) or _COMPANY_WORDS.search(text):
        return False
    if re.fullmatch(r"(?:remote|hybrid|on-?site|wfh|online)(?:\s*,\s*[A-Za-z .'-]+)?", text, re.I):
        return True
    return bool(re.fullmatch(r"[A-Z][A-Za-z.'\- ]+(?:,\s*[A-Za-z][A-Za-z.'\- ]+){1,2}", text))


def _role_company(line: str) -> tuple[str, str]:
    """Title and company from one header written as Title at/|/— Company or Title, Company."""
    if line.endswith((".", "!", "?")) or len(line) > 110:
        return "", ""
    match = re.match(r"^(?P<a>.+?)\s+(?:—|–|-|\||@|at)\s+(?P<b>.+)$", line, re.I)
    if match and not _DATE_RANGE.search(line):
        return _title_and_company(match.group("a").strip(" ,"), match.group("b").strip(" ,"))
    parts = _split_outside_parens(line)
    if len(parts) >= 2 and not _DATE_RANGE.search(line):
        first, second = parts[0], parts[1]
        if len(first) <= 80 and len(second) <= 80 and not _looks_like_location(", ".join(parts[1:])):
            return _title_and_company(first, second)
    return "", ""


def _role_score(text: str) -> float:
    """Above zero reads like a job title, below zero like an organisation."""
    base = re.sub(r"\([^)]*\)", " ", text)
    return len(_ROLE_WORDS.findall(base)) - 0.5 * len(_COMPANY_WORDS.findall(base))


def _is_org_name(text: str) -> bool:
    """A heading written in capitals is an organisation, not a job title."""
    letters = [char for char in text if char.isalpha()]
    return len(letters) >= 3 and all(char.isupper() for char in letters)


def _title_and_company(
    first: str,
    second: str,
    first_dated: bool = False,
    second_dated: bool = False,
) -> tuple[str, str]:
    """Order two header texts as (title, company)."""
    if _role_score(second) > _role_score(first):
        return second, first
    if _role_score(first) > _role_score(second):
        return first, second
    first_org = _is_org_name(first) or (first_dated and not second_dated)
    second_org = _is_org_name(second) or (second_dated and not first_dated)
    if first_org and not second_org:
        return second, first
    if second_org and not first_org:
        return first, second
    return first, second


def _take_dates(entry: dict, text: str) -> str:
    """Store a date range found in text on the entry, and return the text without it."""
    match = _DATE_RANGE.search(text)
    if match:
        if not entry["startDate"]:
            entry["startDate"] = match.group("start").strip()
            entry["endDate"] = match.group("end").strip()
        return (text[:match.start()] + text[match.end():]).strip(" ,|-–—()")
    if _DATE_SINGLE.match(text.strip()):
        if not entry["startDate"] and not entry["endDate"]:
            entry["endDate"] = text.strip()
        return ""
    return text


_COMPANY_LABEL = re.compile(
    r"^(?:c\w{0,3}pany(?:\s+name)?|organi[sz]ation|employer|firm|client)\s*[:\-–—]\s*(?P<value>.+)$",
    re.I,
)
_ROLE_LABEL = re.compile(
    r"^(?:role|designation|position|job\s+title|title|profile)\s*[:\-–—]\s*(?P<value>.+)$",
    re.I,
)
_LOCATION_LABEL = re.compile(r"^(?:location|place|city)\s*[:\-–—]\s*(?P<value>.+)$", re.I)
_DURATION_LABEL = re.compile(r"^(?:duration|period|dates?)\s*[:\-–—]\s*(?P<value>.+)$", re.I)


def _labelled(line: str) -> bool:
    return any(pattern.match(line) for pattern in (_COMPANY_LABEL, _ROLE_LABEL, _LOCATION_LABEL, _DURATION_LABEL))


def _resolve_entry(headers: list[str], notes: list[str]) -> dict:
    """Turn the header rows of one role into title, company, dates, and location."""
    entry = {"title": "", "company": "", "startDate": "", "endDate": "", "location": "", "description": ""}
    texts: list[tuple[str, bool]] = []
    for row in headers:
        columns = _columns(row)
        dated_row = bool(_DATE_RANGE.search(row))
        for index, column in enumerate(columns):
            column = column.strip().rstrip(".")
            for pattern, field in (
                (_COMPANY_LABEL, "company"),
                (_ROLE_LABEL, "title"),
                (_LOCATION_LABEL, "location"),
            ):
                match = pattern.match(column)
                if match:
                    entry[field] = entry[field] or match.group("value").strip(" .")
                    column = ""
                    break
            duration = _DURATION_LABEL.match(column) if column else None
            if duration:
                column = duration.group("value")
            column = _take_dates(entry, column) if column else ""
            if not column:
                continue
            if index > 0 and not entry["location"] and _looks_like_location(column):
                entry["location"] = column
                continue
            texts.append((column, dated_row))

    leftover: list[str] = []
    if not entry["title"] and not entry["company"] and texts:
        title, company = _role_company(texts[0][0])
        if title or company:
            entry["title"], entry["company"] = title, company
            texts = texts[1:]
        elif len(texts) >= 2:
            entry["title"], entry["company"] = _title_and_company(
                texts[0][0], texts[1][0], texts[0][1], texts[1][1],
            )
            texts = texts[2:]
        elif _role_score(texts[0][0]) > 0:
            entry["title"] = texts[0][0]
            texts = []
        else:
            entry["company"] = texts[0][0]
            texts = []
    for text, _dated in texts:
        if not entry["title"] and _role_score(text) > 0:
            entry["title"] = text
        elif not entry["company"] and len(text) <= 80 and _role_score(text) <= 0 and not _looks_like_location(text):
            entry["company"] = text
        elif not entry["location"] and (_looks_like_location(text) or (len(text.split()) <= 3 and len(text) <= 30)):
            entry["location"] = text
        else:
            leftover.append(text)
    entry["description"] = "\n".join([*leftover, *notes]).strip()[:4000]
    return entry


def _parse_jobs(experience) -> list[dict]:
    """Split an experience section into roles using layout, not fixed wording.

    A role starts at a header row: a row with a date range, a row right before one,
    or a labelled Company/Role row after the previous role's description. Up to a few
    short rows after a header belong to the header (title, company, date, location).
    Bullets and wrapped bullet lines are the description.
    """
    rows = experience if isinstance(experience, list) else _resume_rows(experience)
    jobs: list[dict] = []
    headers: list[str] = []
    notes: list[str] = []
    bullet_indent = -1

    def flush() -> None:
        nonlocal headers, notes
        if headers or notes:
            job = _clean_job(_resolve_entry(headers, notes))
            if job["title"] and not job["company"] and jobs:
                # A second role listed under the same employer heading.
                job["company"] = jobs[-1]["company"]
            if _job_has_content(job):
                jobs.append(job)
        headers, notes = [], []

    def has_date(row: str) -> bool:
        return bool(_DATE_RANGE.search(row))

    for index, row in enumerate(rows):
        line = " ".join(row.split())
        indent = len(row) - len(row.lstrip())
        upcoming = " ".join(rows[index + 1].split()) if index + 1 < len(rows) else ""
        if _is_bullet(line):
            notes.append(_BULLET.sub("", line, count=1).strip())
            bullet_indent = indent
            continue
        dated = has_date(line)
        next_dated = bool(upcoming) and not _is_bullet(upcoming) and has_date(upcoming)
        if bullet_indent >= 0 and notes and not dated and not _labelled(line) and (
            line[:1].islower()
            or indent > bullet_indent
            or (not next_dated and not notes[-1].endswith((".", "!", "?", ":")))
        ):
            notes[-1] = f"{notes[-1]} {line}"
            continue
        bullet_indent = -1
        header_dated = any(has_date(header) for header in headers)
        starts_role = dated or next_dated or _labelled(line) or bool(_role_company(line)[0])
        if not headers and not notes:
            headers.append(row)
            continue
        if starts_role and (notes or (dated and header_dated)) and not (
            _labelled(line) and not notes
        ):
            flush()
            headers.append(row)
            continue
        if not notes and (
            _labelled(line)
            or (len(headers) < 2 and len(line) <= 110 and not line.endswith("."))
            or (len(headers) < 4 and (dated or _DATE_SINGLE.match(line) or _looks_like_location(line)
                                      or (len(line) <= 30 and len(line.split()) <= 3)))
        ):
            headers.append(row)
            continue
        notes.append(line)
    flush()
    return jobs[:12]


def _extract_total_experience(
    text: str,
    summary: str,
) -> str:

    search_text = summary or text

    patterns = (
        r"\b(?:over|more than)\s+(?P<value>\d+(?:\.\d+)?)\s+years?\b",
        r"\b(?P<value>\d+(?:\.\d+)?)\s*\+?\s+years?(?:\s+of)?\s+(?:professional\s+)?experience\b",
        r"\bexperience\s+of\s+(?P<value>\d+(?:\.\d+)?)\s*\+?\s+years?\b",
        r"\b(?P<article>an|one)\s+year\s+of\s+experience\b",
    )

    for pattern in patterns:

        match = re.search(pattern, search_text, re.I)

        if not match:
            continue

        if match.groupdict().get("value"):
            value = float(match.group("value"))

            if 0 < value <= 50:
                return f"{value:g} years"

        if match.groupdict().get("article"):
            return "1 year"

    return ""


def _extract_notice_period(text: str) -> str:
    patterns = (
        r"\bnotice period\s*[:\-]?\s*([^\n|,;]+)",
        r"\b(?:available|availability)\s*[:\-]?\s*([^\n|,;]+)",
        r"\b(immediate joiner)\b",
        r"\b(serving notice period)\b",
    )

    for pattern in patterns:
        match = re.search(pattern, text, re.I)

        if match:
            value = match.group(1).strip()
            return value[:120]

    return ""

def _skill_items(rows: list[str]) -> list[str]:
    """Items written in a skills section, including bullet grids that wrap into columns."""
    items: list[str] = []
    previous: list[tuple[int, int]] = []
    for row in rows:
        text = row.rstrip()
        label = re.match(r"^\s*[A-Za-z][A-Za-z /&()+\-]{1,40}:\s*", text)
        if label:
            text = " " * label.end() + text[label.end():]
        current: list[tuple[int, int]] = []
        bulleted_row = any(char in text for char in _BULLET_CHARS)
        for chunk in re.finditer(r"\S(?:.*?\S)?(?=\s{3,}|$)", text):
            pieces = re.split(rf"[{re.escape(_BULLET_CHARS)}]", chunk.group(0))
            offset = chunk.start()
            for position, piece in enumerate(pieces):
                piece = piece.strip()
                column = offset + chunk.group(0).find(piece) if piece else offset
                if not piece:
                    continue
                continued = bulleted_row and position == 0 and not chunk.group(0).lstrip()[:1] in _BULLET_CHARS
                if continued:
                    near = [index for col, index in previous if abs(col - column) <= 6]
                    if near:
                        items[near[0]] = f"{items[near[0]]} {piece}"
                        continue
                for part in _split_outside_parens(piece.replace(";", ",").replace(" | ", ",")):
                    part = part.strip(" .-–—")
                    if part and len(part) <= 40 and len(part.split()) <= 5:
                        current.append((column, len(items)))
                        items.append(part)
        previous = current or previous
    return items


def _job_recency(job: dict) -> tuple[int, int]:
    """Newest role first. A current role sorts above every finished one."""
    end = str(job.get("endDate") or "")
    if end.casefold() in {"present", "current", "now", "ongoing", "today"}:
        return (9999, 12)
    years = re.findall(r"(?:19|20)\d{2}", end)
    year = int(years[-1]) if years else 0
    month = 0
    named = re.search(_MONTH, end, re.I)
    if named:
        month = {
            "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
            "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
        }.get(named.group(0)[:3].casefold(), 0)
    else:
        numeric = re.match(r"\s*(0?[1-9]|1[0-2])\b", end)
        if numeric:
            month = int(numeric.group(1))
    return (year, month)


def _expand_glued_skill(item: str) -> list[str]:
    """Split one skills-list item when missing commas joined two known skills."""
    folded = item.casefold()
    spans: list[tuple[int, int, str]] = []
    for skill in sorted(_SKILL_TERMS, key=len, reverse=True):
        pattern = rf"(?<![a-z0-9+#]){re.escape(skill.casefold())}(?![a-z0-9+#])"
        for match in re.finditer(pattern, folded):
            if any(match.start() < end and match.end() > start for start, end, _name in spans):
                continue
            spans.append((match.start(), match.end(), item[match.start():match.end()]))
    if len(spans) < 2:
        return [item]
    spans.sort()
    pieces: list[str] = []
    cursor = 0
    for start, end, name in spans:
        gap = item[cursor:start]
        if "&" in gap or re.search(r"\band\b", gap, re.I):
            return [item]
        gap = gap.strip(" .,;/+-–—")
        if gap and not all(len(token) <= 3 for token in gap.split()):
            return [item]
        if gap and pieces:
            pieces[-1] = f"{pieces[-1]} {gap}"
        pieces.append(name)
        cursor = end
    tail = item[cursor:].strip(" .,;/+-–—&")
    if tail and not all(len(token) <= 3 for token in tail.split()):
        return [item]
    if tail and pieces:
        pieces[-1] = f"{pieces[-1]} {tail}"
    return pieces or [item]


def _extract_skills(skills_text, full_text: str) -> list[str]:
    rows = skills_text if isinstance(skills_text, list) else _resume_rows(skills_text)
    discovered = _skill_items(rows)
    written = " | ".join(discovered).casefold()
    source_text = "\n".join(rows) if rows else full_text
    folded = source_text.casefold()
    for skill in _SKILL_TERMS:
        pattern = rf"(?<![a-z0-9+#]){re.escape(skill.casefold())}(?![a-z0-9+#])"
        if re.search(pattern, folded) and not re.search(pattern, written):
            discovered.append(skill)
    seen: set[str] = set()
    result = []
    for item in discovered:
        for skill in _expand_glued_skill(item):
            key = re.sub(r"[\s.]+", "", skill.casefold())
            if key and key not in seen:
                seen.add(key)
                result.append(skill)
    return result[:60]


def _pdf_links(content: bytes) -> list[str]:
    """Link targets behind PDF text such as an icon labelled "linkedin" or "gmail"."""
    try:
        import pymupdf

        with pymupdf.open(stream=content, filetype="pdf") as document:
            return [link["uri"] for page in document for link in page.get_links() if link.get("uri")]
    except Exception:
        return []


def parse_resume(text: str, links: list[str] | tuple = ()) -> dict:

    lines = _clean_resume_lines(text)
    joined = "\n".join(lines)

    email_match = re.search(
        r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+",
        joined,
    )

    phone_match = re.search(
        r"(?<!\d)(?:\+?91[\s-]?)?[6-9]\d{9}(?!\d)",
        joined,
    )

    urls = re.findall(
        r"https?://[^\s|,;]+|(?:linkedin\.com|github\.com)/[^\s|,;]+",
        joined,
        re.I,
    )

    sections = _resume_sections(text)
    summary = _section_text(sections.get("summary", []))
    education = _section_text(sections.get("education", []))

    jobs = _parse_jobs(sections.get("experience", []))
    jobs.sort(key=_job_recency, reverse=True)
    current_title = jobs[0]["title"] if jobs else ""
    current_company = jobs[0]["company"] if jobs else ""

    skills = _extract_skills(
        sections.get("skills", []),
        joined,
    )
    urls.extend(str(link) for link in links or () if str(link).casefold().startswith(("http://", "https://")))
    urls.sort(key=lambda url: not url.casefold().startswith(("http://", "https://")))
    if not email_match:
        email_match = next(
            (re.search(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", str(link)) for link in links or () if "mailto:" in str(link)),
            None,
        )

    linkedin = next(
        (
            url.rstrip(".)]")
            for url in urls
            if "linkedin.com" in url.casefold()
        ),
        "",
    )

    github = next(
        (
            url.rstrip(".)]")
            for url in urls
            if "github.com" in url.casefold()
        ),
        "",
    )

    portfolio = next(
        (
            url.rstrip(".)]")
            for url in urls
            if "linkedin.com" not in url.casefold() and "github.com" not in url.casefold()
        ),
        "",
    )

    return {
        "fullName": _extract_name(lines),
        "email": email_match.group(0) if email_match else "",
        "phone": phone_match.group(0) if phone_match else "",
        "headline": current_title,
        "currentTitle": current_title,
        "currentCompany": current_company,
        "summary": summary,
        "totalExperience": _extract_total_experience(
            joined,
            summary,
        ),
        "noticePeriod": _extract_notice_period(joined),
        "skills": skills,
        "linkedin": linkedin,
        "github": github,
        "portfolio": portfolio,
        "education": education,
        "experienceHistory": jobs,
    }


def upload_resume(uid: str, payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("resume payload must be an object")

    uid = str(uid or "").strip()

    if not uid:
        raise ValueError("authenticated user UID is required")

    filename = Path(
        str(payload.get("filename") or "")
    ).name

    suffix = Path(filename).suffix.casefold()

    if suffix not in {".pdf", ".docx", ".txt"}:
        raise ValueError(
            "resume must be a PDF, DOCX, or TXT file"
        )

    encoded = payload.get("content")

    if not isinstance(encoded, str):
        raise ValueError("resume content is required")

    try:
        content = base64.b64decode(
            encoded,
            validate=True,
        )
    except (binascii.Error, ValueError) as exc:
        raise ValueError(
            "resume content must be valid base64"
        ) from exc

    if not content:
        raise ValueError("resume is empty")

    if len(content) > MAX_RESUME_BYTES:
        raise ValueError(
            "resume must be 8 MB or smaller"
        )

    event(
        "profile",
        "info",
        f"resume uploaded filename={filename} bytes={len(content)}",
    )
    text = _resume_text(content, suffix)

    if len(text.strip()) < 20:
        raise ValueError(
            "could not extract enough text from this resume"
        )

    extracted = parse_resume(text, _pdf_links(content) if suffix == ".pdf" else ())
    filled = [name for name, value in extracted.items() if value]
    event(
        "profile",
        "info",
        f"resume parsed filename={filename} fields={', '.join(filled) or 'none'}",
    )

    uploaded_at = datetime.now(
        timezone.utc
    ).isoformat()

    with _profile_lock:
        profile = read_profile(uid)

        # Update only fields that were successfully extracted.
        # Existing manually entered data remains unchanged when
        # the parser cannot identify a value.
        for field, value in extracted.items():
            if value:
                profile[field] = value

        profile["resume"] = {
            "filename": filename,
            "size": len(content),
            "uploadedAt": uploaded_at,
            "type": suffix.lstrip(".").upper(),
        }
        try:
            from Server.resume_analysis import analyze_if_enabled, remember_resume
            remember_resume(uid, text, filename, suffix.lstrip(".").upper())
        except Exception as exc:
            event("resume", "error", f"resume text save failed: {exc}")
        else:
            try:
                analyze_if_enabled(uid)
            except Exception as exc:
                event("resume", "error", f"auto analyse failed: {exc}")

        profile["updatedAt"] = uploaded_at
        profile.pop("completion", None)

        try:
            _profile_document(uid).set(profile)
        except Exception as exc:
            event(
                "profile",
                "error",
                (
                    f"resume profile save failed: {exc}"
                ),
            )
            raise ValueError(
                "could not save parsed resume profile"
            ) from exc

    event(
        "profile",
        "info",
        f"parsed details saved filename={filename}",
    )

    return {
        "profile": read_profile(uid),
        "extracted": extracted,
    }

SNAPSHOT_VERSION = 3


def _snapshot_current(saved: dict) -> bool:
    return (
        saved.get("windowDays") == WINDOW_DAYS
        and saved.get("version") == SNAPSHOT_VERSION
        and not saved.get("loading")
    )


def _portal_list() -> list[dict]:
    return _portal_catalog()


_LABEL_TO_KEY = {cls.label.casefold(): cls.key for cls in CONNECTORS}


def with_portal_key(record: dict) -> dict:
    if not isinstance(record, dict):
        return record
    key = (record.get("portalKey") or "").strip()
    if not key:
        key = _LABEL_TO_KEY.get((record.get("portal") or "").strip().casefold(), "")
    if not key or record.get("portalKey") == key:
        return record
    return {**record, "portalKey": key}


def with_note_key(note: dict) -> dict:
    if not isinstance(note, dict):
        return note
    key = (note.get("portalKey") or "").strip()
    if not key:
        key = _LABEL_TO_KEY.get((note.get("portal") or "").strip().casefold(), "")
    if not key or note.get("portalKey") == key:
        return note
    return {**note, "portalKey": key}


def _portal_catalog(live: bool = True) -> list[dict]:
    live_state = FEED_COORDINATOR.status() if live else {}
    main_running = live and bool(_progress.get("running"))
    catalog = []
    for cls in CONNECTORS:
        info = live_state.get(cls.key, {})
        if cls.key in PAUSED_SIDECAR_KEYS:
            status = "paused"
        elif live and info.get("running"):
            status = "running"
        elif live and getattr(cls, "persist", "") != "sidecar" and main_running:
            status = "running"
        elif info.get("error"):
            status = "error"
        else:
            status = "idle"
        catalog.append({
            "key": cls.key,
            "label": cls.label,
            "status": status,
            "warnings": list(info.get("warnings") or []),
            "error": info.get("error") or "",
            "seconds": info.get("seconds"),
        })
    return catalog


def _respond(payload: dict) -> dict:
    merged = FEED_COORDINATOR.merge(payload, CREDITS)
    merged["jobs"] = [with_portal_key(record) for record in merged.get("jobs") or []]
    merged["notes"] = [with_note_key(note) for note in merged.get("notes") or []]
    merged["portals"] = _portal_catalog()
    return merged


def _payload_from_progress(loading: bool, cached: bool = False) -> dict:
    jobs = list(_progress["jobs"])
    return {
        "source": "all",
        "query": "",
        "where": "",
        "limit": 0,
        "count": len(jobs),
        "jobs": jobs,
        "portals": _portal_list(),
        "credits": list(_progress["credits"]),
        "notes": list(_progress["notes"]),
        "windowDays": WINDOW_DAYS,
        "version": SNAPSHOT_VERSION,
        "loading": loading,
        "cached": cached,
        "fetchedAt": "" if loading else _progress["fetchedAt"],
        "error": _progress["error"],
        "stopped": bool(_progress.get("stopped")),
    }


def _remember(generation: int, connector, batch: list[Job], preferences: dict | None = None) -> None:
    store_cloud = False
    with _jobs_lock:
        if generation != _progress["generation"]:
            return
        for job in batch:
            record = job_record(job, include_portal=True, portal_key=connector.key)
            if preferences and not _matches_preferences(record, preferences):
                continue
            link = record.get("link") or ""
            if not link:
                continue
            position = _progress["index"].get(link)
            if position is None:
                _progress["index"][link] = len(_progress["jobs"])
                _progress["jobs"].append(record)
            else:
                _progress["jobs"][position] = record
        credit = CREDITS.get(connector.key)
        if credit and batch and credit not in _progress["credits"]:
            _progress["credits"].append(credit)
        for warning in connector.warnings:
            key = (connector.label, warning)
            if key in _progress["note_keys"]:
                continue
            _progress["note_keys"].add(key)
            _progress["notes"].append({
                "portal": connector.label,
                "portalKey": connector.key,
                "message": warning,
            })
        now = time.monotonic()
        if batch and now - _progress.get("last_write", 0.0) >= 0.6:
            if not _progress.get("owner"):
                write_jobs_file(_payload_from_progress(loading=not _progress.get("stopped")))
            _progress["last_write"] = now
            store_cloud = bool(_progress.get("owner"))
        else:
            store_cloud = False
    if store_cloud:
        _store_owner_results(force=False)


def _finish_fetch(generation: int, error: str = "") -> None:
    with _jobs_lock:
        if generation != _progress["generation"]:
            return
        _progress["running"] = False
        _progress["error"] = error
        _progress["fetchedAt"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        if error:
            _progress["notes"].append({"portal": "fetch", "portalKey": "", "message": error})
            event("jobs", "error", error)
        else:
            event("jobs", "info", f"company boards done jobs={len(_progress['jobs'])}")
        owner = str(_progress.get("owner") or "")
        payload = _payload_from_progress(loading=False)
        if not owner:
            write_jobs_file(payload)
    if owner:
        _store_owner_results(force=not _feeds_running())


def _portal_sources() -> list[type]:
    return [cls for cls in CONNECTORS if getattr(cls, "persist", "") != "sidecar"]


def _fetch_worker(country: str, generation: int, preferences: dict) -> None:
    event("jobs", "info", "company boards start")
    paused = set(preferences.get("pausedPortals") or [])
    sources = [cls for cls in _portal_sources() if cls.key not in paused]
    if not sources:
        _finish_fetch(generation)
        return
    try:
        run_sources(
            sources,
            query="",
            where="",
            limit=0,
            boards=None,
            country=country,
            open_browser=False,
            posted_within_days=preference_days(preferences),
            publish=lambda connector, batch: _remember(generation, connector, batch, preferences),
        )
    except Exception as exc:
        _finish_fetch(generation, error=str(exc))
        return
    _finish_fetch(generation)


def _reset_progress(uid: str = "") -> None:
    _progress["generation"] += 1
    _progress["running"] = True
    _progress["jobs"] = []
    _progress["index"] = {}
    _progress["notes"] = []
    _progress["note_keys"] = set()
    _progress["credits"] = []
    _progress["error"] = ""
    _progress["fetchedAt"] = ""
    _progress["last_write"] = 0.0
    _progress["last_cloud"] = 0.0
    _progress["stopped"] = False
    _progress["owner"] = str(uid or "")
    _progress["pausedPortals"] = []
    arm()
    FEED_COORDINATOR.suppressed = False


def stop_fetches(uid: str = "") -> dict:
    owner = str(_progress.get("owner") or "")
    uid = str(uid or "").strip()
    if owner and uid != owner:
        stored = read_user_jobs(uid) if uid else None
        return _respond_saved(stored or _blank_search())
    cancel()
    from connectors.Naukri.Naukri import close_naukri_browser

    close_naukri_browser()
    FEED_COORDINATOR.stop()
    with _jobs_lock:
        _progress["stopped"] = True
        _progress["running"] = False
        _progress["fetchedAt"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        note = {"portal": "fetch", "portalKey": "", "message": "Stopped. Showing jobs saved so far."}
        if note not in _progress["notes"]:
            _progress["notes"].append(note)
        payload = _payload_from_progress(loading=False)
        if not owner:
            write_jobs_file(payload)
    responded = _respond(payload)
    if owner:
        _store_owner_results(responded, force=True)
    return responded


def _start_main_fetch(country: str, refresh_sidecars: bool, preferences: dict, uid: str = "") -> dict:
    with _jobs_lock:
        _reset_progress(uid)
        _progress["pausedPortals"] = list(preferences.get("pausedPortals") or [])
        generation = _progress["generation"]
        payload = _payload_from_progress(loading=True)
    FEED_COORDINATOR.start_all(refresh_sidecars, _feed_preferences(preferences))
    threading.Thread(
        target=_fetch_worker,
        args=(country, generation, preferences),
        name="jobs-fetch",
        daemon=True,
    ).start()
    return payload


_JOB_PART_BYTES = 700_000
_job_search_memory: dict | None = None
_BUSY_NOTE = {
    "portal": "fetch",
    "portalKey": "",
    "message": "Another search is still running. Showing your last results.",
}


def _feeds_running() -> bool:
    return bool(FEED_COORDINATOR._running)


def _blank_search() -> dict:
    return {
        "source": "all",
        "query": "",
        "where": "",
        "limit": 0,
        "count": 0,
        "jobs": [],
        "credits": [],
        "notes": [],
        "windowDays": WINDOW_DAYS,
        "version": SNAPSHOT_VERSION,
        "loading": False,
        "cached": False,
        "fetchedAt": "",
        "error": "",
        "stopped": False,
    }


def _fetched_jobs_id(index: int) -> str:
    """First result document is jobs. Extra documents exist only past the size limit."""
    return "jobs" if index == 0 else f"jobs-{index + 1}"


def _chunk_jobs(jobs: list[dict]) -> list[list[dict]]:
    """Split a result list into pieces that fit in one Firestore document."""
    parts: list[list[dict]] = []
    current: list[dict] = []
    size = 2
    for job in jobs:
        extra = len(json.dumps(job, ensure_ascii=False).encode("utf-8")) + 1
        if current and size + extra > _JOB_PART_BYTES:
            parts.append(current)
            current = []
            size = 2
        current.append(job)
        size += extra
    if current:
        parts.append(current)
    return parts


def _search_record(payload: dict) -> dict:
    jobs = [dict(job) for job in payload.get("jobs") or [] if isinstance(job, dict)]
    return {
        "source": payload.get("source") or "all",
        "query": payload.get("query") or "",
        "where": payload.get("where") or "",
        "limit": payload.get("limit") or 0,
        "jobs": jobs,
        "credits": list(payload.get("credits") or []),
        "notes": [dict(note) for note in payload.get("notes") or [] if isinstance(note, dict)],
        "windowDays": payload.get("windowDays") or WINDOW_DAYS,
        "version": SNAPSHOT_VERSION,
        "loading": bool(payload.get("loading")),
        "cached": True,
        "fetchedAt": str(payload.get("fetchedAt") or ""),
        "error": str(payload.get("error") or ""),
        "stopped": bool(payload.get("stopped")),
        "count": len(jobs),
    }


def _job_search_collection(uid: str):
    return (
        get_firestore_client()
        .collection("users")
        .document(uid)
        .collection("jobSearch")
    )


def replace_user_jobs(uid: str, payload: dict) -> None:
    """Overwrite this user's search. The job list is stored in the jobs document."""
    uid = str(uid or "").strip()
    if not uid:
        return
    record = _search_record(payload)
    jobs = record.pop("jobs")
    if _job_search_memory is not None:
        _job_search_memory[uid] = {**record, "jobs": jobs}
        return
    chunks = _chunk_jobs(jobs)
    record["jobDocuments"] = len(chunks)
    collection = _job_search_collection(uid)
    previous_jobs = 0
    previous_parts = 0
    status = collection.document("status").get()
    if status.exists:
        stored = status.to_dict() or {}
        previous_jobs = int(stored.get("jobDocuments") or 0)
        previous_parts = int(stored.get("parts") or 0)
    collection.document("status").set(record)
    for index, chunk in enumerate(chunks):
        collection.document(_fetched_jobs_id(index)).set({"jobs": chunk})
    for index in range(len(chunks), max(previous_jobs, len(chunks))):
        collection.document(_fetched_jobs_id(index)).delete()
    for index in range(previous_parts):
        collection.document(f"part-{index}").delete()


def read_user_jobs(uid: str) -> dict | None:
    uid = str(uid or "").strip()
    if not uid:
        return None
    if _job_search_memory is not None:
        stored = _job_search_memory.get(uid)
        if stored is None:
            return None
        return {**stored, "jobs": [dict(job) for job in stored.get("jobs") or []]}
    try:
        status = _job_search_collection(uid).document("status").get()
    except Exception as exc:
        event("jobs", "error", f"search read failed: {exc}")
        return None
    if not status.exists:
        return None
    record = status.to_dict() or {}
    document_count = int(record.pop("jobDocuments", 0) or 0)
    legacy_parts = int(record.pop("parts", 0) or 0)
    jobs: list[dict] = []
    collection = _job_search_collection(uid)
    if document_count:
        names = [_fetched_jobs_id(index) for index in range(document_count)]
    else:
        names = [f"part-{index}" for index in range(legacy_parts)]
    for name in names:
        part = collection.document(name).get()
        chunk = (part.to_dict() or {}).get("jobs") if part.exists else []
        if isinstance(chunk, list):
            jobs.extend(item for item in chunk if isinstance(item, dict))
    if legacy_parts and not document_count and jobs:
        try:
            replace_user_jobs(uid, {**record, "jobs": jobs})
        except Exception as exc:
            event("jobs", "error", f"search rename failed: {exc}")
    record["jobs"] = jobs
    record["count"] = len(jobs)
    record["cached"] = True
    record["loading"] = False
    return record


def _respond_saved(payload: dict | None) -> dict:
    """A stored search, without mixing in another user's live portal files."""
    result = dict(payload or _blank_search())
    jobs = [with_portal_key(record) for record in result.get("jobs") or [] if isinstance(record, dict)]
    result["jobs"] = jobs
    result["count"] = len(jobs)
    result["notes"] = [with_note_key(note) for note in result.get("notes") or [] if isinstance(note, dict)]
    result["loading"] = False
    result["cached"] = True
    result["portals"] = _portal_catalog(live=False)
    result.setdefault("credits", [])
    result.setdefault("windowDays", WINDOW_DAYS)
    result.setdefault("version", SNAPSHOT_VERSION)
    result.setdefault("fetchedAt", "")
    result.setdefault("error", "")
    result.setdefault("stopped", False)
    result.setdefault("source", "all")
    return result


def _without_paused_jobs(payload: dict, paused: set[str]) -> dict:
    """Keep another user's active portal out of this user's saved search."""
    if not paused:
        return payload
    result = dict(payload)
    result["jobs"] = [
        job for job in result.get("jobs") or []
        if (job.get("portalKey") or "") not in paused
    ]
    result["count"] = len(result["jobs"])
    return result


def _store_owner_results(payload: dict | None = None, force: bool = False) -> None:
    uid = str(_progress.get("owner") or "")
    if not uid:
        return
    now = time.monotonic()
    if not force and now - float(_progress.get("last_cloud") or 0) < 15:
        return
    if payload is None:
        loading = bool(_progress.get("running")) or _feeds_running()
        payload = _respond(_payload_from_progress(loading=loading))
    payload = _without_paused_jobs(payload, set(_progress.get("pausedPortals") or []))
    jobs = payload.get("jobs") or []
    if payload.get("loading") and not jobs:
        return
    try:
        replace_user_jobs(uid, payload)
    except Exception as exc:
        event("jobs", "error", f"search save failed: {exc}")
        return
    _progress["last_cloud"] = now
    if not payload.get("loading"):
        event("jobs", "info", f"search saved jobs={len(jobs)}")


def _after_portal(_key: str) -> None:
    if not _progress.get("owner"):
        return
    boards = bool(_progress.get("running"))
    _store_owner_results(force=not boards and not _feeds_running())


FEED_COORDINATOR.after_portal = _after_portal


def _user_search(country: str, refresh: bool, preferences: dict, uid: str) -> dict:
    with _jobs_lock:
        owner = str(_progress.get("owner") or "")
        boards_running = bool(_progress.get("running"))
        has_jobs = bool(_progress.get("jobs"))
    running = boards_running or _feeds_running()
    mine = owner == uid and (running or has_jobs)

    if mine and refresh and running:
        return _respond(_payload_from_progress(loading=True))
    if mine and refresh and not running:
        return _respond(_start_main_fetch(country, True, preferences, uid))
    if mine:
        payload = _respond(_payload_from_progress(loading=running))
        _store_owner_results(payload, force=not running)
        return payload

    if running and owner != uid:
        stored = read_user_jobs(uid) or _blank_search()
        nothing_saved = not stored.get("jobs") and not stored.get("fetchedAt")
        if refresh or nothing_saved:
            notes = list(stored.get("notes") or [])
            if _BUSY_NOTE not in notes:
                notes.append(dict(_BUSY_NOTE))
            stored = {**stored, "notes": notes}
        return _respond_saved(stored)

    stored = read_user_jobs(uid)
    if not refresh and stored is not None and _snapshot_current(stored):
        return _respond_saved(stored)
    return _respond(_start_main_fetch(country, bool(refresh), preferences, uid))


def saved_or_live_jobs(country: str, refresh: bool, preferences: dict, uid: str = "") -> dict:
    uid = str(uid or "").strip()
    if uid:
        return _user_search(country, refresh, preferences, uid)
    if _progress.get("owner"):
        saved = dict(read_jobs_file() or _blank_search())
        saved["jobs"] = list(saved.get("jobs") or [])
        return _respond_saved(saved)
    if refresh:
        payload = _start_main_fetch(country, True, preferences)
        return _respond(payload)

    saved = read_jobs_file()
    if held() or FEED_COORDINATOR.suppressed or _progress.get("stopped") or (saved or {}).get("stopped"):
        FEED_COORDINATOR.suppressed = True
        with _jobs_lock:
            if _progress.get("stopped") or _progress["jobs"]:
                payload = _payload_from_progress(loading=False)
            elif saved is not None:
                saved["cached"] = True
                saved["loading"] = False
                saved["stopped"] = True
                saved["portals"] = _portal_list()
                payload = saved
            else:
                payload = _payload_from_progress(loading=False)
        return _respond(payload)

    with _jobs_lock:
        if _progress["running"]:
            payload = _payload_from_progress(loading=True)
            running = True
        else:
            payload = None
            running = False
    if running:
        FEED_COORDINATOR.start_all(False, _feed_preferences(preferences))
        return _respond(payload)

    if saved is not None and _snapshot_current(saved):
        FEED_COORDINATOR.start_all(False, _feed_preferences(preferences))
        saved["cached"] = True
        saved["loading"] = False
        saved["portals"] = _portal_list()
        return _respond(saved)

    payload = _start_main_fetch(country, False, preferences)
    return _respond(payload)


def _current_view() -> dict:
    with _jobs_lock:
        live = _progress["running"] or bool(_progress["jobs"])
        payload = _payload_from_progress(loading=_progress["running"]) if live else None
    if payload is None:
        payload = read_jobs_file() or _payload_from_progress(loading=False)
    return _respond(payload)


def jobs_summary(uid: str = "") -> dict:
    uid = str(uid or "").strip()
    owner = str(_progress.get("owner") or "")
    live = bool(_progress.get("running") or _progress.get("jobs") or _feeds_running())
    if uid and owner == uid and live:
        view = _current_view()
    elif uid:
        stored = read_user_jobs(uid)
        view = _respond_saved(stored if stored is not None else _blank_search())
    else:
        view = _current_view()
    counts: dict[str, int] = {}
    new_today = 0
    for record in view.get("jobs") or []:
        key = record.get("portalKey") or ""
        if key:
            counts[key] = counts.get(key, 0) + 1
        posted = record.get("added on") or ""
        if posted and within_days(posted, 1):
            new_today += 1
    sources = [
        {"key": portal["key"], "label": portal["label"], "status": portal["status"], "count": counts[portal["key"]]}
        for portal in view.get("portals") or []
        if counts.get(portal["key"])
    ]
    sources.sort(key=lambda source: source["count"], reverse=True)
    saved_count = 0
    if uid:
        try:
            saved_count = len(read_saved(uid))
        except ValueError:
            saved_count = 0
    return {
        "sourcesConnected": len(sources),
        "sources": sources,
        "total": len(view.get("jobs") or []),
        "newToday": new_today,
        "saved": saved_count,
        "loading": bool(view.get("loading")),
        "fetchedAt": view.get("fetchedAt") or "",
    }


class JobsApiHandler(BaseHTTPRequestHandler):
    """JSON API for the web app. Jobs are read-only; saved jobs accept POST and DELETE."""

    max_body = 12 * 1024 * 1024
    server_version = "fetchJobsForMe"
    country = "in"

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._send_cors()
        self.end_headers()

    def do_GET(self) -> None:
        route = urlparse(self.path)
        params = parse_qs(route.query)
        if route.path == "/api/health":
            self._send_json(200, {"status": "ok"})
            return
        if route.path == "/api/resume/analysis":
            uid = self._user_id("resume")
            if not uid:
                return
            try:
                from Server.resume_analysis import analysis_view
                self._send_json(200, analysis_view(uid))
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
            return
        if route.path == "/api/resume/download":
            uid = self._user_id("resume")
            if not uid:
                return
            try:
                from Server.resume_analysis import download_resume
                body, filename, mime = download_resume(uid)
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            self._send_file(body, mime, filename)
            return
        if route.path == "/api/jobs/stop":
            event("jobs", "info", "stop requested")
            uid = ""
            try:
                uid = uid_from_authorization(self.headers.get("Authorization"))
            except (ValueError, RuntimeError, FileNotFoundError):
                uid = ""
            self._send_json(200, stop_fetches(uid))
            return
        if route.path == "/api/portals":
            self._send_json(200, {"portals": _portal_catalog()})
            return
        if route.path == "/api/saved":
            uid = self._user_id("saved")
            if not uid:
                return
            try:
                saved = read_saved(uid)
            except ValueError as exc:
                self._send_json(502, {"error": str(exc)})
                return
            self._send_json(200, {"jobs": saved, "count": len(saved)})
            return
        if route.path == "/api/summary":
            uid = ""
            try:
                uid = uid_from_authorization(self.headers.get("Authorization"))
            except (ValueError, RuntimeError, FileNotFoundError):
                uid = ""
            self._send_json(200, jobs_summary(uid))
            return
        if route.path == "/api/profile":
            try:
                uid = uid_from_authorization(self.headers.get("Authorization"))
            except ValueError as exc:
                event("profile", "warning", f"profile read rejected: {exc}")
                self._send_json(401, {"error": str(exc)})
                return
            except (RuntimeError, FileNotFoundError) as exc:
                event("profile", "error", f"profile read failed: {exc}")
                self._send_json(500, {"error": str(exc)})
                return
            try:
                profile = read_profile(uid)
            except ValueError as exc:
                cause = exc.__cause__ or exc
                self._send_json(502, {"error": f"{exc}: {cause}"})
                return
            event("profile", "info", f"profile loaded completion={profile.get('completion')}")
            self._send_json(200, {"profile": profile})
            return
        if route.path == "/api/preferences":
            uid = self._user_id("preferences")
            if not uid:
                return
            self._send_json(200, {"preferences": read_preferences(uid)})
            return
        if route.path != "/api/jobs":
            self._send_json(404, {"error": f"unknown path {route.path}"})
            return

        first = lambda name, fallback="": (params.get(name) or [fallback])[0].strip()
        refresh = first("refresh", "").casefold() in {"1", "true", "yes"}
        uid = ""
        try:
            uid = uid_from_authorization(self.headers.get("Authorization"))
        except (ValueError, RuntimeError, FileNotFoundError):
            uid = ""
        preferences = read_preferences(uid) if uid else _empty_preferences()
        try:
            payload = saved_or_live_jobs(
                country=first("country", self.country) or self.country,
                refresh=refresh,
                preferences=preferences,
                uid=uid,
            )
            payload = apply_preferences(payload, preferences)
        except SystemExit as exc:
            self._send_json(400, {"error": str(exc)})
            return
        except Exception as exc:
            event("api", "error", f"GET /api/jobs failed: {exc}")
            self._send_json(502, {"error": str(exc)})
            return
        self._send_json(200, payload)

    def do_POST(self) -> None:
        path = urlparse(self.path).path

        # Gemini AI Generation Endpoint
        if path == "/api/ai/generate":
            auth_header = self.headers.get("Authorization")
            try:
                client, uid = get_user_gemini_client(auth_header)
            except ValueError as exc:
                event("gemini", "warning", f"generate rejected: {exc}")
                self._send_json(401, {"error": str(exc)})
                return
            except Exception as exc:
                event("gemini", "error", f"generate auth failed: {exc}")
                self._send_json(500, {"error": f"Auth check failed: {exc}"})
                return

            body = self._read_json()
            if body is None:
                return

            prompt = str(body.get("prompt") or "").strip()
            if not prompt:
                self._send_json(400, {"error": "prompt is required"})
                return

            event("gemini", "info", f"generate started chars={len(prompt)}")
            try:
                response = client.models.generate_content(
                    model="gemini-3.8-flash",
                    contents=prompt,
                )
                event("gemini", "info", "generate done")
                self._send_json(200, {"result": response.text})
            except Exception as exc:
                event("gemini", "error", f"generation failed: {exc}")
                self._send_json(502, {"error": f"Gemini error: {exc}"})
            return

        if path in {"/api/resume/analyze", "/api/resume/compare", "/api/resume/rewrite"}:
            self._resume_action(path)
            return

        if path == "/api/resume":
            body = self._read_json()
            if body is None:
                return
            filename = Path(str(body.get("filename") or "")).name if isinstance(body, dict) else ""
            try:
                uid = uid_from_authorization(self.headers.get("Authorization"))
            except ValueError as exc:
                event("profile", "warning", f"resume upload rejected filename={filename or '-'}: {exc}")
                self._send_json(401, {"error": str(exc)})
                return
            except (RuntimeError, FileNotFoundError) as exc:
                event("profile", "error", f"resume upload failed filename={filename or '-'}: {exc}")
                self._send_json(500, {"error": str(exc)})
                return
            try:
                result = upload_resume(uid, body)
            except ValueError as exc:
                event("profile", "error", f"resume upload failed filename={filename or '-'}: {exc}")
                self._send_json(400, {"error": str(exc)})
                return
            self._send_json(200, result)
            return

        if path != "/api/saved":
            self._send_json(404, {"error": f"unknown path {path}"})
            return
        body = self._read_json()
        if body is None:
            return
        uid = self._user_id("saved")
        if not uid:
            return
        try:
            job = save_job(uid, body.get("job") if isinstance(body, dict) else None)
        except ValueError as exc:
            if exc.__cause__ is None:
                event("saved", "error", f"save failed: {exc}")
            self._send_json(400, {"error": str(exc)})
            return
        self._send_json(201, {"job": job, "count": len(read_saved(uid))})

    def do_PUT(self) -> None:
        route = urlparse(self.path)
        path = route.path
        if path not in {"/api/profile", "/api/preferences"}:
            self._send_json(404, {"error": f"unknown path {path}"})
            return
        body = self._read_json()
        if body is None:
            return
        if path == "/api/preferences":
            uid = self._user_id("preferences")
            if not uid:
                return
            try:
                preferences = update_preferences(
                    uid,
                    body.get("preferences") if isinstance(body, dict) else None,
                )
            except ValueError as exc:
                event("preferences", "error", f"update failed: {exc}")
                self._send_json(400, {"error": str(exc)})
                return
            self._send_json(200, {"preferences": preferences})
            return
        try:
            uid = uid_from_authorization(self.headers.get("Authorization"))
        except ValueError as exc:
            event("profile", "warning", f"profile update rejected: {exc}")
            self._send_json(401, {"error": str(exc)})
            return
        except (RuntimeError, FileNotFoundError) as exc:
            event("profile", "error", f"profile update failed: {exc}")
            self._send_json(500, {"error": str(exc)})
            return
        try:
            profile = update_profile(uid, body.get("profile") if isinstance(body, dict) else None)
        except ValueError as exc:
            cause = exc.__cause__
            if cause is None:
                event("profile", "error", f"profile update failed: {exc}")
            self._send_json(400, {"error": f"{exc}: {cause}" if cause else str(exc)})
            return
        self._send_json(200, {"profile": profile})

    def do_DELETE(self) -> None:
        route = urlparse(self.path)
        if route.path != "/api/saved":
            self._send_json(404, {"error": f"unknown path {route.path}"})
            return
        link = (parse_qs(route.query).get("link") or [""])[0].strip()
        if not link:
            self._send_json(400, {"error": "link is required"})
            return
        uid = self._user_id("saved")
        if not uid:
            return
        try:
            removed = unsave_job(uid, link)
        except ValueError as exc:
            self._send_json(502, {"error": str(exc)})
            return
        if not removed:
            event("saved", "warning", f"remove missed {link}")
        self._send_json(200 if removed else 404, {"removed": removed, "count": len(read_saved(uid))})

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > self.max_body:
            self._send_json(413, {"error": "request body too large"})
            return None
        try:
            return json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, {"error": "body must be JSON"})
            return None

    def _resume_action(self, path: str) -> None:
        try:
            client, uid = get_user_gemini_client(self.headers.get("Authorization"))
        except ValueError as exc:
            message = str(exc)
            expired = "authorization" in message.casefold() or "token" in message.casefold() or "sign in" in message.casefold()
            self._send_json(401 if expired else 400, {"error": message})
            return
        except Exception as exc:
            self._send_json(500, {"error": f"Auth check failed: {exc}"})
            return
        from Server.resume_analysis import analyze_resume, compare_resumes, rewrite_resume
        try:
            if path == "/api/resume/analyze":
                body = self._read_json()
                if body is None:
                    return
                force = bool(body.get("force")) if isinstance(body, dict) else False
                payload = analyze_resume(uid, client, force=force)
            elif path == "/api/resume/compare":
                body = self._read_json()
                if body is None:
                    return
                filename = Path(str(body.get("filename") or "")).name if isinstance(body, dict) else ""
                encoded = body.get("content") if isinstance(body, dict) else ""
                try:
                    content = base64.b64decode(encoded or "", validate=True)
                except (binascii.Error, ValueError) as exc:
                    raise ValueError("resume content must be valid base64") from exc
                if len(content) > MAX_RESUME_BYTES:
                    raise ValueError("resume must be 8 MB or smaller")
                payload = compare_resumes(uid, client, filename, content)
            elif path == "/api/resume/rewrite":
                payload = rewrite_resume(uid, client)
            else:
                payload = analyze_resume(uid, client)
        except ValueError as exc:
            event("resume", "error", f"{path} failed: {exc}")
            self._send_json(400, {"error": str(exc)})
            return
        except Exception as exc:
            event("resume", "error", f"{path} failed: {exc}")
            self._send_json(502, {"error": f"Gemini error: {exc}"})
            return
        self._send_json(200, payload)

    def _user_id(self, area: str) -> str:
        try:
            return uid_from_authorization(self.headers.get("Authorization"))
        except ValueError as exc:
            event(area, "warning", f"rejected: {exc}")
            self._send_json(401, {"error": str(exc)})
        except (RuntimeError, FileNotFoundError) as exc:
            event(area, "error", f"failed: {exc}")
            self._send_json(500, {"error": str(exc)})
        return ""

    def log_message(self, fmt: str, *args) -> None:
        message = fmt % args
        message = re.sub(r"([?&])uid=[^&\s\"]*", r"\1", message)
        message = message.replace("?&", "?").replace("&&", "&")
        message = re.sub(r"[?&](?=\s|HTTP)", "", message)
        event("api", "info", f"{self.address_string()} {message}")

    def _send_cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")

    def _send_file(self, body: bytes, mime: str, filename: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self._send_cors()
        self.end_headers()
        self._write_body(body)

    def _write_body(self, body: bytes) -> None:
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            event("api", "info", "client closed the connection before the response finished")

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._send_cors()
        self.end_headers()
        self._write_body(body)


def serve(host: str, port: int, country: str) -> None:
    enable()
    handler = type("JobsApiHandler", (JobsApiHandler,), {"country": country})
    httpd = ThreadingHTTPServer((host, port), handler)
    display_host = "127.0.0.1" if host == "0.0.0.0" else host
    event("api", "info", f"listening on http://{display_host}:{port}")
    print(f"fetchJobsForMe API on http://{display_host}:{port}")
    print(f"  GET http://{display_host}:{port}/api/jobs?source=all&query=engineer")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        event("api", "info", "stopped")
        print("\nstopped")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()