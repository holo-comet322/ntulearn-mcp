# ntulearn-mcp

An MCP server (like canvas-mcp, but for **NTULearn**, which runs on Blackboard Learn) so Claude can
see your courses, read lecture notes, check announcements, due dates and grades, and
**bulk-download every file in your courses** into tidy folders on your computer.

It also comes with a plain command-line tool (`ntulearn`) for doing the big downloads yourself.

---

## How it gets into NTULearn (and why it's done this way)

Blackboard does have a REST API, but the "official" route needs an application key that NTU's
admins must register and approve. Students can't get one. So this tool does what the NTULearn
website itself does:

1. It opens a browser window (Microsoft Edge, which every Windows PC has) at NTULearn.
2. **You** sign in normally with NTU SSO + MFA. Your password never touches this tool.
3. Once you're in, it saves the session cookies to `C:\Users\<you>\.ntulearn-mcp\cookies.json`
   and uses them to call the same `/learn/api/public/...` endpoints the site uses.

Think of it like a visitor pass: you show your matric card at the front desk once, and the
pass gets you through the doors until it expires. When it expires (usually after a few hours of
no use), just log in again.

> Keep `cookies.json` private. Anyone with it can act as you on NTULearn until it expires.
> Use `ntulearn logout` to delete it.

---

## Setup (Windows, about 5 minutes)

**1. Install `uv`** (a fast Python manager; it also fetches Python for you). In PowerShell:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Close and reopen PowerShell afterwards.

**2. Install the project's dependencies:**

```powershell
cd C:\ntulearn-mcp
uv sync
```

**3. Log in once to test it:**

```powershell
uv run ntulearn login
uv run ntulearn courses
```

If `courses` prints your modules, everything works.

**4. Connect it to Claude.**

*Claude Desktop*: open **Settings → Developer → Edit Config** (this opens
`%APPDATA%\Claude\claude_desktop_config.json`) and add:

```json
{
  "mcpServers": {
    "ntulearn": {
      "command": "uv",
      "args": ["--directory", "C:\\ntulearn-mcp", "run", "ntulearn-mcp"]
    }
  }
}
```

If Claude says it can't find `uv`, replace `"uv"` with the full path, usually
`"C:\\Users\\<you>\\.local\\bin\\uv.exe"`. Restart Claude Desktop completely afterwards.

*Claude Code*:

```powershell
claude mcp add ntulearn -- uv --directory C:\ntulearn-mcp run ntulearn-mcp
```

---

## What Claude can do with it

| Tool | What it does |
|---|---|
| `ntulearn_auth_status` | Am I logged in? As whom? |
| `ntulearn_login` | Opens the sign-in window (when the session has expired) |
| `list_courses` | Your current courses (or all of them with `include_past`) |
| `browse_course` | The folder tree of a course: folders, files, documents, links, assignments |
| `get_content` | The text of one item plus its attached/embedded files |
| `list_course_files` | Every downloadable file in a course, with folder paths |
| `read_course_file` | Downloads one PDF/PPTX/DOCX and gives Claude its text to read |
| `download_course_materials` | **Bulk download** one, several, or all current courses (runs in the background) |
| `download_status` | Progress of a bulk download |
| `get_announcements` | Recent announcements, per course or across all courses |
| `get_upcoming` | Upcoming due dates and calendar events |
| `get_grades` | Your released marks for a course |

You can name courses by code (`SC1007`), part of the name (`data structures`) or internal id (`_12345_1`).

Things to try:

- "What's due in the next two weeks on NTULearn?"
- "Download all the lecture slides for my current courses, PDFs and PPTX only."
- "Read the week 5 lecture for MH1810 and explain it step by step."
- "Any new announcements this week?"

---

## Bulk downloads

Files are saved to `C:\Users\<you>\NTULearn\<Course>\<same folders as on NTULearn>\`.

- **Re-running is cheap.** A hidden `.ntulearn-manifest.json` in each course folder remembers what
  was downloaded, so the next run only fetches new or changed files. Run it weekly to stay synced.
- **Filters:** include/exclude by filename pattern, and skip huge files.
- Files that live inside a document or assignment get their own subfolder named after that item.
- Lecture recordings on Panopto/Kaltura/Zoom are external links, not files, so they're skipped.

From the command line:

```powershell
uv run ntulearn download                                   # all current courses
uv run ntulearn download SC1007 MH1810                     # specific courses
uv run ntulearn download --include "*.pdf" --include "*.pptx"
uv run ntulearn download --exclude "*.mp4" --max-mb 200
uv run ntulearn download --dry-run                         # just list what would be fetched
uv run ntulearn download --dest "D:\Uni\Notes"
```

---

## Settings (optional environment variables)

| Variable | Default |
|---|---|
| `NTULEARN_BASE_URL` | `https://ntulearn.ntu.edu.sg` |
| `NTULEARN_DOWNLOAD_DIR` | `~\NTULearn` |
| `NTULEARN_DATA_DIR` | `~\.ntulearn-mcp` (session, login browser profile, file cache) |
| `NTULEARN_COOKIES` | Use this cookie string instead of the saved session |

In Claude Desktop's config, put these in an `"env": { ... }` block next to `"args"`.

---

## Troubleshooting

- **"Not logged in / session expired"**: run `uv run ntulearn login`, or ask Claude to log you in.
- **The login window doesn't open**: install Chrome, or run `uv run playwright install chromium`.
- **Login keeps failing**: there's a manual fallback. Log in to NTULearn in your normal browser,
  press F12, go to *Network*, click any request to `ntulearn.ntu.edu.sg`, copy the `Cookie` request
  header, then run `uv run ntulearn set-cookie "<paste here>"`.
- **"not accessible (403)" for some folders**: those are hidden by the instructor or not yet
  released (for example, the answers before the quiz closes). That's expected.
- **Paths too long on Windows**: names are already shortened, but if a deep course still fails,
  use a short destination like `--dest C:\NTU` or enable Windows long paths.

## Notes

- This has been tested against a mock Blackboard server, not against NTULearn itself, since that
  needs your login. The endpoints are Blackboard's standard public ones, but if something like
  grades comes back empty for your courses, that part probably needs a tweak.
- It only reads. It never submits, posts or changes anything on NTULearn.
- Course materials are for your own study. Please don't redistribute them.
