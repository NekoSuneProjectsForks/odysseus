"""
github_service.py

Business logic for the GitHub integration (Settings > Integrations > GitHub —
admin-only, see THREAT_MODEL.md). Two responsibilities:
  1. OAuth App flow + GitHub REST calls (connect account, create repo, open PR).
  2. Local git operations against tracked repos under GITHUB_REPOS_DIR.

routes/github_routes.py and src/agent_tools/github_tools.py are both thin
callers of the functions here so the HTTP surface and the agent-tool surface
can't drift out of sync with each other.
"""

import os
import re
import shutil
import logging
import uuid
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any, List

import httpx

from core.database import SessionLocal, GithubAccount, GithubRepo
from src.constants import GITHUB_REPOS_DIR

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
GIT_TIMEOUT = 120  # seconds; clone/push can be slow on big repos
_SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


class GithubServiceError(Exception):
    """Raised for user-facing errors (bad input, GitHub API failure, git failure)."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------------

def get_account(owner: Optional[str]) -> Optional[GithubAccount]:
    db = SessionLocal()
    try:
        q = db.query(GithubAccount)
        if owner:
            q = q.filter(GithubAccount.owner == owner)
        return q.order_by(GithubAccount.connected_at.desc()).first()
    finally:
        db.close()


def account_status(owner: Optional[str]) -> Dict[str, Any]:
    acct = get_account(owner)
    if not acct or not acct.access_token:
        return {"connected": False}
    return {
        "connected": True,
        "login": acct.login,
        "avatar_url": acct.avatar_url,
        "connected_at": acct.connected_at.isoformat() if acct.connected_at else None,
    }


def disconnect_account(owner: Optional[str]) -> None:
    db = SessionLocal()
    try:
        q = db.query(GithubAccount)
        if owner:
            q = q.filter(GithubAccount.owner == owner)
        q.delete()
        db.commit()
    finally:
        db.close()


def _get_token(owner: Optional[str]) -> str:
    acct = get_account(owner)
    if not acct or not acct.access_token:
        raise GithubServiceError(
            "No GitHub account connected. Connect one in Settings > Integrations > GitHub."
        )
    return acct.access_token  # EncryptedText column decrypts transparently on read


def save_account(owner: Optional[str], access_token: str, scope: str, user_data: Dict[str, Any]) -> Dict[str, Any]:
    db = SessionLocal()
    try:
        q = db.query(GithubAccount)
        if owner:
            q = q.filter(GithubAccount.owner == owner)
        acct = q.first()
        if not acct:
            acct = GithubAccount(id=uuid.uuid4().hex[:12], owner=owner)
            db.add(acct)
        acct.access_token = access_token  # EncryptedText encrypts on write
        acct.token_scope = scope
        acct.login = user_data.get("login")
        acct.avatar_url = user_data.get("avatar_url")
        acct.connected_at = _utcnow()
        db.commit()
        return {"login": acct.login}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------

def oauth_client_id() -> str:
    return os.environ.get("GITHUB_OAUTH_CLIENT_ID", "")


def oauth_authorize_url(state: str, redirect_uri: str) -> str:
    import urllib.parse
    client_id = oauth_client_id()
    if not client_id:
        raise GithubServiceError("GITHUB_OAUTH_CLIENT_ID not set — add it to .env")
    params = urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": "repo read:user",
        "state": state,
    })
    return f"https://github.com/login/oauth/authorize?{params}"


async def oauth_exchange_code(code: str, redirect_uri: str) -> Dict[str, Any]:
    client_id = oauth_client_id()
    client_secret = os.environ.get("GITHUB_OAUTH_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        raise GithubServiceError("GitHub OAuth App not configured (GITHUB_OAUTH_CLIENT_ID/SECRET)")
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            "https://github.com/login/oauth/access_token",
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
            },
            headers={"Accept": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()
    if "access_token" not in data:
        raise GithubServiceError(data.get("error_description") or "Token exchange failed")
    return data


# ---------------------------------------------------------------------------
# GitHub REST helpers
# ---------------------------------------------------------------------------

def _api_headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def fetch_github_user(access_token: str) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(f"{GITHUB_API}/user", headers=_api_headers(access_token))
        resp.raise_for_status()
        return resp.json()


async def _github_api(method: str, path: str, token: str, json_body: Optional[dict] = None) -> Any:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.request(method, f"{GITHUB_API}{path}", headers=_api_headers(token), json=json_body)
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("message", resp.text)
        except Exception:
            detail = resp.text
        raise GithubServiceError(f"GitHub API {method} {path} failed ({resp.status_code}): {detail}")
    if resp.status_code == 204 or not resp.content:
        return {}
    return resp.json()


async def list_github_repos(owner: Optional[str], per_page: int = 100) -> List[Dict[str, Any]]:
    """Repos on the connected GitHub account — for a "pick one to clone" UI."""
    token = _get_token(owner)
    data = await _github_api("GET", f"/user/repos?per_page={per_page}&sort=updated", token)
    return [
        {
            "full_name": r["full_name"],
            "private": r["private"],
            "html_url": r["html_url"],
            "default_branch": r.get("default_branch"),
            "description": r.get("description") or "",
        }
        for r in data
    ]


async def create_github_repo(
    owner: Optional[str], name: str, description: str = "", private: bool = True,
    org: Optional[str] = None, auto_init: bool = True,
) -> Dict[str, Any]:
    if not _SLUG_RE.match(name or ""):
        raise GithubServiceError("Repo name may only contain letters, numbers, '.', '_', '-'")
    token = _get_token(owner)
    path = f"/orgs/{org}/repos" if org else "/user/repos"
    body = {"name": name, "description": description or "", "private": bool(private), "auto_init": auto_init}
    return await _github_api("POST", path, token, body)


async def create_pull_request(
    owner: Optional[str], full_name: str, title: str, head: str, base: str, body: str = "",
) -> Dict[str, Any]:
    token = _get_token(owner)
    return await _github_api("POST", f"/repos/{full_name}/pulls", token, {
        "title": title, "head": head, "base": base, "body": body or "",
    })


async def get_pr_checks(owner: Optional[str], full_name: str, ref: str) -> Dict[str, Any]:
    token = _get_token(owner)
    return await _github_api("GET", f"/repos/{full_name}/commits/{ref}/check-runs", token)


# ---------------------------------------------------------------------------
# Local git operations (argv passed directly to git — never shell=True)
# ---------------------------------------------------------------------------

async def _run_git(args: List[str], cwd: str, timeout: int = GIT_TIMEOUT) -> Dict[str, Any]:
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return {
            "stdout": stdout_b.decode(errors="replace"),
            "stderr": stderr_b.decode(errors="replace"),
            "returncode": proc.returncode,
        }
    except asyncio.TimeoutError:
        if proc:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
        raise GithubServiceError(f"git {' '.join(args)} timed out after {timeout}s")
    except FileNotFoundError:
        raise GithubServiceError("git is not installed on this host")


async def _git_ok(args: List[str], cwd: str, timeout: int = GIT_TIMEOUT) -> str:
    result = await _run_git(args, cwd, timeout)
    if result["returncode"] != 0:
        raise GithubServiceError(
            f"git {' '.join(args)} failed: {(result['stderr'].strip() or result['stdout'].strip())}"
        )
    return result["stdout"]


def _repo_dir_name(full_name: str) -> str:
    """'user/repo' -> 'user__repo': filesystem-safe, no path traversal."""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", full_name)


def _authed_remote_url(remote_url: str, token: str) -> str:
    """Inject the OAuth token into an https:// clone URL for this call only —
    never persisted (GithubRepo.remote_url stores the bare URL)."""
    if remote_url.startswith("https://"):
        return remote_url.replace("https://", f"https://x-access-token:{token}@", 1)
    return remote_url


def _local_path_for(full_name: str) -> str:
    Path(GITHUB_REPOS_DIR).mkdir(parents=True, exist_ok=True)
    return os.path.join(GITHUB_REPOS_DIR, _repo_dir_name(full_name))


def _safe_rmtree(path: str) -> None:
    """Only ever remove directories inside GITHUB_REPOS_DIR — defense in depth
    against a corrupted/foreign local_path wiping something unrelated."""
    root = os.path.realpath(GITHUB_REPOS_DIR)
    target = os.path.realpath(path)
    if target == root or os.path.commonpath([root, target]) != root:
        raise GithubServiceError("Refusing to delete a path outside the repos directory")
    shutil.rmtree(target, ignore_errors=True)


async def _configure_git_identity(local_path: str) -> None:
    """Set a repo-local commit identity if none is configured globally, so
    `git commit` doesn't fail with 'please tell me who you are' on a fresh host."""
    result = await _run_git(["config", "user.email"], cwd=local_path)
    if result["returncode"] == 0 and result["stdout"].strip():
        return
    await _run_git(["config", "user.email", "odysseus-agent@localhost"], cwd=local_path)
    await _run_git(["config", "user.name", "Odysseus Agent"], cwd=local_path)


