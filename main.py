from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from markupsafe import Markup
import hashlib
import json
import re
import logging
import os
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit, urlunsplit
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import httpx
from dotenv import load_dotenv
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

app = FastAPI(
    title="First Commit Finder",
    description="Provide a repo URL and get the first commit URL (HTMX UI)",
    version="1.0.0",
)

load_dotenv()

app.mount("/static", StaticFiles(directory="static"), name="static")

templates = Jinja2Templates(directory="templates")

limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return templates.TemplateResponse(
        status_code=429,
        request=request,
        name="error.html",
        context={
            "message": "Too many requests. Please wait a minute and try again."
        },
    )


CACHE_DB_PATH = os.environ.get("CACHE_DB_PATH", "first_commit_cache.db")
CACHE_TTL_SECONDS = 3600
AUDIT_RETENTION_DAYS = int(os.environ.get("AUDIT_RETENTION_DAYS", "90"))
AUDIT_LOG_IP = os.environ.get("AUDIT_LOG_IP", "1") == "1"
CACHE_ENABLED = True

CSRF_COOKIE = "csrf_token"

SCHEMA = """
CREATE TABLE IF NOT EXISTS search_cache (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    stored_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_search_cache_stored_at ON search_cache(stored_at);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    client_ip TEXT,
    repo_url TEXT NOT NULL,
    host TEXT,
    outcome TEXT NOT NULL,
    cache_hit INTEGER NOT NULL DEFAULT 0,
    commit_hash TEXT,
    status_code INTEGER NOT NULL,
    duration_ms INTEGER,
    prev_hash TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_log_ts ON audit_log(ts);

CREATE TABLE IF NOT EXISTS cache_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    cache_key TEXT NOT NULL,
    change_type TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cache_audit_ts ON cache_audit(ts);

CREATE TRIGGER IF NOT EXISTS trg_search_cache_insert
AFTER INSERT ON search_cache BEGIN
    INSERT INTO cache_audit(ts, cache_key, change_type)
    VALUES (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), NEW.key, 'insert');
END;
CREATE TRIGGER IF NOT EXISTS trg_search_cache_update
AFTER UPDATE ON search_cache BEGIN
    INSERT INTO cache_audit(ts, cache_key, change_type)
    VALUES (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), NEW.key, 'update');
END;
CREATE TRIGGER IF NOT EXISTS trg_search_cache_delete
AFTER DELETE ON search_cache BEGIN
    INSERT INTO cache_audit(ts, cache_key, change_type)
    VALUES (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), OLD.key, 'delete');
END;
"""

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def _sanitize(value: str | None) -> str | None:
    if value is None:
        return None
    return _CONTROL_CHARS.sub("", value)


def _db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(CACHE_DB_PATH, timeout=10, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA trusted_schema=OFF")
    conn.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 100_000)
    return conn


def init_db() -> None:
    global CACHE_ENABLED
    try:
        conn = _db_connect()
        conn.executescript(SCHEMA)
        os.chmod(CACHE_DB_PATH, 0o600)
        rows = conn.execute("PRAGMA quick_check").fetchall()
        if rows != [("ok",)]:
            logger.warning("SQLite quick_check failed: %s", rows)
            CACHE_ENABLED = False
        purge_expired(conn)
        conn.close()
    except Exception:
        logger.exception("Failed to initialize SQLite cache; running uncached")
        CACHE_ENABLED = False


def purge_expired(conn: sqlite3.Connection) -> None:
    conn.execute(
        "DELETE FROM search_cache WHERE stored_at <= ?",
        (time.time() - CACHE_TTL_SECONDS,),
    )
    cutoff = (datetime.now(timezone.utc) - timedelta(days=AUDIT_RETENTION_DAYS))
    conn.execute(
        "DELETE FROM audit_log WHERE ts < ?",
        (cutoff.isoformat(timespec="milliseconds"),),
    )
    conn.execute(
        "DELETE FROM cache_audit WHERE ts < ?",
        (cutoff.isoformat(timespec="milliseconds").replace("+00:00", "Z"),),
    )


