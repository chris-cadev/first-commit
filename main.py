from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse
import subprocess
import os
import re
import asyncio
import logging
from tempfile import TemporaryDirectory

app = FastAPI(
    title="First Commit Finder",
    description="Provide a repo URL and get the first commit URL (HTMX UI)",
    version="1.0.0",
)

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
    return re.sub(r"\.git$", "", url)


def parse_host_from_url(url: str) -> str:
    if url.startswith("git@"):
        m = re.match(r"git@([^:]+):", url)
        return m.group(1) if m else ""
    m = re.match(r"https?://([^/]+)/", url)
    return m.group(1) if m else ""


def run_git_commands(repo_url: str):
    """
    Clone repo and find first commit. Returns first commit hash and remote URL.
    """
    temp_dir = "/tmp" if os.path.exists("/tmp") else None
    with TemporaryDirectory(prefix="repo_clone_", dir=temp_dir) as tmp:
        repo_dir = os.path.join(tmp, "repo.git")

        try:
            logger.info("Cloning %s into %s", repo_url, repo_dir)
            command = ["git", "clone", "--bare", "--filter=blob:none", "--no-checkout", repo_url, repo_dir]
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )

            for line in process.stdout:
                logger.info('Command (%s)\n%s', ' '.join(command), line.strip())

            retcode = process.wait()
            if retcode != 0:
                raise subprocess.CalledProcessError(retcode, process.args)

            logger.info("Getting first commit from %s", repo_dir)
            res = subprocess.run(
                ["git", "rev-list", "--max-parents=0", "--all"],
                cwd=repo_dir,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
            )
            logger.info("First commit found: %s", res)
            out = res.stdout.decode().strip()
            if not out:
                raise HTTPException(status_code=400, detail="Could not determine first commit.")
            first_commit = out.splitlines()[0].strip()

            try:
                res = subprocess.run(
                    ["git", "remote", "get-url", "origin"],
                    cwd=repo_dir,
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=10,
                )
                remote = res.stdout.decode().strip()
            except subprocess.CalledProcessError:
                remote = repo_url

            return first_commit, remote

        except subprocess.CalledProcessError as e:
            out = (e.stdout or e.stderr or b"").decode(errors="ignore")
            logger.warning("git error: %s", out)
            raise HTTPException(status_code=400, detail=f"git error: {out}")
        except subprocess.TimeoutExpired:
            logger.warning("git operation timed out for %s", repo_url)
            raise HTTPException(status_code=504, detail="git operation timed out")


@app.post("/first-commit", response_class=HTMLResponse)
async def first_commit(repo_url: str = Form(...)):
    if not repo_url:
        return HTMLResponse("<div class='error'>Please provide a repository URL.</div>", status_code=400)

    host = parse_host_from_url(repo_url)
    if not host:
        return HTMLResponse("<div class='error'>Invalid repository URL.</div>", status_code=400)
    if host not in ALLOWED_HOSTS:
        return HTMLResponse(f"<div class='error'>Host not allowed: {host}</div>", status_code=403)

    normalized = to_https_remote(repo_url)

    loop = asyncio.get_event_loop()
    try:
        first_commit, remote = await loop.run_in_executor(None, run_git_commands, repo_url)
    except HTTPException as he:
        detail = he.detail if isinstance(he.detail, str) else str(he.detail)
        status = he.status_code
        return HTMLResponse(f"<div class='error'>{detail}</div>", status_code=status)
    except Exception as e:
        logger.exception("Unhandled error for %s", repo_url)
        return HTMLResponse("<div class='error'>internal error</div>", status_code=500)

    remote_https = to_https_remote(remote) or normalized
    commit_url = f"{remote_https.rstrip('/')}/commit/{first_commit}"

    html = f"""
    <div class="result">
        <div class="success-icon">✓</div>
        <div class="result-content">
            <div class="result-label">First Commit Hash:</div>
            <div class="commit-hash">{first_commit}</div>
            <div class="result-label" style="margin-top: 1rem;">View on {host}:</div>
            <a href="{commit_url}" target="_blank" rel="noopener" class="commit-link">{commit_url}</a>
        </div>
    </div>
    """
    return HTMLResponse(html)


