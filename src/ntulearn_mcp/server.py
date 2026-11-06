"""MCP server exposing NTULearn (Blackboard Learn) to Claude."""

from __future__ import annotations

import hashlib
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server

from . import auth
from .client import BbApiError, BlackboardClient, NotAuthenticated
from .config import settings
from .content import collect_files, files_for_item, friendly_type, html_to_text, is_container, walk
from .courses import is_recent, list_courses as _list_courses, resolve_course
from .downloader import SyncReport, ensure_extension, safe_name, sync
from .extract import extract_text

mcp = _Server(
    "ntulearn",
    instructions=(
        "Tools for NTU's NTULearn (Blackboard Learn). Courses can be referred to by internal id "
        "(_12345_1), course code (e.g. SC1007) or part of the name. If a tool says the session "
        "expired, call ntulearn_login and ask the user to sign in in the browser window that opens. "
        "For grabbing many files, use download_course_materials (runs in the background) and poll "
        "download_status; use dry_run first if the user wants to see what would be fetched."
    ),
)
client = BlackboardClient()
_jobs: dict[str, SyncReport] = {}


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _course_id(course: str) -> str:
    return resolve_course(client, course)["id"]


# ---------------------------------------------------------------- session
@mcp.tool()
def ntulearn_auth_status() -> dict:
    """Check whether there is a working NTULearn session, and who it belongs to."""
    saved = auth.cookies_saved_at()
    try:
        me = client.me()
        return {
            "logged_in": True,
            "user": me.get("userName"),
            "name": " ".join(filter(None, [me.get("name", {}).get("given"), me.get("name", {}).get("family")])),
            "session_saved_at": datetime.fromtimestamp(saved).isoformat() if saved else None,
            "base_url": settings.base_url,
        }
    except NotAuthenticated as e:
        return {"logged_in": False, "message": str(e), "base_url": settings.base_url}


@mcp.tool()
def ntulearn_login(timeout_seconds: int = 300) -> dict:
    """Open a browser window on the user's computer so they can sign in to NTULearn via NTU SSO.

    Blocks until sign-in finishes (or times out), then saves the session for the other tools.
    Tell the user to look for the new browser window before calling this.
    """
    # Playwright's sync API refuses to run on the server's asyncio thread, so use a worker thread.
    box: dict = {}

    def run() -> None:
        try:
            box["result"] = auth.interactive_login(timeout_s=timeout_seconds)
        except Exception as e:  # noqa: BLE001
            box["error"] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout_seconds + 60)
    client._me = None
    if "error" in box:
        raise box["error"]
    return box.get("result", {"ok": False, "message": "Login timed out."})


# ---------------------------------------------------------------- courses
@mcp.tool()
def list_courses(include_past: bool = False, include_organizations: bool = False, refresh: bool = False) -> list[dict]:
    """List the user's NTULearn courses (most recently accessed first).

    By default only current/recent courses are shown; set include_past=True for everything.
    """
    courses = _list_courses(client, refresh=refresh)
    if not include_organizations:
        courses = [c for c in courses if not c["is_organization"]]
    if not include_past:
        recent = [c for c in courses if is_recent(c)]
        courses = recent or courses
    return courses


@mcp.tool()
def browse_course(course: str, folder_id: str | None = None, depth: int = 1) -> dict:
    """Show a course's content tree (folders, files, documents, assignments, links).

    course: id, code or name. folder_id: start inside this folder (from an earlier listing).
    depth: how many folder levels to expand (1 = just this level, max 6).
    """
    c = resolve_course(client, course)
    depth = max(1, min(depth, 6))
    errors: list[str] = []
    rows = []
    for path, item in walk(client, c["id"], folder_id, max_depth=depth, errors=errors):
        row = {
            "path": "/".join(path),
            "title": item.get("title"),
            "type": friendly_type(item),
            "id": item["id"],
        }
        if is_container(item):
            row["has_children"] = True
        h = (item.get("contentHandler") or {})
        if h.get("url"):
            row["link"] = h["url"]
        due = ((item.get("availability") or {}).get("adaptiveRelease") or {}).get("end")
        if due:
            row["available_until"] = due
        rows.append(row)
    return {"course": c.get("name"), "course_id": c["id"], "items": rows, "errors": errors or None}


@mcp.tool()
def get_content(course: str, content_id: str) -> dict:
    """Read one content item: its text/description plus the files attached or embedded in it."""
    cid = _course_id(course)
    item = client.content(cid, content_id)
    files = files_for_item(client, cid, (), item)
    out = {
        "title": item.get("title"),
        "type": friendly_type(item),
        "text": html_to_text(item.get("body") or item.get("description")),
        "modified": item.get("modified"),
        "files": [f.to_dict() for f in files],
    }
    h = item.get("contentHandler") or {}
    if h.get("url"):
        out["link"] = h["url"]
    if h.get("gradeColumnId"):
        out["grade_column_id"] = h["gradeColumnId"]
    return out