def get_cached(key: str) -> tuple[int, str, dict] | None:
    if not CACHE_ENABLED:
        return None
    try:
        conn = _db_connect()
        row = conn.execute(
            "SELECT value FROM search_cache WHERE key = ? AND stored_at > ?",
            (key, time.time() - CACHE_TTL_SECONDS),
        ).fetchone()
        conn.close()
        if row is None:
            return None
        data = json.loads(row[0])
        return data["status"], data["template"], data["context"]
    except Exception:
        logger.warning("cache read failed", exc_info=True)
        return None


def set_cached(key: str, result: tuple[int, str, dict]) -> None:
    if not CACHE_ENABLED:
        return
    try:
        conn = _db_connect()
        conn.execute(
            "INSERT OR REPLACE INTO search_cache(key, value, stored_at) VALUES (?, ?, ?)",
            (
                key,
                json.dumps(
                    {"status": result[0], "template": result[1], "context": result[2]}
                ),
                time.time(),
            ),
        )
        conn.execute(
            "DELETE FROM search_cache WHERE stored_at <= ?",
            (time.time() - CACHE_TTL_SECONDS,),
        )
        conn.close()
    except Exception:
        logger.warning("cache write failed", exc_info=True)


def _audit_prev_hash(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT ts, client_ip, repo_url, host, outcome, cache_hit, commit_hash, "
        "status_code, duration_ms, prev_hash FROM audit_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    parts = ["" if v is None else str(v) for v in row]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def record_audit(
    *,
    client_ip: str | None,
    repo_url: str,
    host: str | None,
    outcome: str,
    cache_hit: bool,
    commit_hash: str | None,
    status_code: int,
    duration_ms: int,
) -> None:
    try:
        conn = _db_connect()
        prev_hash = _audit_prev_hash(conn)
        conn.execute(
            "INSERT INTO audit_log(ts, client_ip, repo_url, host, outcome, cache_hit, "
            "commit_hash, status_code, duration_ms, prev_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                client_ip if AUDIT_LOG_IP else None,
                _sanitize(repo_url),
                _sanitize(host),
                outcome,
                int(cache_hit),
                commit_hash,
                status_code,
                duration_ms,
                prev_hash,
            ),
        )
        purge_expired(conn)
        conn.close()
    except Exception:
        logger.warning("audit write failed", exc_info=True)


def audit_outcome(status_code: int, template: str, context: dict) -> str:
    if status_code == 200:
        return "success"
    if status_code == 403:
        return "host_not_allowed"
    if status_code == 429:
        return "rate_limited"
    if status_code == 504:
        return "timeout"
    if status_code == 502:
        return "upstream_error"
    message = (context or {}).get("message", "")
    if "not found" in message.lower():
        return "not_found"
    if "no commits" in message.lower():
        return "no_commits"
    if status_code >= 500:
        return "internal_error"
    return "error"


def get_or_create_csrf_token(request: Request) -> str:
    token = request.cookies.get(CSRF_COOKIE)
    if not token:
        token = secrets.token_urlsafe(32)
    return token


def set_csrf_cookie(response, token: str) -> None:
    response.set_cookie(
        CSRF_COOKIE,
        token,
        max_age=60 * 60,
        httponly=True,
        samesite="strict",
        secure=os.environ.get("CSRF_COOKIE_SECURE") == "1",
    )


SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "font-src 'self'; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Strict-Transport-Security": "max-age=63072000; includeSubDomains",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    for header, value in SECURITY_HEADERS.items():
        response.headers.setdefault(header, value)
    return response


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("first-commit")

init_db()

ALLOWED_HOSTS = {"github.com", "gitlab.com", "bitbucket.org"}

BITBUCKET_SERVER_HOSTS = {
    h.strip().lower()
    for h in os.environ.get("BITBUCKET_SERVER_HOSTS", "").split(",")
    if h.strip()
}