# ---------------------------------------------------------------------------
# Repo tracking (DB) CRUD
# ---------------------------------------------------------------------------

def _repo_dict(r: GithubRepo) -> Dict[str, Any]:
    return {
        "id": r.id,
        "full_name": r.full_name,
        "remote_url": r.remote_url,
        "local_path": r.local_path,
        "default_branch": r.default_branch,
        "private": r.private,
        "origin": r.origin,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


def list_repos(owner: Optional[str]) -> List[Dict[str, Any]]:
    db = SessionLocal()
    try:
        q = db.query(GithubRepo)
        if owner:
            q = q.filter(GithubRepo.owner == owner)
        return [_repo_dict(r) for r in q.order_by(GithubRepo.created_at.desc()).all()]
    finally:
        db.close()


def get_repo_or_raise(owner: Optional[str], repo_id: str) -> GithubRepo:
    db = SessionLocal()
    try:
        q = db.query(GithubRepo).filter(GithubRepo.id == repo_id)
        if owner:
            q = q.filter(GithubRepo.owner == owner)
        repo = q.first()
        if not repo:
            raise GithubServiceError(f"Repo {repo_id} not found")
        db.expunge(repo)
        return repo
    finally:
        db.close()


def _track_repo(
    owner: Optional[str], full_name: str, remote_url: str, local_path: str,
    default_branch: str, private: bool, origin: str,
) -> Dict[str, Any]:
    db = SessionLocal()
    try:
        row = GithubRepo(
            id=uuid.uuid4().hex[:12],
            owner=owner,
            full_name=full_name,
            remote_url=remote_url,
            local_path=local_path,
            default_branch=default_branch,
            private=private,
            origin=origin,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return _repo_dict(row)
    finally:
        db.close()


def untrack_repo(owner: Optional[str], repo_id: str, delete_files: bool = False) -> None:
    repo = get_repo_or_raise(owner, repo_id)
    db = SessionLocal()
    try:
        db.query(GithubRepo).filter(GithubRepo.id == repo_id).delete()
        db.commit()
    finally:
        db.close()
    if delete_files:
        _safe_rmtree(repo.local_path)


# ---------------------------------------------------------------------------
# High-level actions
# ---------------------------------------------------------------------------

async def clone_repo(owner: Optional[str], full_name_or_url: str, branch: Optional[str] = None) -> Dict[str, Any]:
    token = _get_token(owner)
    full_name_or_url = full_name_or_url.strip()
    if full_name_or_url.startswith("http://") or full_name_or_url.startswith("https://"):
        remote_url = full_name_or_url.rstrip("/")
        if remote_url.endswith(".git"):
            remote_url = remote_url[:-4]
        parts = [p for p in remote_url.split("/") if p]
        full_name = "/".join(parts[-2:])
    else:
        full_name = full_name_or_url.strip("/")
        if full_name.count("/") != 1:
            raise GithubServiceError("Give an 'owner/repo' name or a full GitHub URL")
        remote_url = f"https://github.com/{full_name}"

    info = await _github_api("GET", f"/repos/{full_name}", token)
    default_branch = branch or info.get("default_branch", "main")
    private = bool(info.get("private", True))

    local_path = _local_path_for(full_name)
    if os.path.exists(local_path):
        raise GithubServiceError(f"{local_path} already exists — remove/untrack it first")

    authed_url = _authed_remote_url(remote_url + ".git", token)
    await _git_ok(
        ["clone", "--branch", default_branch, "--single-branch", authed_url, local_path],
        cwd=str(Path(GITHUB_REPOS_DIR)),
    )
    await _configure_git_identity(local_path)
    return _track_repo(owner, full_name, remote_url, local_path, default_branch, private, "cloned")


async def create_repo(
    owner: Optional[str], name: str, description: str = "", private: bool = True, org: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a brand-new GitHub repo (GitHub-side auto_init'd with a README),
    then clone it locally so it drops into the same workspace flow as any
    other tracked repo."""
    token = _get_token(owner)
    info = await create_github_repo(owner, name, description, private, org, auto_init=True)
    full_name = info["full_name"]
    default_branch = info.get("default_branch", "main")
    local_path = _local_path_for(full_name)
    if os.path.exists(local_path):
        raise GithubServiceError(f"{local_path} already exists — remove/untrack it first")
    remote_url = info["html_url"]
    authed_url = _authed_remote_url(remote_url + ".git", token)
    await _git_ok(["clone", authed_url, local_path], cwd=str(Path(GITHUB_REPOS_DIR)))
    await _configure_git_identity(local_path)
    return _track_repo(owner, full_name, remote_url, local_path, default_branch, private, "created")


async def upload_existing(
    owner: Optional[str], local_path: str, name: str, description: str = "",
    private: bool = True, org: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a new GitHub repo from an existing local directory: git-init if
    needed, commit anything uncommitted, add the remote, and push."""
    token = _get_token(owner)
    resolved = os.path.realpath(os.path.expanduser(local_path))
    if not os.path.isdir(resolved):
        raise GithubServiceError(f"{local_path} is not a directory on this host")

    info = await create_github_repo(owner, name, description, private, org, auto_init=False)
    full_name = info["full_name"]
    remote_url = info["html_url"]
    default_branch = info.get("default_branch") or "main"

    git_dir = os.path.join(resolved, ".git")
    if not os.path.isdir(git_dir):
        await _git_ok(["init", "-b", default_branch], cwd=resolved)
    await _configure_git_identity(resolved)

    status = await _git_ok(["status", "--porcelain"], cwd=resolved)
    if status.strip():
        await _git_ok(["add", "-A"], cwd=resolved)
        await _git_ok(["commit", "-m", "Initial commit (uploaded via Odysseus)"], cwd=resolved)

    remotes = await _git_ok(["remote"], cwd=resolved)
    if "origin" in remotes.split():
        await _git_ok(["remote", "set-url", "origin", remote_url + ".git"], cwd=resolved)
    else:
        await _git_ok(["remote", "add", "origin", remote_url + ".git"], cwd=resolved)

    authed_url = _authed_remote_url(remote_url + ".git", token)
    await _git_ok(["push", "-u", authed_url, f"HEAD:{default_branch}"], cwd=resolved, timeout=180)

    return _track_repo(owner, full_name, remote_url, resolved, default_branch, private, "uploaded")


async def repo_status(owner: Optional[str], repo_id: str) -> Dict[str, Any]:
    repo = get_repo_or_raise(owner, repo_id)
    branch = (await _git_ok(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo.local_path)).strip()
    porcelain = await _git_ok(["status", "--porcelain"], cwd=repo.local_path)
    dirty_files = [line[3:] for line in porcelain.splitlines() if line.strip()]
    ahead = behind = None
    try:
        await _git_ok(["fetch", "origin", branch], cwd=repo.local_path, timeout=30)
        counts = await _git_ok(
            ["rev-list", "--left-right", "--count", f"origin/{branch}...HEAD"], cwd=repo.local_path
        )
        parts = counts.split()
        if len(parts) == 2:
            behind, ahead = int(parts[0]), int(parts[1])
    except GithubServiceError:
        pass  # offline, or branch not pushed yet — status is still useful without ahead/behind
    return {
        "full_name": repo.full_name,
        "branch": branch,
        "dirty": bool(dirty_files),
        "dirty_files": dirty_files,
        "ahead": ahead,
        "behind": behind,
    }


async def repo_diff(owner: Optional[str], repo_id: str) -> str:
    repo = get_repo_or_raise(owner, repo_id)
    diff = await _git_ok(["diff", "--no-color", "HEAD"], cwd=repo.local_path)
    if not diff.strip():
        untracked = await _git_ok(["ls-files", "--others", "--exclude-standard"], cwd=repo.local_path)
        if untracked.strip():
            return "Untracked files (not shown in diff):\n" + untracked
    return diff


async def commit_changes(owner: Optional[str], repo_id: str, message: str) -> Dict[str, Any]:
    if not message or not message.strip():
        raise GithubServiceError("Commit message is required")
    repo = get_repo_or_raise(owner, repo_id)
    await _configure_git_identity(repo.local_path)
    status = await _git_ok(["status", "--porcelain"], cwd=repo.local_path)
    if not status.strip():
        return {"committed": False, "message": "Nothing to commit"}
    await _git_ok(["add", "-A"], cwd=repo.local_path)
    await _git_ok(["commit", "-m", message], cwd=repo.local_path)
    return {"committed": True}


async def push_changes(owner: Optional[str], repo_id: str, set_upstream: bool = True) -> Dict[str, Any]:
    repo = get_repo_or_raise(owner, repo_id)
    token = _get_token(owner)
    branch = (await _git_ok(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo.local_path)).strip()
    authed_url = _authed_remote_url(repo.remote_url + ".git", token)
    args = ["push", authed_url, f"HEAD:{branch}"]
    if set_upstream:
        args.insert(1, "-u")
    await _git_ok(args, cwd=repo.local_path, timeout=180)
    return {"pushed": True, "branch": branch}


async def create_branch(owner: Optional[str], repo_id: str, branch_name: str) -> Dict[str, Any]:
    repo = get_repo_or_raise(owner, repo_id)
    if not branch_name or not _SLUG_RE.match(branch_name.replace("/", "_")):
        raise GithubServiceError("Invalid branch name")
    await _git_ok(["checkout", "-b", branch_name], cwd=repo.local_path)
    return {"branch": branch_name}


async def open_pull_request(
    owner: Optional[str], repo_id: str, title: str, body: str = "", base: Optional[str] = None,
) -> Dict[str, Any]:
    repo = get_repo_or_raise(owner, repo_id)
    head = (await _git_ok(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo.local_path)).strip()
    base = base or repo.default_branch
    if head == base:
        raise GithubServiceError(
            f"Currently on '{base}' — create/checkout a feature branch before opening a PR"
        )
    await push_changes(owner, repo_id, set_upstream=True)
    pr = await create_pull_request(owner, repo.full_name, title, head, base, body)
    return {"number": pr.get("number"), "url": pr.get("html_url"), "head": head, "base": base}


async def pr_checks(owner: Optional[str], repo_id: str, ref: Optional[str] = None) -> Dict[str, Any]:
    repo = get_repo_or_raise(owner, repo_id)
    resolved_ref = ref or (await _git_ok(["rev-parse", "HEAD"], cwd=repo.local_path)).strip()
    return await get_pr_checks(owner, repo.full_name, resolved_ref)