@app.get("/", response_class=HTMLResponse)
def read_root():
    return """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width,initial-scale=1" />
        <title>First Commit Finder</title>
        <script src="https://cdn.jsdelivr.net/npm/htmx.org@2.0.7/dist/htmx.min.js"></script>
        <style>
            * { box-sizing: border-box; }
            body {
                margin: 0;
                padding: 1rem;
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
                display: flex;
                justify-content: center;
                align-items: center;
                min-height: 100vh;
                background: linear-gradient(135deg, #0f2027, #203a43, #2c5364);
                color: #fff;
            }
            .card {
                background: rgba(15, 15, 20, 0.9);
                border-radius: 16px;
                padding: 2.5rem;
                max-width: 720px;
                width: 100%;
                box-shadow: 0 20px 60px rgba(0,0,0,0.7);
                backdrop-filter: blur(10px);
                border: 1px solid rgba(255,255,255,0.1);
            }
            h2 {
                margin: 0 0 0.5rem 0;
                font-size: 2.25rem;
                font-weight: 700;
                background: linear-gradient(90deg, #00d4ff, #0066ff);
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
                background-clip: text;
            }
            .subtitle {
                margin: 0 0 2rem 0;
                line-height: 1.6;
                color: #a0aec0;
                font-size: 0.95rem;
            }
            .form-group {
                margin-bottom: 1rem;
            }
            label {
                display: block;
                margin-bottom: 0.5rem;
                color: #cbd5e0;
                font-size: 0.9rem;
                font-weight: 500;
            }
            input[type="text"] {
                width: 100%;
                padding: 0.875rem 1.125rem;
                border-radius: 10px;
                border: 2px solid #2d3748;
                background: rgba(26, 32, 44, 0.8);
                color: #fff;
                font-size: 1rem;
                transition: all 0.3s ease;
                font-family: 'Monaco', 'Courier New', monospace;
            }
            input[type="text"]:focus {
                outline: none;
                border-color: #00d4ff;
                background: rgba(26, 32, 44, 1);
                box-shadow: 0 0 0 3px rgba(0, 212, 255, 0.1);
            }
            input[type="text"]::placeholder {
                color: #4a5568;
            }
            .form-buttons {
                display: flex;
                gap: 0.75rem;
                margin-top: 1.25rem;
            }
            button {
                padding: 0.875rem 1.5rem;
                border-radius: 10px;
                border: none;
                font-weight: 600;
                font-size: 0.95rem;
                cursor: pointer;
                transition: all 0.2s ease;
                flex: 1;
            }
            button[type="submit"] {
                background: linear-gradient(135deg, #00d4ff, #0066ff);
                color: #fff;
                box-shadow: 0 4px 15px rgba(0, 100, 255, 0.3);
            }
            button[type="submit"]:hover:not(:disabled) {
                transform: translateY(-2px);
                box-shadow: 0 6px 20px rgba(0, 212, 255, 0.4);
            }
            button[type="submit"]:active:not(:disabled) {
                transform: translateY(0);
            }
            button[type="submit"]:disabled {
                opacity: 0.6;
                cursor: not-allowed;
            }
            button[type="reset"] {
                background: rgba(74, 85, 104, 0.5);
                color: #e2e8f0;
                border: 1px solid rgba(255, 255, 255, 0.1);
            }
            button[type="reset"]:hover {
                background: rgba(74, 85, 104, 0.7);
                transform: translateY(-2px);
            }
            #loading-indicator {
                display: none;
                margin-top: 1.5rem;
                padding: 1rem;
                background: rgba(0, 212, 255, 0.1);
                border-radius: 10px;
                border: 1px solid rgba(0, 212, 255, 0.3);
                text-align: center;
                color: #00d4ff;
                font-weight: 500;
            }
            #loading-indicator.htmx-request {
                display: block;
                animation: fadeIn 0.3s ease;
            }
            @keyframes fadeIn {
                from { opacity: 0; transform: translateY(-10px); }
                to { opacity: 1; transform: translateY(0); }
            }
            .spinner {
                display: inline-block;
                width: 16px;
                height: 16px;
                border: 2px solid rgba(0, 212, 255, 0.3);
                border-top-color: #00d4ff;
                border-radius: 50%;
                animation: spin 0.8s linear infinite;
                margin-right: 0.5rem;
                vertical-align: middle;
            }
            @keyframes spin {
                to { transform: rotate(360deg); }
            }
            #result {
                margin-top: 1.5rem;
                animation: slideIn 0.4s ease;
            }
            @keyframes slideIn {
                from { opacity: 0; transform: translateY(10px); }
                to { opacity: 1; transform: translateY(0); }
            }
            .result {
                background: rgba(16, 185, 129, 0.1);
                border: 2px solid rgba(16, 185, 129, 0.3);
                border-radius: 12px;
                padding: 1.5rem;
                display: flex;
                gap: 1rem;
                align-items: flex-start;
            }
            .success-icon {
                flex-shrink: 0;
                width: 40px;
                height: 40px;
                background: linear-gradient(135deg, #10b981, #059669);
                border-radius: 50%;
                display: flex;
                align-items: center;
                justify-content: center;
                font-size: 1.5rem;
                font-weight: bold;
            }
            .result-content {
                flex: 1;
                min-width: 0;
            }
            .result-label {
                font-size: 0.85rem;
                color: #9ca3af;
                text-transform: uppercase;
                letter-spacing: 0.5px;
                font-weight: 600;
                margin-bottom: 0.5rem;
            }
            .commit-hash {
                font-family: 'Monaco', 'Courier New', monospace;
                font-size: 1rem;
                color: #e2e8f0;
                background: rgba(0, 0, 0, 0.3);
                padding: 0.5rem 0.75rem;
                border-radius: 6px;
                word-break: break-all;
            }
            .commit-link {
                display: inline-block;
                color: #00d4ff;
                text-decoration: none;
                word-break: break-all;
                padding: 0.5rem 0.75rem;
                background: rgba(0, 212, 255, 0.1);
                border-radius: 6px;
                transition: all 0.2s ease;
                border: 1px solid rgba(0, 212, 255, 0.2);
            }
            .commit-link:hover {
                background: rgba(0, 212, 255, 0.2);
                border-color: rgba(0, 212, 255, 0.4);
                transform: translateX(2px);
            }
            .error {
                background: rgba(239, 68, 68, 0.1);
                border: 2px solid rgba(239, 68, 68, 0.3);
                border-radius: 12px;
                padding: 1rem 1.25rem;
                color: #fca5a5;
                white-space: pre-wrap;
                line-height: 1.6;
            }
            .error::before {
                content: "⚠ ";
                font-size: 1.2rem;
                margin-right: 0.5rem;
            }
            @media (max-width: 640px) {
                .card { padding: 1.5rem; }
                h2 { font-size: 1.75rem; }
                .form-buttons { flex-direction: column; }
                button { width: 100%; }
            }
        </style>
    </head>
    <body>
        <div class="card">
            <h2>🔍 First Commit Finder</h2>
            <p class="subtitle">Discover the very first commit of any GitHub, GitLab, or Bitbucket repository. Enter a repository URL below to get started.</p>
            
            <form hx-post="/first-commit" 
                  hx-target="#result" 
                  hx-swap="innerHTML"
                  hx-indicator="#loading-indicator"
                  hx-on::before-request="document.querySelector('button[type=submit]').disabled = true"
                  hx-on::after-request="document.querySelector('button[type=submit]').disabled = false">
                
                <div class="form-group">
                    <label for="repo_url">Repository URL</label>
                    <input 
                        name="repo_url" 
                        id="repo_url"
                        type="text" 
                        placeholder="https://github.com/user/repo.git" 
                        required 
                        autocomplete="off"
                        spellcheck="false" />
                </div>
                
                <div class="form-buttons">
                    <button type="submit">Find First Commit</button>
                    <button type="reset" onclick="document.getElementById('result').innerHTML=''; document.getElementById('repo_url').focus();">Clear</button>
                </div>
            </form>
            
            <div id="loading-indicator">
                <span class="spinner"></span>
                Cloning repository and analyzing commits...
            </div>
            
            <div id="result"></div>
        </div>

        <script>
            // Handle HTMX errors gracefully
            document.body.addEventListener('htmx:responseError', function(event) {
                const resultDiv = document.getElementById('result');
                const status = event.detail.xhr.status;
                let message = 'An unexpected error occurred. Please try again.';
                
                if (status === 400) {
                    message = 'Invalid repository URL or git error. Please check the URL and try again.';
                } else if (status === 403) {
                    message = 'Repository host not allowed. Only GitHub, GitLab, and Bitbucket are supported.';
                } else if (status === 504) {
                    message = 'The operation timed out. The repository might be too large or unavailable.';
                } else if (status === 0) {
                    message = 'Network error. Please check your internet connection.';
                }
                
                resultDiv.innerHTML = `<div class="error">${message}</div>`;
            });

            // Handle network errors
            document.body.addEventListener('htmx:sendError', function(event) {
                const resultDiv = document.getElementById('result');
                resultDiv.innerHTML = '<div class="error">Network error. Unable to reach the server.</div>';
            });

            // Focus input on page load
            document.addEventListener('DOMContentLoaded', function() {
                document.getElementById('repo_url').focus();
            });

            // Clear error when user starts typing
            document.getElementById('repo_url').addEventListener('input', function() {
                const resultDiv = document.getElementById('result');
                if (resultDiv.querySelector('.error')) {
                    resultDiv.innerHTML = '';
                }
            });
        </script>
    </body>
    </html>
    """