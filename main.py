from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
import re
import logging
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import httpx

app = FastAPI(
    title="First Commit Finder",
    description="Provide a repo URL and get the first commit URL (HTMX UI)",
    version="1.0.0",
)

app.mount("/static", StaticFiles(directory="static"), name="static")

templates = Jinja2Templates(directory="templates")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("first-commit")

ALLOWED_HOSTS = {"github.com", "gitlab.com", "bitbucket.org"}


def to_https_remote(url: str) -> str:
    if not url:
        return url
    if url.startswith("git@"):
        m = re.match(r"git@([^:]+):(.+?)(?:\.git)?$", url)
        if m:
            host, path = m.group(1), m.group(2)
            return f"https://{host}/{path}"
    if url.startswith("ssh://"):
        url = re.sub(r"^ssh://", "https://", url)
    if not url.startswith("https://"):
        url = "https://" + url
    return re.sub(r"\.git$", "", url)


def parse_host_from_url(url: str) -> str:
    if url.startswith("git@"):
        m = re.match(r"git@([^:]+):", url)
        return m.group(1) if m else ""
    m = re.match(r"https?://([^/]+)/", url)
    return m.group(1) if m else ""


async def get_first_commit_github(owner: str, repo: str) -> tuple[str, str]:
    """Get first commit using GitHub API"""
    async with httpx.AsyncClient(timeout=30.0) as client:
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
    async with httpx.AsyncClient(timeout=30.0) as client:
        # URL encode the project path
        import urllib.parse
        encoded_path = urllib.parse.quote(project_path, safe='')

        # Get commits in reverse order
        commits_url = f"https://gitlab.com/api/v4/projects/{encoded_path}/repository/commits"
        params = {"per_page": 100, "order": "default"}

        all_commits = []
        page = 1
        while True:
            params["page"] = page
            resp = await client.get(commits_url, params=params)
            if resp.status_code != 200:
                if page == 1:
                    raise HTTPException(
                        status_code=400, detail="Repository not found or not accessible")
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

        if not all_commits:
            raise HTTPException(status_code=400, detail="No commits found")

        first_commit = all_commits[-1]["id"]
        https_url = f"https://gitlab.com/{project_path}"
        return first_commit, https_url


async def get_first_commit_bitbucket(workspace: str, repo: str) -> tuple[str, str]:
    """Get first commit using Bitbucket API"""
    async with httpx.AsyncClient(timeout=30.0) as client:
        # Get commits
        commits_url = f"https://api.bitbucket.org/2.0/repositories/{workspace}/{repo}/commits"
        params = {"pagelen": 100}

        all_commits = []
        next_url = commits_url
        iterations = 0

        while next_url and iterations < 100:
            resp = await client.get(next_url, params=params if iterations == 0 else None)
            if resp.status_code != 200:
                if iterations == 0:
                    raise HTTPException(
                        status_code=400, detail="Repository not found or not accessible")
                break

            data = resp.json()
            values = data.get("values", [])
            if not values:
                break

            all_commits.extend(values)
            next_url = data.get("next")
            iterations += 1

        if not all_commits:
            raise HTTPException(status_code=400, detail="No commits found")

        first_commit = all_commits[-1]["hash"]
        https_url = f"https://bitbucket.org/{workspace}/{repo}"
        return first_commit, https_url


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return FileResponse("static/favicon.ico")


def error_response(request: Request, status: int, detail: str):
    return templates.TemplateResponse(
        status_code=status,
        request=request,
        name="error.html",
        context={"message": detail}
    )


@app.post("/first-commit", response_class=HTMLResponse)
async def first_commit(request: Request, repo_url: str = Form(...)):
    if not repo_url:
        return HTMLResponse("<div class='error'>Please provide a repository URL.</div>", status_code=400)

    normalized = to_https_remote(repo_url)
    host = parse_host_from_url(normalized)

    if not host:
        return HTMLResponse("<div class='error'>Invalid repository URL.</div>", status_code=400)
    if host not in ALLOWED_HOSTS:
        return HTMLResponse(f"<div class='error'>Host not allowed: {host}</div>", status_code=403)

    try:
        # Parse repository info based on host
        if host == "github.com":
            # Extract owner/repo from URL
            match = re.match(
                r"https://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$", normalized)
            if not match:
                raise HTTPException(
                    status_code=400, detail="Invalid GitHub URL format")
            owner, repo = match.groups()
            first_commit_hash, remote_https = await get_first_commit_github(owner, repo)

        elif host == "gitlab.com":
            # Extract project path from URL
            match = re.match(
                r"https://gitlab\.com/(.+?)(?:\.git)?/?$", normalized)
            if not match:
                raise HTTPException(
                    status_code=400, detail="Invalid GitLab URL format")
            project_path = match.group(1)
            first_commit_hash, remote_https = await get_first_commit_gitlab(project_path)

        elif host == "bitbucket.org":
            # Extract workspace/repo from URL
            match = re.match(
                r"https://bitbucket\.org/([^/]+)/([^/]+?)(?:\.git)?/?$", normalized)
            if not match:
                raise HTTPException(
                    status_code=400, detail="Invalid Bitbucket URL format")
            workspace, repo = match.groups()
            first_commit_hash, remote_https = await get_first_commit_bitbucket(workspace, repo)
        else:
            raise HTTPException(
                status_code=403, detail=f"Unsupported {host} host at the moment.")

    except HTTPException as he:
        detail = he.detail if isinstance(he.detail, str) else str(he.detail)
        status = he.status_code
        return error_response(
            status=status,
            request=request,
            detail=detail
        )
    except httpx.TimeoutException:
        logger.warning("API request timed out for %s", repo_url)

        return error_response(
            status=status,
            request=request,
            detail="Request timed out. Please try again."
        )
    except Exception as e:
        logger.exception("Unhandled error for %s", repo_url)
        return error_response(
            status=status,
            request=request,
            detail="An internal error occurred. Please try again."
        )

    commit_url = f"{remote_https.rstrip('/')}/commit/{first_commit_hash}"

    return templates.TemplateResponse(
        request=request,
        name="first-commit.html",
        context={
            "commit_hash": first_commit_hash,
            "commit_url": commit_url,
            "host": host,
        }
    )


@app.get("/", response_class=HTMLResponse)
def read_root(request: Request):
    return templates.TemplateResponse(
        request=request, name="main.html"
    )