@mcp.tool()
def list_course_files(course: str, folder_id: str | None = None, name_filter: str | None = None) -> dict:
    """List every downloadable file in a course (or one folder of it), with sizes and folder paths.

    name_filter: optional case-insensitive substring, e.g. 'lecture' or '.pdf'.
    """
    c = resolve_course(client, course)
    errors: list[str] = []
    files = collect_files(client, c["id"], folder_id, errors=errors)
    if name_filter:
        q = name_filter.lower()
        files = [f for f in files if q in f.filename.lower() or q in "/".join(f.folder).lower()]
    total = sum(f.size or 0 for f in files)
    return {
        "course": c.get("name"),
        "count": len(files),
        "total_mb_known": round(total / 1e6, 1),
        "files": [f.to_dict() for f in files],
        "errors": errors or None,
    }


@mcp.tool()
def read_course_file(
    course: str,
    content_id: str,
    attachment_id: str | None = None,
    url: str | None = None,
    start_char: int = 0,
    max_chars: int = 40000,
) -> dict:
    """Download one file (into a local cache) and return its text so you can read it.

    Works for PDF, PPTX, DOCX, HTML and plain-text/code files. Identify the file with the
    content_id + attachment_id (or the 'url' for embedded files) from list_course_files/get_content.
    Long files are paged: call again with start_char to continue.
    """
    cid = _course_id(course)
    if not attachment_id and not url:
        item = client.content(cid, content_id)
        files = files_for_item(client, cid, (), item)
        if len(files) != 1:
            raise ValueError(
                f"Item has {len(files)} files; pass attachment_id or url. Files: "
                + ", ".join(f"{f.filename} (attachment_id={f.attachment_id}, url={f.url if f.kind=='embedded' else None})" for f in files)
            )
        rf = files[0]
        attachment_id, url, filename = rf.attachment_id, (rf.url if rf.kind == "embedded" else None), rf.filename
    else:
        filename = None
    if attachment_id:
        dl = client.attachment_download_path(cid, content_id, attachment_id)
        if not filename:
            for a in client.attachments(cid, content_id):
                if a["id"] == attachment_id:
                    filename = a.get("fileName")
    else:
        dl = url  # type: ignore[assignment]
        filename = filename or Path(dl.split("?")[0]).name
    key = hashlib.sha1(f"{cid}:{content_id}:{attachment_id}:{url}".encode()).hexdigest()[:16]
    path = settings.cache_dir / key / safe_name(filename or "file")
    done = [p for p in path.parent.glob(path.name + "*") if not p.name.endswith(".part")] if path.parent.exists() else []
    if done:
        path = done[0]
    else:
        client.download_to(dl, path)
        path = ensure_extension(path)
    text = extract_text(path)
    chunk = text[start_char : start_char + max_chars]
    end = start_char + len(chunk)
    return {
        "filename": filename,
        "local_path": str(path),
        "total_chars": len(text),
        "start_char": start_char,
        "next_start_char": end if end < len(text) else None,
        "text": chunk,
    }


# ------------------------------------------------------------ bulk download
@mcp.tool()
def download_course_materials(
    courses: list[str] | None = None,
    all_current_courses: bool = False,
    dest_dir: str | None = None,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    max_file_mb: float | None = None,
    dry_run: bool = False,
) -> dict:
    """Bulk-download course files onto the user's computer, mirroring NTULearn's folder structure.

    courses: list of ids/codes/names, or set all_current_courses=True.
    dest_dir: defaults to ~/NTULearn (or NTULEARN_DOWNLOAD_DIR). Each course gets its own folder.
    include / exclude: filename globs, e.g. include=["*.pdf","*.pptx"], exclude=["*.mp4"].
    max_file_mb: skip files bigger than this (when size is known).
    dry_run: only list what would be downloaded.
    Re-running only fetches new or changed files. Runs in the background: returns a job_id
    to poll with download_status.
    """
    if all_current_courses:
        targets = [c for c in _list_courses(client) if not c["is_organization"] and is_recent(c)]
    elif courses:
        targets = [resolve_course(client, q) for q in courses]
    else:
        raise ValueError("Pass courses=[...] or all_current_courses=True.")
    dest = Path(dest_dir).expanduser() if dest_dir else settings.download_dir
    job_id = uuid.uuid4().hex[:8]
    report = SyncReport(dest=str(dest), dry_run=dry_run)
    _jobs[job_id] = report

    def run() -> None:
        sync(client, targets, dest, include, exclude, max_file_mb, dry_run, report=report,
             log=lambda m: print(m, file=sys.stderr, flush=True))

    threading.Thread(target=run, daemon=True, name=f"ntulearn-dl-{job_id}").start()
    return {
        "job_id": job_id,
        "courses": [f"{c.get('code')}: {c.get('name')}" for c in targets],
        "dest": str(dest),
        "dry_run": dry_run,
        "note": "Poll download_status(job_id) for progress.",
    }


