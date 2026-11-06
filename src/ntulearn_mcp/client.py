"""Thin client for the Blackboard Learn REST API, authenticated with session cookies."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any, Iterator

import logging

import httpx

logging.getLogger("httpx").setLevel(logging.WARNING)

from .auth import load_cookies
from .config import settings

PUB = "/learn/api/public"


class NotAuthenticated(Exception):
    """The NTULearn session is missing or has expired."""

    def __init__(self, msg: str | None = None):
        super().__init__(
            msg
            or "Not logged in to NTULearn (or the session expired). Run the `ntulearn_login` tool "
            "(or `uv run ntulearn login` in a terminal) and sign in, then try again."
        )


class BbApiError(Exception):
    def __init__(self, status: int, path: str, message: str):
        self.status = status
        self.path = path
        super().__init__(f"Blackboard API {status} on {path}: {message}")


def _is_login_page(resp: httpx.Response) -> bool:
    url = str(resp.url).lower()
    return "/webapps/login" in url or "login.microsoftonline" in url or "/auth-saml/" in url


class BlackboardClient:
    def __init__(self, base_url: str | None = None):
        self.base_url = (base_url or settings.base_url).rstrip("/")
        self._http: httpx.Client | None = None
        self._cookie_sig: tuple | None = None
        self._lock = threading.Lock()
        self._me: dict | None = None

    # ------------------------------------------------------------------ http
    @property
    def http(self) -> httpx.Client:
        cookies = load_cookies()
        sig = tuple(sorted((c["name"], c["value"]) for c in cookies))
        with self._lock:
            if self._http is None or sig != self._cookie_sig:
                if not cookies:
                    raise NotAuthenticated()
                jar = httpx.Cookies()
                for c in cookies:
                    jar.set(c["name"], c["value"], domain=c.get("domain") or "", path=c.get("path") or "/")
                if self._http is not None:
                    self._http.close()
                self._http = httpx.Client(
                    cookies=jar,
                    headers={"Accept": "application/json", "User-Agent": "ntulearn-mcp/0.1"},
                    timeout=httpx.Timeout(30.0, read=60.0),
                    follow_redirects=False,
                )
                self._cookie_sig = sig
                self._me = None
            return self._http

    def url(self, path_or_url: str) -> str:
        if path_or_url.startswith(("http://", "https://")):
            return path_or_url
        return self.base_url + (path_or_url if path_or_url.startswith("/") else "/" + path_or_url)

    def get(self, path: str, params: dict | None = None) -> Any:
        for attempt in range(3):
            resp = self.http.get(self.url(path), params=params)
            if resp.status_code == 429 or resp.status_code >= 500:
                time.sleep(1.5 * (attempt + 1))
                continue
            break
        if resp.status_code == 401 or resp.is_redirect:
            raise NotAuthenticated()
        if resp.status_code >= 400:
            try:
                msg = resp.json().get("message") or resp.text[:200]
            except ValueError:
                msg = resp.text[:200]
            raise BbApiError(resp.status_code, path, msg)
        if "json" not in resp.headers.get("content-type", ""):
            raise NotAuthenticated()
        return resp.json()

    def paginate(self, path: str, params: dict | None = None, limit: int | None = None) -> Iterator[dict]:
        params = dict(params or {})
        params.setdefault("limit", 100)
        count = 0
        next_url: str | None = path
        first = True
        while next_url:
            data = self.get(next_url, params if first else None)
            first = False
            for item in data.get("results", []):
                yield item
                count += 1
                if limit and count >= limit:
                    return
            next_url = (data.get("paging") or {}).get("nextPage")

    # ------------------------------------------------------------- download
    def download_to(self, path_or_url: str, dest: Path) -> int:
        """Stream a file to dest (atomically via a .part file). Returns bytes written."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        written = 0
        with self.http.stream(
            "GET",
            self.url(path_or_url),
            follow_redirects=True,
            timeout=httpx.Timeout(30.0, read=300.0),
            headers={"Accept": "*/*"},
        ) as resp:
            if resp.status_code == 401 or _is_login_page(resp):
                raise NotAuthenticated()
            if resp.status_code >= 400:
                raise BbApiError(resp.status_code, path_or_url, "download failed")
            with open(tmp, "wb") as fh:
                for chunk in resp.iter_bytes(1 << 16):
                    fh.write(chunk)
                    written += len(chunk)
        os.replace(tmp, dest)
        return written

    # ------------------------------------------------------------ endpoints
    def me(self) -> dict:
        self.http  # reloads cookies and clears the cached user if they changed
        if self._me is None:
            self._me = self.get(f"{PUB}/v1/users/me")
        return self._me

    def memberships(self) -> list[dict]:
        uid = self.me()["id"]
        items = list(self.paginate(f"{PUB}/v1/users/{uid}/courses", {"expand": "course"}))
        for m in items:
            if not isinstance(m.get("course"), dict):
                try:
                    m["course"] = self.course(m["courseId"])
                except BbApiError:
                    m["course"] = {"id": m["courseId"], "name": m["courseId"]}
        return items

    def course(self, course_id: str) -> dict:
        return self.get(f"{PUB}/v3/courses/{course_id}")

    def contents(self, course_id: str, parent_id: str | None = None) -> list[dict]:
        path = (
            f"{PUB}/v1/courses/{course_id}/contents/{parent_id}/children"
            if parent_id
            else f"{PUB}/v1/courses/{course_id}/contents"
        )
        return list(self.paginate(path))

    def content(self, course_id: str, content_id: str) -> dict:
        return self.get(f"{PUB}/v1/courses/{course_id}/contents/{content_id}")

    def attachments(self, course_id: str, content_id: str) -> list[dict]:
        return list(self.paginate(f"{PUB}/v1/courses/{course_id}/contents/{content_id}/attachments"))

    def attachment_download_path(self, course_id: str, content_id: str, attachment_id: str) -> str:
        return f"{PUB}/v1/courses/{course_id}/contents/{content_id}/attachments/{attachment_id}/download"

    def announcements(self, course_id: str) -> list[dict]:
        return list(self.paginate(f"{PUB}/v1/courses/{course_id}/announcements"))

    def calendar_items(self, since_iso: str, until_iso: str) -> list[dict]:
        return list(self.paginate(f"{PUB}/v1/calendars/items", {"since": since_iso, "until": until_iso}))

    def gradebook_columns(self, course_id: str) -> list[dict]:
        return list(self.paginate(f"{PUB}/v2/courses/{course_id}/gradebook/columns"))

    def gradebook_column(self, course_id: str, column_id: str) -> dict:
        return self.get(f"{PUB}/v2/courses/{course_id}/gradebook/columns/{column_id}")

    def my_grades(self, course_id: str) -> list[dict]:
        uid = self.me()["id"]
        last: Exception | None = None
        for ver in ("v2", "v1"):
            try:
                return list(self.paginate(f"{PUB}/{ver}/courses/{course_id}/gradebook/users/{uid}"))
            except BbApiError as e:
                last = e
                if e.status not in (404, 400):
                    break
        raise last  # type: ignore[misc]