_BITBUCKET_SERVER_WEB_RE = re.compile(
    r"^/projects/([^/]+)/repos/([^/]+?)(?:\.git)?(?:/.*)?$", re.IGNORECASE)
_BITBUCKET_SERVER_SCM_RE = re.compile(
    r"^/scm/([^/]+)/([^/]+?)(?:\.git)?/?$", re.IGNORECASE)


def resolve_bitbucket_server(url: str) -> str | None:
    """Resolve a Bitbucket Server web/SCM URL to its canonical git URI.

    Returns "https://host/scm/PROJECT/repo.git" or None when the URL does
    not look like a Bitbucket Server repository.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        parts = urlsplit("https://" + url)
    host = parts.hostname.lower() if parts.hostname else ""
    if not host:
        return None
    path = parts.path.rstrip("/")
    match = _BITBUCKET_SERVER_WEB_RE.match(path) or _BITBUCKET_SERVER_SCM_RE.match(path)
    if not match:
        return None
    project, repo = match.groups()
    return urlunsplit(("https", host, f"/scm/{project}/{repo}.git", "", ""))


def to_https_remote(url: str) -> str:
    url = url.strip()
    if not url:
        return url
    if url.startswith("git@"):
        match = re.match(r"git@([^:]+):(.+?)(?:\.git)?$", url)
        if match:
            host, path = match.groups()
            url = f"https://{host}/{path}"
    resolved = resolve_bitbucket_server(url)
    if resolved is not None:
        return resolved
    parts = urlsplit(url)
    if parts.scheme == "ssh":
        host = parts.hostname.lower() if parts.hostname else ""
        path = parts.path.rstrip("/")
    else:
        if parts.scheme not in ("http", "https"):
            parts = urlsplit("https://" + url)
        host = parts.hostname.lower() if parts.hostname else ""
        path = parts.path.rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    return urlunsplit(("https", host, path, "", ""))


def parse_host_from_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        parts = urlsplit("https://" + url)
    return parts.hostname.lower() if parts.hostname else ""


async def get_first_commit_github(owner: str, repo: str) -> tuple[str, str]:
    """Get first commit using GitHub API"""
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
        # Get default branch
        repo_url = f"https://api.github.com/repos/{owner}/{repo}"
        resp = await client.get(repo_url)
        if resp.status_code != 200:
            raise HTTPException(
                status_code=400, detail="Repository not found or not accessible")

        default_branch = resp.json().get("default_branch", "main")

        # Get commits in reverse order (oldest first)
        commits_url = f"https://api.github.com/repos/{owner}/{repo}/commits"
        params = {"sha": default_branch, "per_page": 1}

        # Try to get the last page (oldest commits)
        resp = await client.get(commits_url, params=params)
        if resp.status_code != 200:
            raise HTTPException(
                status_code=400, detail="Unable to fetch commits")

        # Get link header to find last page
        link_header = resp.headers.get("Link", "")
        last_page_match = re.search(r'page=(\d+)>; rel="last"', link_header)

        if last_page_match:
            last_page = int(last_page_match.group(1))
            params["page"] = last_page
            resp = await client.get(commits_url, params=params)
            commits = resp.json()
            if commits:
                first_commit = commits[-1]["sha"]
            else:
                raise HTTPException(status_code=400, detail="No commits found")
        else:
            # Single page of commits, get the last one
            commits = resp.json()
            if not commits:
                raise HTTPException(status_code=400, detail="No commits found")

            # Need to get all commits on this page to find the first
            params["per_page"] = 100
            all_commits = []
            page = 1
            while True:
                params["page"] = page
                resp = await client.get(commits_url, params=params)
                if resp.status_code != 200:
                    break
                batch = resp.json()
                if not batch:
                    break
                all_commits.extend(batch)
                if len(batch) < 100:
                    break
                page += 1
                if page > 100:  # Safety limit
                    break

            if all_commits:
                first_commit = all_commits[-1]["sha"]
            else:
                raise HTTPException(status_code=400, detail="No commits found")

        https_url = f"https://github.com/{owner}/{repo}"
        return first_commit, https_url


async def get_first_commit_gitlab(project_path: str) -> tuple[str, str]:
    """Get first commit using GitLab API"""
    import asyncio
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
        # URL encode the project path
        import urllib.parse
        encoded_path = urllib.parse.quote(project_path, safe='')

        # Commits come back newest-first; the first commit lives on the last
        # page, which GitLab never advertises (no rel="last" Link header), so
        # locate it by binary search. Pages past the end return 200 + [].
        commits_url = f"https://gitlab.com/api/v4/projects/{encoded_path}/repository/commits"
        page_size = 100

        async def page_commits(page: int) -> list:
            last_status = None
            for attempt in range(3):
                resp = await client.get(
                    commits_url,
                    params={"per_page": page_size, "page": page, "order": "default"},
                )
                last_status = resp.status_code
                if resp.status_code == 200:
                    await asyncio.sleep(0.5)
                    return resp.json()
                if resp.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                    await asyncio.sleep(2 + attempt * 3)
                    continue
                break
            if last_status in (429, 500, 502, 503, 504):
                raise HTTPException(
                    status_code=502, detail="Upstream API unavailable. Please try again.")
            raise HTTPException(
                status_code=400, detail="Repository not found or not accessible")

        first_page = await page_commits(1)
        if not first_page:
            raise HTTPException(status_code=400, detail="No commits found")

        # Exponential growth finds an empty page, then binary search narrows
        # down to the last non-empty one (the oldest commits).
        lo, hi = 1, 2
        while await page_commits(hi):
            lo, hi = hi, hi * 2
        while lo + 1 < hi:
            mid = (lo + hi) // 2
            if await page_commits(mid):
                lo = mid
            else:
                hi = mid

        commits = await page_commits(lo)
        if not commits:
            raise HTTPException(status_code=400, detail="No commits found")

        first_commit = commits[-1]["id"]
        https_url = f"https://gitlab.com/{project_path}"
        return first_commit, https_url


async def get_first_commit_bitbucket(workspace: str, repo: str) -> tuple[str, str]:
    """Get first commit using Bitbucket API"""
    import asyncio
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
        # Commits come back newest-first; the first commit lives on the last
        # page. Pages past the end return 200 + {"values": []}, so locate the
        # last non-empty page by binary search.
        commits_url = f"https://api.bitbucket.org/2.0/repositories/{workspace}/{repo}/commits"
        page_size = 100

        async def page_commits(page: int) -> list:
            last_status = None
            for attempt in range(3):
                resp = await client.get(
                    commits_url, params={"pagelen": page_size, "page": page})
                last_status = resp.status_code
                if resp.status_code == 200:
                    await asyncio.sleep(0.5)
                    return resp.json().get("values", [])
                if resp.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                    await asyncio.sleep(2 + attempt * 3)
                    continue
                break
            if last_status in (429, 500, 502, 503, 504):
                raise HTTPException(
                    status_code=502, detail="Upstream API unavailable. Please try again.")
            raise HTTPException(
                status_code=400, detail="Repository not found or not accessible")

        first_page = await page_commits(1)
        if not first_page:
            raise HTTPException(status_code=400, detail="No commits found")

        # Exponential growth finds an empty page, then binary search narrows
        # down to the last non-empty one (the oldest commits).
        lo, hi = 1, 2
        while await page_commits(hi):
            lo, hi = hi, hi * 2
        while lo + 1 < hi:
            mid = (lo + hi) // 2
            if await page_commits(mid):
                lo = mid
            else:
                hi = mid

        commits = await page_commits(lo)
        if not commits:
            raise HTTPException(status_code=400, detail="No commits found")

        first_commit = commits[-1]["hash"]
        https_url = f"https://bitbucket.org/{workspace}/{repo}"
        return first_commit, https_url


async def get_first_commit_bitbucket_server(host: str, project: str, repo: str) -> tuple[str, str]:
    """Get first commit from a Bitbucket Server (Data Center) REST API."""
    import asyncio
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
        # Commits come back newest-first; the first commit lives on the last
        # offset. Pages past the end return 200 + {"values": []}, so locate
        # the last non-empty offset by binary search.
        commits_url = (
            f"https://{host}/rest/api/1.0/projects/{project}/repos/{repo}/commits"
        )
        page_size = 100

        async def page_commits(start: int) -> list:
            last_status = None
            for attempt in range(3):
                resp = await client.get(
                    commits_url, params={"limit": page_size, "start": start})
                last_status = resp.status_code
                if resp.status_code == 200:
                    await asyncio.sleep(0.5)
                    return resp.json().get("values", [])
                if resp.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                    await asyncio.sleep(2 + attempt * 3)
                    continue
                break
            if last_status in (429, 500, 502, 503, 504):
                raise HTTPException(
                    status_code=502, detail="Upstream API unavailable. Please try again.")
            raise HTTPException(
                status_code=400, detail="Repository not found or not accessible")

        first_page = await page_commits(0)
        if not first_page:
            raise HTTPException(status_code=400, detail="No commits found")

        # Exponential growth finds an empty offset, then binary search narrows
        # down to the last non-empty one (the oldest commits).
        lo, hi = 0, page_size
        while await page_commits(hi):
            lo, hi = hi, hi * 2
        while lo + page_size < hi:
            mid = ((lo + hi) // (2 * page_size)) * page_size
            if await page_commits(mid):
                lo = mid
            else:
                hi = mid

        commits = await page_commits(lo)
        if not commits:
            raise HTTPException(status_code=400, detail="No commits found")

        first_commit = commits[-1]["id"]
        https_url = f"https://{host}/projects/{project}/repos/{repo}"
        return first_commit, https_url


async def _resolve_first_commit(repo_url: str) -> tuple[int, str, dict, bool, str]:
    """Resolve a repo URL to (status_code, template_name, context, cache_hit, host)."""
    if not repo_url:
        return 400, "error.html", {"message": "Please provide a repository URL."}, False, ""

    normalized = to_https_remote(repo_url)
    host = parse_host_from_url(normalized)
    logger.info("lookup for host=%s", host)

    if not host:
        return 400, "error.html", {"message": "Invalid repository URL."}, False, host
    if host not in ALLOWED_HOSTS and host not in BITBUCKET_SERVER_HOSTS:
        return 403, "error.html", {"message": f"Host not allowed: {host}"}, False, host

    cached = get_cached(normalized)
    if cached is not None:
        return cached[0], cached[1], cached[2], True, host

    try:
        if host == "github.com":
            match = re.match(
                r"https://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$", normalized)
            if not match:
                return 400, "error.html", {"message": "Invalid GitHub URL format"}, False, host
            owner, repo = match.groups()
            first_commit_hash, remote_https = await get_first_commit_github(owner, repo)

        elif host == "gitlab.com":
            match = re.match(
                r"https://gitlab\.com/(.+?)(?:\.git)?/?$", normalized)
            if not match:
                return 400, "error.html", {"message": "Invalid GitLab URL format"}, False, host
            project_path = match.group(1)
            first_commit_hash, remote_https = await get_first_commit_gitlab(project_path)

        elif host == "bitbucket.org":
            match = re.match(
                r"https://bitbucket\.org/([^/]+)/([^/]+?)(?:\.git)?/?$", normalized)
            if not match:
                return 400, "error.html", {"message": "Invalid Bitbucket URL format"}, False, host
            workspace, repo = match.groups()
            first_commit_hash, remote_https = await get_first_commit_bitbucket(workspace, repo)
        elif host in BITBUCKET_SERVER_HOSTS:
            match = _BITBUCKET_SERVER_SCM_RE.match(urlsplit(normalized).path)
            if not match:
                return 400, "error.html", {"message": "Invalid Bitbucket Server URL format"}, False, host
            project, repo = match.groups()
            first_commit_hash, remote_https = await get_first_commit_bitbucket_server(
                host, project, repo)
        else:
            return 403, "error.html", {"message": f"Unsupported {host} host at the moment."}, False, host
    except HTTPException as he:
        detail = he.detail if isinstance(he.detail, str) else str(he.detail)
        return he.status_code, "error.html", {"message": detail}, False, host
    except httpx.TimeoutException:
        logger.warning("API request timed out for %s", repo_url)
        return 504, "error.html", {"message": "Request timed out. Please try again."}, False, host
    except Exception:
        logger.exception("Unhandled error for %s", repo_url)
        return 500, "error.html", {"message": "An internal error occurred. Please try again."}, False, host

    if host in BITBUCKET_SERVER_HOSTS:
        commit_url = f"{remote_https.rstrip('/')}/commits/{first_commit_hash}"
    else:
        commit_url = f"{remote_https.rstrip('/')}/commit/{first_commit_hash}"
    context = {
        "commit_hash": first_commit_hash,
        "commit_url": commit_url,
        "host": host,
    }
    result = 200, "first-commit.html", context
    set_cached(normalized, result)
    return 200, "first-commit.html", dict(context), False, host


async def resolve_first_commit(repo_url: str, request: Request | None = None) -> tuple[int, str, dict]:
    """Resolve a repo URL to (status_code, template_name, context), recording an audit entry."""
    start = time.perf_counter()
    status, template, context, cache_hit, host = await _resolve_first_commit(repo_url)
    client_ip = request.client.host if request and request.client else None
    record_audit(
        client_ip=client_ip,
        repo_url=repo_url,
        host=host or None,
        outcome=audit_outcome(status, template, context),
        cache_hit=cache_hit,
        commit_hash=context.get("commit_hash") if status == 200 else None,
        status_code=status,
        duration_ms=int((time.perf_counter() - start) * 1000),
    )
    return status, template, context


def render_fragment(request: Request, template: str, context: dict) -> Markup:
    return Markup(templates.get_template(template).render({**context, "request": request}))


@app.post("/first-commit", response_class=HTMLResponse)
@limiter.limit("20/minute")
async def first_commit(request: Request, repo_url: str = Form(...), csrf_token: str = Form("")):
    submitted = csrf_token or request.headers.get("X-CSRF-Token", "")
    cookie_token = request.cookies.get(CSRF_COOKIE)
    if not cookie_token or not secrets.compare_digest(cookie_token, submitted):
        return templates.TemplateResponse(
            request=request,
            name="error.html",
            context={"message": "Session expired. Please reload the page and try again."},
            status_code=403,
        )

    status, template, context = await resolve_first_commit(repo_url, request=request)
    if request.headers.get("HX-Request") == "true":
        return templates.TemplateResponse(
            request=request, name=template, context=context, status_code=status
        )
    token = get_or_create_csrf_token(request)
    fragment = render_fragment(request, template, context)
    response = templates.TemplateResponse(
        request=request,
        name="main.html",
        context={"fragment": fragment, "csrf_token": token},
        status_code=status,
    )
    set_csrf_cookie(response, token)
    return response


@app.get("/", response_class=HTMLResponse)
@limiter.limit("60/minute")
async def read_root(request: Request, url: str = None):
    token = get_or_create_csrf_token(request)
    if not url:
        response = templates.TemplateResponse(
            request=request,
            name="main.html",
            context={"fragment": "", "csrf_token": token},
        )
    else:
        status, template, context = await resolve_first_commit(url, request=request)
        fragment = render_fragment(request, template, context)
        response = templates.TemplateResponse(
            request=request,
            name="main.html",
            context={"fragment": fragment, "csrf_token": token},
            status_code=status,
        )
    set_csrf_cookie(response, token)
    return response


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return FileResponse("static/favicon.ico")
