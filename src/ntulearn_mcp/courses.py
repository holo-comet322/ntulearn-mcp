"""Course listing and lookup by code / name."""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone

from .client import BlackboardClient

_INTERNAL_ID = re.compile(r"^_\d+_\d+$")
_cache: dict[str, tuple[float, list[dict]]] = {}
CACHE_TTL = 600


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def summarize(m: dict) -> dict:
    c = m.get("course") or {}
    return {
        "id": c.get("id") or m.get("courseId"),
        "code": c.get("courseId"),
        "name": c.get("name"),
        "role": m.get("courseRoleId"),
        "is_organization": bool(c.get("organization")),
        "ultra": c.get("ultraStatus") == "Ultra",
        "available": (c.get("availability") or {}).get("available"),
        "term_id": c.get("termId"),
        "created": c.get("created"),
        "last_accessed": m.get("lastAccessed"),
        "url": c.get("externalAccessUrl"),
    }


def list_courses(client: BlackboardClient, refresh: bool = False) -> list[dict]:
    key = client.me()["id"]
    hit = _cache.get(key)
    if hit and not refresh and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    courses = [summarize(m) for m in client.memberships()]
    courses.sort(key=lambda c: c.get("last_accessed") or c.get("created") or "", reverse=True)
    _cache[key] = (time.time(), courses)
    return courses


def is_recent(course: dict, days: int = 240) -> bool:
    now = datetime.now(timezone.utc)
    created = _parse_dt(course.get("created"))
    accessed = _parse_dt(course.get("last_accessed"))
    return bool(
        (created and now - created < timedelta(days=days))
        or (accessed and now - accessed < timedelta(days=45))
    )


def resolve_course(client: BlackboardClient, query: str) -> dict:
    """Accept an internal id (_123_1), a course code (SC1007) or part of the name."""
    query = query.strip()
    courses = list_courses(client)
    if _INTERNAL_ID.match(query):
        for c in courses:
            if c["id"] == query:
                return c
        return {"id": query, "code": query, "name": query}
    q = query.lower()
    exact = [c for c in courses if (c.get("code") or "").lower() == q]
    if len(exact) == 1:
        return exact[0]
    matches = [
        c for c in courses if q in (c.get("code") or "").lower() or q in (c.get("name") or "").lower()
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ValueError(f"No enrolled course matches '{query}'. Use list_courses to see your courses.")
    # Prefer the most recent if several runs of the same course match.
    recent = [c for c in matches if is_recent(c)]
    if len(recent) == 1:
        return recent[0]
    options = ", ".join(f"{c['code']} ({c['id']}): {c['name']}" for c in matches[:10])
    raise ValueError(f"'{query}' matches several courses; use the id instead. Matches: {options}")
