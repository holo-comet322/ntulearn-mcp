"""Walking a course's content tree and finding every downloadable file in it."""

from __future__ import annotations

import hashlib
import html as htmllib
import json
import os
import re
from dataclasses import dataclass, field
from typing import Iterator
from urllib.parse import unquote, urlparse

from bs4 import BeautifulSoup

from .client import BbApiError, BlackboardClient

FOLDER_HANDLERS = {"resource/x-bb-folder", "resource/x-bb-lesson", "resource/x-bb-module-page"}
# Content types whose /attachments endpoint can hold files.
ATTACHMENT_HANDLERS = {
    "resource/x-bb-file",
    "resource/x-bb-document",
    "resource/x-bb-assignment",
    "resource/x-bb-asmt-test-link",
    "resource/x-bb-blankpage",
}
FRIENDLY_TYPE = {
    "resource/x-bb-folder": "folder",
    "resource/x-bb-lesson": "learning module",
    "resource/x-bb-module-page": "module page",
    "resource/x-bb-file": "file",
    "resource/x-bb-document": "document",
    "resource/x-bb-externallink": "link",
    "resource/x-bb-assignment": "assignment",
    "resource/x-bb-asmt-test-link": "test/quiz",
    "resource/x-bb-blankpage": "page",
    "resource/x-bb-forumlink": "discussion",
    "resource/x-bb-blti-link": "external tool (LTI)",
    "resource/x-bb-toollink": "tool link",
}


def handler_of(item: dict) -> str:
    return (item.get("contentHandler") or {}).get("id", "")


def friendly_type(item: dict) -> str:
    h = handler_of(item)
    return FRIENDLY_TYPE.get(h, h.replace("resource/x-bb-", "") or "unknown")


def is_container(item: dict) -> bool:
    return handler_of(item) in FOLDER_HANDLERS or bool(item.get("hasChildren"))


def html_to_text(body: str | None) -> str:
    if not body:
        return ""
    soup = BeautifulSoup(body, "html.parser")
    for a in soup.find_all("a"):
        href = a.get("href")
        text = a.get_text(" ", strip=True)
        if href and href not in text:
            a.replace_with(f"{text} ({href})" if text else href)
    for br in soup.find_all("br"):
        br.replace_with("\n")
    text = soup.get_text("\n")
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


@dataclass
class RemoteFile:
    course_id: str
    content_id: str
    content_title: str
    filename: str
    url: str  # API path or absolute URL to GET
    folder: tuple[str, ...] = field(default_factory=tuple)
    attachment_id: str | None = None
    size: int | None = None
    mime: str | None = None
    modified: str | None = None
    kind: str = "attachment"  # or "embedded"

    @property
    def key(self) -> str:
        # Embedded-file URLs carry signed query params that change on every request,
        # so identify them by path only.
        stable = self.url.split("?", 1)[0]
        suffix = self.attachment_id or hashlib.sha1(stable.encode()).hexdigest()[:12]
        return f"{self.content_id}:{suffix}"

    def to_dict(self) -> dict:
        return {
            "filename": self.filename,
            "folder": "/".join(self.folder),
            "content_id": self.content_id,
            "content_title": self.content_title,
            "attachment_id": self.attachment_id,
            "url": self.url if self.kind == "embedded" else None,
            "size_bytes": self.size,
            "mime": self.mime,
            "modified": self.modified,
        }


def walk(
    client: BlackboardClient,
    course_id: str,
    parent_id: str | None = None,
    max_depth: int | None = None,
    path: tuple[str, ...] = (),
    errors: list[str] | None = None,
) -> Iterator[tuple[tuple[str, ...], dict]]:
    """Yield (folder_path, item) for every content item, depth-first."""
    try:
        items = client.contents(course_id, parent_id)
    except BbApiError as e:
        # Folders hidden by release conditions return 403/404 to students.
        if errors is not None:
            where = "/".join(path) or "(course root)"
            errors.append(f"{where}: not accessible ({e.status}) - probably hidden or not yet released")
        return
    for it in items:
        yield path, it
        if is_container(it) and (max_depth is None or len(path) + 1 < max_depth):
            yield from walk(client, course_id, it["id"], max_depth, path + (it.get("title") or it["id"],), errors)


