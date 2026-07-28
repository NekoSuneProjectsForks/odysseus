"""GitHub integration routes — /api/github/*.

Admin-only, full stop (see THREAT_MODEL.md / todo.md §7): connecting a GitHub
account, creating/cloning repos, committing, pushing, and opening PRs are all
gated behind `require_admin`. There is no per-user scoping here (the same
pattern as ApiToken/ModelEndpoint) — one connected account and one set of
tracked repos, shared by whichever admin is signed in.

Routes here are thin: all real logic lives in src/github_service.py so the
HTTP surface and the agent-tool surface (src/agent_tools/github_tools.py)
can't drift apart.
"""

import logging

from fastapi import APIRouter, Request, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from core.middleware import require_admin
from src.auth_helpers import get_current_user
from src.github_service import GithubServiceError

logger = logging.getLogger(__name__)


class CreateRepoBody(BaseModel):
    name: str
    description: str = ""
    private: bool = True
    org: str | None = None


class CloneRepoBody(BaseModel):
    full_name_or_url: str
    branch: str | None = None


class UploadRepoBody(BaseModel):
    local_path: str
    name: str
    description: str = ""
    private: bool = True
    org: str | None = None


class CommitBody(BaseModel):
    message: str


class BranchBody(BaseModel):
    name: str


class PullRequestBody(BaseModel):
    title: str
    body: str = ""
    base: str | None = None


def setup_github_routes() -> APIRouter:
    router = APIRouter(prefix="/api/github", tags=["github"])

    @router.get("/status")
    async def status(request: Request):
        require_admin(request)
        from src.github_service import account_status
        return account_status(None)

    @router.post("/disconnect")
    async def disconnect(request: Request):
        require_admin(request)
        from src.github_service import disconnect_account
        disconnect_account(None)
        return {"ok": True}

    # ── OAuth ──

    @router.get("/oauth/authorize")
    async def oauth_authorize(request: Request):
        require_admin(request)
        from routes.email_helpers import make_oauth_state
        from src.github_service import oauth_authorize_url
        import os
        owner = get_current_user(request) or ""
        redirect_uri = (
            os.environ.get("GITHUB_OAUTH_REDIRECT_URI")
            or f"http://{request.headers.get('host', 'localhost:7000')}/api/github/oauth/callback"
        )
        state = make_oauth_state("github", owner)
        try:
            url = oauth_authorize_url(state, redirect_uri)
        except GithubServiceError as exc:
            raise HTTPException(400, str(exc)) from exc
        return RedirectResponse(url)

    @router.get("/oauth/callback")
    async def oauth_callback(
        request: Request,
        code: str = Query(None),
        state: str = Query(None),
        error: str = Query(None),
    ):
        from routes.email_helpers import verify_oauth_state
        from src.github_service import oauth_exchange_code, fetch_github_user, save_account
        import os

        if error:
            return RedirectResponse(f"/?section=integrations&github_oauth_error={error}")
        if not code or not state:
            return RedirectResponse("/?section=integrations&github_oauth_error=missing_code")
        state_data = verify_oauth_state(state)
        if not state_data:
            return RedirectResponse("/?section=integrations&github_oauth_error=invalid_state")

        redirect_uri = (
            os.environ.get("GITHUB_OAUTH_REDIRECT_URI")
            or f"http://{request.headers.get('host', 'localhost:7000')}/api/github/oauth/callback"
        )
        try:
            token_data = await oauth_exchange_code(code, redirect_uri)
            access_token = token_data["access_token"]
            scope = token_data.get("scope", "")
            user_data = await fetch_github_user(access_token)
            save_account(None, access_token, scope, user_data)
        except GithubServiceError:
            logger.warning("GitHub OAuth exchange failed")
            return RedirectResponse("/?section=integrations&github_oauth_error=token_exchange_failed")
        except Exception:
            logger.exception("Unexpected error in GitHub OAuth callback")
            return RedirectResponse("/?section=integrations&github_oauth_error=unexpected")
        return RedirectResponse("/?section=integrations&github_oauth_success=1")

    # ── Repos ──

    @router.get("/repos")
    async def repos(request: Request):
        require_admin(request)
        from src.github_service import list_repos
        return {"repos": list_repos(None)}

    @router.get("/repos/remote")
    async def remote_repos(request: Request):
        require_admin(request)
        from src.github_service import list_github_repos
        try:
            return {"repos": await list_github_repos(None)}
        except GithubServiceError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.post("/repos/create")
    async def create(request: Request, body: CreateRepoBody):
        require_admin(request)
        from src.github_service import create_repo
        try:
            return await create_repo(None, body.name, body.description, body.private, body.org)
        except GithubServiceError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.post("/repos/clone")
    async def clone(request: Request, body: CloneRepoBody):
        require_admin(request)
        from src.github_service import clone_repo
        try:
            return await clone_repo(None, body.full_name_or_url, body.branch)
        except GithubServiceError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.post("/repos/upload")
    async def upload(request: Request, body: UploadRepoBody):
        require_admin(request)
        from src.github_service import upload_existing
        try:
            return await upload_existing(None, body.local_path, body.name, body.description, body.private, body.org)
        except GithubServiceError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.delete("/repos/{repo_id}")
    async def delete_repo(request: Request, repo_id: str, delete_files: bool = Query(False)):
        require_admin(request)
        from src.github_service import untrack_repo
        try:
            untrack_repo(None, repo_id, delete_files)
        except GithubServiceError as exc:
            raise HTTPException(404, str(exc)) from exc
        return {"ok": True}

    @router.get("/repos/{repo_id}/status")
    async def get_status(request: Request, repo_id: str):
        require_admin(request)
        from src.github_service import repo_status
        try:
            return await repo_status(None, repo_id)
        except GithubServiceError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.get("/repos/{repo_id}/diff")
    async def get_diff(request: Request, repo_id: str):
        require_admin(request)
        from src.github_service import repo_diff
        try:
            return {"diff": await repo_diff(None, repo_id)}
        except GithubServiceError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.post("/repos/{repo_id}/commit")
    async def commit(request: Request, repo_id: str, body: CommitBody):
        require_admin(request)
        from src.github_service import commit_changes
        try:
            return await commit_changes(None, repo_id, body.message)
        except GithubServiceError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.post("/repos/{repo_id}/push")
    async def push(request: Request, repo_id: str):
        require_admin(request)
        from src.github_service import push_changes
        try:
            return await push_changes(None, repo_id)
        except GithubServiceError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.post("/repos/{repo_id}/branch")
    async def branch(request: Request, repo_id: str, body: BranchBody):
        require_admin(request)
        from src.github_service import create_branch
        try:
            return await create_branch(None, repo_id, body.name)
        except GithubServiceError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.post("/repos/{repo_id}/pr")
    async def pr(request: Request, repo_id: str, body: PullRequestBody):
        require_admin(request)
        from src.github_service import open_pull_request
        try:
            return await open_pull_request(None, repo_id, body.title, body.body, body.base)
        except GithubServiceError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.get("/repos/{repo_id}/checks")
    async def checks(request: Request, repo_id: str, ref: str = Query(None)):
        require_admin(request)
        from src.github_service import pr_checks
        try:
            return await pr_checks(None, repo_id, ref)
        except GithubServiceError as exc:
            raise HTTPException(400, str(exc)) from exc

    return router
