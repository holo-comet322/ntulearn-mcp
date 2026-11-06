"""Session handling.

Blackboard's official REST API needs an application key that only the
university's admins can approve, so a student can't get one. Instead we do
what the NTULearn web app itself does: log in through NTU SSO in a real
browser window, keep the resulting session cookies, and call the same
/learn/api/public/... endpoints with them.
"""

from __future__ import annotations

import json
import os
import sys
import time
from urllib.parse import urlparse

from .config import settings


def _log(msg: str) -> None:
    # stdout is reserved for the MCP protocol, so always log to stderr.
    print(msg, file=sys.stderr, flush=True)


def _host() -> str:
    return urlparse(settings.base_url).hostname or ""


def _cookie_matches_host(cookie: dict) -> bool:
    domain = (cookie.get("domain") or "").lstrip(".").lower()
    host = _host().lower()
    return bool(domain) and (host == domain or host.endswith("." + domain))


def save_cookies(cookies: list[dict]) -> int:
    keep = [
        {k: c.get(k) for k in ("name", "value", "domain", "path", "expires", "secure", "httpOnly")}
        for c in cookies
        if _cookie_matches_host(c)
    ]
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    tmp = settings.cookie_file.with_suffix(".tmp")
    tmp.write_text(json.dumps({"saved_at": time.time(), "cookies": keep}, indent=2), encoding="utf-8")
    os.replace(tmp, settings.cookie_file)
    try:
        os.chmod(settings.cookie_file, 0o600)
    except OSError:
        pass
    return len(keep)


def parse_cookie_header(header: str) -> list[dict]:
    """Turn a 'name=value; name2=value2' string (copied from DevTools) into cookie dicts."""
    header = header.strip()
    if header.lower().startswith("cookie:"):
        header = header[7:]
    out = []
    for part in header.split(";"):
        if "=" not in part:
            continue
        name, value = part.strip().split("=", 1)
        if name:
            out.append({"name": name.strip(), "value": value.strip(), "domain": _host(), "path": "/"})
    return out


def load_cookies() -> list[dict]:
    env = os.environ.get("NTULEARN_COOKIES")
    if env:
        return parse_cookie_header(env)
    if not settings.cookie_file.exists():
        return []
    try:
        data = json.loads(settings.cookie_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return data.get("cookies", [])


def cookies_saved_at() -> float | None:
    try:
        return json.loads(settings.cookie_file.read_text(encoding="utf-8")).get("saved_at")
    except (OSError, json.JSONDecodeError):
        return None


def clear_cookies() -> None:
    try:
        settings.cookie_file.unlink()
    except FileNotFoundError:
        pass


def interactive_login(timeout_s: int = 300) -> dict:
    """Open a browser window, let the user sign in through NTU SSO, save the cookies.

    Uses Microsoft Edge (always present on Windows) or Chrome if available, so no
    separate browser download is needed. Falls back to Playwright's Chromium.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("Playwright is not installed. Run: uv sync") from e

    me_url = settings.base_url + "/learn/api/public/v1/users/me"
    settings.profile_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        ctx = None
        errors = []
        for channel in ("msedge", "chrome", None):
            try:
                kwargs = {"headless": False, "viewport": None}
                if channel:
                    kwargs["channel"] = channel
                ctx = p.chromium.launch_persistent_context(str(settings.profile_dir), **kwargs)
                break
            except Exception as e:  # noqa: BLE001
                errors.append(f"{channel or 'chromium'}: {e}")
        if ctx is None:
            raise RuntimeError(
                "Could not start a browser for login. Install Edge/Chrome or run "
                "'uv run playwright install chromium'.\n" + "\n".join(errors)
            )
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(settings.base_url + "/ultra/course", wait_until="domcontentloaded")
            _log("A browser window has opened. Sign in to NTULearn there (NTU SSO / MFA).")
            deadline = time.time() + timeout_s
            while time.time() < deadline:
                try:
                    r = ctx.request.get(me_url, fail_on_status_code=False, max_redirects=0)
                    if r.status == 200 and "json" in (r.headers.get("content-type") or ""):
                        user = r.json()
                        n = save_cookies(ctx.cookies())
                        return {
                            "ok": True,
                            "user": user.get("userName"),
                            "name": " ".join(
                                filter(None, [user.get("name", {}).get("given"), user.get("name", {}).get("family")])
                            ),
                            "cookies_saved": n,
                        }
                except Exception:  # noqa: BLE001 - page may be mid-navigation
                    pass
                time.sleep(2)
            raise TimeoutError(f"Login not completed within {timeout_s}s.")
        finally:
            try:
                ctx.close()
            except Exception:  # noqa: BLE001
                pass