_EXT_OK = re.compile(r"^\.[A-Za-z0-9]{1,8}$")


def _embedded_links(body: str | None, base_url: str) -> list[tuple[str, str]]:
    """Find files embedded in an HTML body (Ultra documents put inline files here)."""
    if not body:
        return []
    soup = BeautifulSoup(body, "html.parser")
    found: list[tuple[str, str]] = []
    seen = set()
    for tag in soup.find_all(["a", "img", "source", "embed", "iframe"]):
        href = tag.get("href") or tag.get("src") or ""
        if "/bbcswebdav/" not in href:
            continue
        name = None
        meta = tag.get("data-bbfile")
        if meta:
            try:
                info = json.loads(htmllib.unescape(meta))
                name = info.get("linkName") or info.get("displayName") or info.get("fileName")
            except (ValueError, AttributeError):
                pass
        if not name and tag.name == "a":
            name = tag.get_text(" ", strip=True) or None
        tail = unquote(os.path.basename(urlparse(href).path))
        if not name or not _EXT_OK.match(os.path.splitext(name)[1] or ""):
            # Prefer a name with an extension; the URL tail often has one.
            if _EXT_OK.match(os.path.splitext(tail)[1] or ""):
                name = name + os.path.splitext(tail)[1] if name else tail
            name = name or tail or "file"
        url = href if href.startswith("http") else base_url + href
        if url not in seen:
            seen.add(url)
            found.append((name, url))
    return found


def files_for_item(client: BlackboardClient, course_id: str, folder: tuple[str, ...], item: dict) -> list[RemoteFile]:
    h = handler_of(item)
    title = item.get("title") or item["id"]
    out: list[RemoteFile] = []
    # A plain file item lives directly in its folder; anything that can hold
    # several files (document, assignment, ...) gets its own subfolder.
    # (Ultra often puts a document called "Tutorial 1" inside a folder called "Tutorial 1"; don't nest twice.)
    if h == "resource/x-bb-file" or (folder and folder[-1].strip().lower() == title.strip().lower()):
        target = folder
    else:
        target = folder + (title,)
    names_seen = set()
    if h in ATTACHMENT_HANDLERS:
        try:
            atts = client.attachments(course_id, item["id"])
        except BbApiError:
            atts = []
        for a in atts:
            fname = a.get("fileName") or title
            names_seen.add(fname.lower())
            out.append(
                RemoteFile(
                    course_id=course_id,
                    content_id=item["id"],
                    content_title=title,
                    filename=fname,
                    url=client.attachment_download_path(course_id, item["id"], a["id"]),
                    folder=target,
                    attachment_id=a["id"],
                    size=a.get("size") if isinstance(a.get("size"), int) else None,
                    mime=a.get("mimeType"),
                    modified=item.get("modified"),
                )
            )
    body = item.get("body")
    if body is None and h in ("resource/x-bb-document", "resource/x-bb-blankpage"):
        try:
            body = client.content(course_id, item["id"]).get("body")
        except BbApiError:
            body = None
    for name, url in _embedded_links(body, client.base_url):
        if name.lower() in names_seen:
            continue
        names_seen.add(name.lower())
        out.append(
            RemoteFile(
                course_id=course_id,
                content_id=item["id"],
                content_title=title,
                filename=name,
                url=url,
                folder=target,
                modified=item.get("modified"),
                kind="embedded",
            )
        )
    return out


def collect_files(
    client: BlackboardClient,
    course_id: str,
    folder_id: str | None = None,
    errors: list[str] | None = None,
) -> list[RemoteFile]:
    files: list[RemoteFile] = []
    for folder, item in walk(client, course_id, folder_id, errors=errors):
        files.extend(files_for_item(client, course_id, folder, item))
    return files