@mcp.tool()
def download_status(job_id: str | None = None) -> dict:
    """Progress/result of a download_course_materials job (latest job if job_id is omitted)."""
    if not _jobs:
        return {"message": "No download jobs have been started in this session."}
    report = _jobs.get(job_id) if job_id else list(_jobs.values())[-1]
    if report is None:
        raise ValueError(f"Unknown job_id {job_id}. Known: {', '.join(_jobs)}")
    return report.to_dict()


# ------------------------------------------------------- announcements etc.
@mcp.tool()
def get_announcements(course: str | None = None, days: int = 14, max_items: int = 30) -> list[dict]:
    """Recent announcements for one course, or across all current courses if course is omitted."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    targets = [resolve_course(client, course)] if course else [
        c for c in _list_courses(client) if is_recent(c) and not c["is_organization"]
    ]
    out = []
    for c in targets:
        try:
            anns = client.announcements(c["id"])
        except BbApiError:
            continue
        for a in anns:
            when = a.get("modified") or a.get("created") or ""
            try:
                dt = datetime.fromisoformat(when.replace("Z", "+00:00"))
            except ValueError:
                dt = None
            if dt and dt < since:
                continue
            out.append({
                "course": c.get("code") or c.get("name"),
                "title": a.get("title"),
                "posted": a.get("created"),
                "modified": a.get("modified"),
                "text": html_to_text(a.get("body")),
            })
    out.sort(key=lambda a: a.get("modified") or a.get("posted") or "", reverse=True)
    return out[:max_items]


@mcp.tool()
def get_upcoming(days: int = 14) -> list[dict]:
    """Upcoming calendar items and due dates across all courses for the next `days` days (max 112)."""
    now = datetime.now(timezone.utc)
    items = client.calendar_items(_iso(now - timedelta(hours=1)), _iso(now + timedelta(days=min(days, 112))))
    out = [
        {
            "title": i.get("title"),
            "course_or_calendar": i.get("calendarName"),
            "type": i.get("type"),
            "start": i.get("start"),
            "end": i.get("end"),
            "description": html_to_text(i.get("description")) or None,
            "location": i.get("location"),
        }
        for i in items
    ]
    out.sort(key=lambda i: i.get("end") or i.get("start") or "")
    return out


@mcp.tool()
def get_grades(course: str) -> dict:
    """The user's own grades/marks for a course (only items the instructor has released)."""
    c = resolve_course(client, course)
    grades = client.my_grades(c["id"])
    names: dict[str, dict] = {}
    try:
        for col in client.gradebook_columns(c["id"]):
            names[col["id"]] = col
    except BbApiError:
        pass
    rows = []
    for g in grades:
        col = names.get(g.get("columnId"))
        if col is None:
            try:
                col = client.gradebook_column(c["id"], g["columnId"])
            except BbApiError:
                col = {}
        disp = g.get("displayGrade") or {}
        # v2 puts the number in displayGrade.score; v1 puts it at top level.
        score = g.get("score") if g.get("score") is not None else disp.get("score")
        possible = disp.get("possible") or (col.get("score") or {}).get("possible")
        text = disp.get("text") or g.get("text")
        if not text and score is not None and disp.get("scaleType") == "Percent":
            text = f"{score:g}%"
        elif not text and score is not None and possible and score > possible:
            # Some columns report a percentage without saying so (e.g. 100 on a 50-point quiz).
            text = f"{score:g}% (reported as a percentage)"
        if not text and score is not None:
            text = f"{score:g}/{possible:g}" if possible else f"{score:g}"
            if possible:
                text += f" ({100 * score / possible:.0f}%)"
        rows.append({
            "item": col.get("name") or g.get("columnId"),
            "score": score,
            "out_of": possible,
            "display": text,
            "status": g.get("status"),
            "due": (col.get("grading") or {}).get("due"),
            "feedback": html_to_text(g.get("feedback")) or None,
            "exempt": g.get("exempt") or None,
        })
    return {"course": c.get("name"), "grades": rows}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
