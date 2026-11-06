"""Bulk download / incremental sync of course materials."""

from __future__ import annotations

import fnmatch
import html as htmllib
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .client import BlackboardClient, NotAuthenticated
from .content import RemoteFile, collect_files

MANIFEST = ".ntulearn-manifest.json"
_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def safe_name(name: str | None, maxlen: int = 80) -> str:
    """Make a string safe as a single Windows/macOS/Linux path component."""
    s = htmllib.unescape(name or "").strip()
    s = _BAD.sub("_", s)
    s = re.sub(r"\s+", " ", s).strip(" .")
    if not s:
        s = "untitled"
    stem, ext = os.path.splitext(s)
    if not re.fullmatch(r"\.[A-Za-z0-9]{1,8}", ext or ""):
        stem, ext = s, ""
    if len(s) > maxlen:
        s = stem[: maxlen - len(ext)].rstrip(" .") + ext
        stem = os.path.splitext(s)[0] if ext else s
    if stem.upper() in _RESERVED:
        s = "_" + s
    return s


_VALID_EXT = re.compile(r"\.[A-Za-z0-9]{1,8}$")


def sniff_extension(path: Path) -> str:
    """Guess a file's extension from its first bytes (NTULearn often omits it)."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(8192)
    except OSError:
        return ""
    if head.startswith(b"%PDF"):
        return ".pdf"
    if head.startswith(b"PK\x03\x04"):
        import zipfile

        try:
            names = zipfile.ZipFile(path).namelist()
        except zipfile.BadZipFile:
            return ".zip"
        if any(n.startswith("ppt/") for n in names):
            return ".pptx"
        if any(n.startswith("word/") for n in names):
            return ".docx"
        if any(n.startswith("xl/") for n in names):
            return ".xlsx"
        return ".zip"
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        with open(path, "rb") as fh:
            blob = fh.read(2_000_000)
        for marker, ext in (("PowerPoint Document", ".ppt"), ("WordDocument", ".doc"), ("Workbook", ".xls")):
            if marker.encode("utf-16-le") in blob:
                return ext
        return ".doc"
    for magic, ext in ((b"\x89PNG", ".png"), (b"\xff\xd8\xff", ".jpg"), (b"GIF8", ".gif")):
        if head.startswith(magic):
            return ext
    if head[4:8] == b"ftyp":
        return ".mp4"
    return ""


def ensure_extension(path: Path) -> Path:
    """If path has no real extension, add one based on its content (renaming the file)."""
    if _VALID_EXT.search(path.name) or not path.exists():
        return path
    ext = sniff_extension(path)
    if not ext:
        return path
    new = path.with_name(path.name + ext)
    n = 2
    while new.exists():
        new = path.with_name(f"{path.name} ({n}){ext}")
        n += 1
    os.replace(path, new)
    return new


def course_dir_name(course: dict) -> str:
    code = course.get("code") or ""
    name = course.get("name") or course.get("id") or "course"
    label = name if code and code.lower() in name.lower() else f"{code} - {name}" if code else name
    return safe_name(label, 70)


def _matches(name: str, patterns: list[str] | None) -> bool:
    n = name.lower()
    return any(fnmatch.fnmatch(n, p.lower()) for p in patterns or [])


@dataclass
class SyncReport:
    dest: str
    dry_run: bool = False
    courses: list[str] = field(default_factory=list)
    planned: int = 0
    downloaded: int = 0
    skipped_unchanged: int = 0
    skipped_filtered: int = 0
    failed: int = 0
    bytes: int = 0
    current: str | None = None
    errors: list[str] = field(default_factory=list)
    files: list[dict] = field(default_factory=list)  # filled in dry runs
    status: str = "running"
    started: float = field(default_factory=time.time)
    finished: float | None = None

    def to_dict(self, max_items: int = 200) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k not in ("files", "errors")}
        d["mb"] = round(self.bytes / 1e6, 2)
        d["errors"] = self.errors[:50]
        if self.files:
            d["files"] = self.files[:max_items]
            if len(self.files) > max_items:
                d["files_truncated"] = len(self.files) - max_items
        return d


def _load_manifest(course_dir: Path) -> dict:
    try:
        return json.loads((course_dir / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_manifest(course_dir: Path, manifest: dict) -> None:
    course_dir.mkdir(parents=True, exist_ok=True)
    tmp = course_dir / (MANIFEST + ".tmp")
    tmp.write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    os.replace(tmp, course_dir / MANIFEST)


def plan_paths(files: list[RemoteFile], course_dir: Path, manifest: dict) -> list[tuple[RemoteFile, Path]]:
    used: set[str] = set()
    out = []
    for rf in files:
        prev = manifest.get(rf.key, {}).get("path")
        if prev:
            rel = prev
        else:
            parts = [safe_name(p, 60) for p in rf.folder] + [safe_name(rf.filename, 100)]
            rel = "/".join(parts)
            base, ext = os.path.splitext(rel)
            n = 2
            while rel.lower() in used:
                rel = f"{base} ({n}){ext}"
                n += 1
        used.add(rel.lower())
        out.append((rf, course_dir / rel))
    return out


def sync(
    client: BlackboardClient,
    courses: list[dict],
    dest_root: Path,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    max_file_mb: float | None = None,
    dry_run: bool = False,
    workers: int = 4,
    report: SyncReport | None = None,
    log: Callable[[str], None] | None = None,
) -> SyncReport:
    """Download every file in the given courses into dest_root/<course>/<folders>/.

    Re-running is cheap: files already downloaded and unchanged on NTULearn are skipped.
    """
    report = report or SyncReport(dest=str(dest_root), dry_run=dry_run)
    report.dry_run = dry_run
    log = log or (lambda _m: None)
    lock = threading.Lock()
    try:
        for course in courses:
            label = course_dir_name(course)
            report.courses.append(label)
            report.current = f"scanning {label}"
            log(f"Scanning {label} ...")
            course_dir = dest_root / label
            manifest = _load_manifest(course_dir)
            for entry in manifest.values():  # fix files saved without an extension by older versions
                old = course_dir / entry["path"]
                new = ensure_extension(old)
                if new != old:
                    entry["path"] = new.relative_to(course_dir).as_posix()
            scan_errors: list[str] = []
            files = collect_files(client, course["id"], errors=scan_errors)
            report.errors.extend(f"{label}: {e}" for e in scan_errors)

            todo: list[tuple[RemoteFile, Path]] = []
            for rf, path in plan_paths(files, course_dir, manifest):
                if (include and not _matches(rf.filename, include)) or _matches(rf.filename, exclude):
                    report.skipped_filtered += 1
                    continue
                if max_file_mb and rf.size and rf.size > max_file_mb * 1e6:
                    report.skipped_filtered += 1
                    continue
                entry = manifest.get(rf.key)
                if entry and path.exists() and entry.get("modified") == rf.modified:
                    report.skipped_unchanged += 1
                    continue
                if not entry and path.exists() and rf.size and path.stat().st_size == rf.size:
                    manifest[rf.key] = {"path": path.relative_to(course_dir).as_posix(), "modified": rf.modified}
                    report.skipped_unchanged += 1
                    continue
                todo.append((rf, path))
            report.planned += len(todo)
            log(f"  {len(files)} files found, {len(todo)} to download")

            if dry_run:
                for rf, path in todo:
                    report.files.append({**rf.to_dict(), "would_save_to": str(path)})
                continue

            def fetch(job: tuple[RemoteFile, Path]) -> tuple[RemoteFile, Path, int]:
                rf, path = job
                n = client.download_to(rf.url, path)
                return rf, ensure_extension(path), n

            with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
                futures = [pool.submit(fetch, j) for j in todo]
                for fut in as_completed(futures):
                    try:
                        rf, path, n = fut.result()
                    except NotAuthenticated:
                        for f in futures:
                            f.cancel()
                        raise
                    except Exception as e:  # noqa: BLE001
                        with lock:
                            report.failed += 1
                            report.errors.append(f"{label}: {e}")
                        continue
                    with lock:
                        report.downloaded += 1
                        report.bytes += n
                        report.current = f"{label}: {rf.filename}"
                        manifest[rf.key] = {
                            "path": path.relative_to(course_dir).as_posix(),
                            "modified": rf.modified,
                            "size": n,
                        }
                    log(f"  ✓ {path.relative_to(dest_root)}")
            _save_manifest(course_dir, manifest)
        report.status = "done"
    except NotAuthenticated as e:
        report.status = "error"
        report.errors.append(str(e))
    except Exception as e:  # noqa: BLE001
        report.status = "error"
        report.errors.append(f"{type(e).__name__}: {e}")
    finally:
        report.current = None
        report.finished = time.time()
    return report
