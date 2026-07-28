"""GitHub agent tool (manage_github) — admin-only.

Thin dispatcher over src/github_service.py so the agent can drive the same
clone/create/commit/push/PR workflow available in Settings > Integrations >
GitHub. Mirrors the shape of admin_tools.py's manage_endpoints/manage_mcp/etc:
each action lives in one do_manage_github(content, owner) function, wrapped
into the registry via the shared _owner_adapter.
"""
import logging
from typing import Optional, Dict

from src.tool_utils import _parse_tool_args

logger = logging.getLogger(__name__)


async def do_manage_github(content: str, owner: Optional[str] = None) -> Dict:
    """Manage the GitHub integration: connect status, tracked repos, create/
    clone/upload, status/diff, commit/push, branches, and pull requests."""
    from src.github_service import GithubServiceError

    try:
        args = _parse_tool_args(content)
    except ValueError:
        return {"error": "Invalid JSON arguments", "exit_code": 1}

    action = args.get("action", "status")
    try:
        if action == "status":
            from src.github_service import account_status
            return {"response": "GitHub account status", **account_status(None), "exit_code": 0}

        if action == "list_repos":
            from src.github_service import list_repos
            repos = list_repos(None)
            return {"response": f"{len(repos)} tracked repos", "repos": repos, "exit_code": 0}

        if action == "list_remote_repos":
            from src.github_service import list_github_repos
            repos = await list_github_repos(None)
            return {"response": f"{len(repos)} repos on the connected GitHub account", "repos": repos, "exit_code": 0}

        if action == "create_repo":
            from src.github_service import create_repo
            name = args.get("name", "")
            if not name:
                return {"error": "name is required", "exit_code": 1}
            repo = await create_repo(
                None, name, args.get("description", ""), args.get("private", True), args.get("org"),
            )
            return {"response": f"Created and cloned {repo['full_name']} to {repo['local_path']}", "repo": repo, "exit_code": 0}

        if action == "clone_repo":
            from src.github_service import clone_repo
            target = args.get("full_name_or_url", "")
            if not target:
                return {"error": "full_name_or_url is required", "exit_code": 1}
            repo = await clone_repo(None, target, args.get("branch"))
            return {"response": f"Cloned {repo['full_name']} to {repo['local_path']}", "repo": repo, "exit_code": 0}

        if action == "upload_repo":
            from src.github_service import upload_existing
            local_path = args.get("local_path", "")
            name = args.get("name", "")
            if not local_path or not name:
                return {"error": "local_path and name are required", "exit_code": 1}
            repo = await upload_existing(
                None, local_path, name, args.get("description", ""), args.get("private", True), args.get("org"),
            )
            return {"response": f"Created {repo['full_name']} from {local_path} and pushed it", "repo": repo, "exit_code": 0}

        if action == "delete_repo":
            from src.github_service import untrack_repo
            repo_id = args.get("repo_id", "")
            if not repo_id:
                return {"error": "repo_id is required", "exit_code": 1}
            untrack_repo(None, repo_id, args.get("delete_files", False))
            return {"response": f"Stopped tracking repo {repo_id}", "exit_code": 0}

        # Everything below operates on a tracked repo_id.
        repo_id = args.get("repo_id", "")
        if action in (
            "repo_status", "repo_diff", "commit", "push", "create_branch", "create_pr", "checks",
        ) and not repo_id:
            return {"error": "repo_id is required", "exit_code": 1}

        if action == "repo_status":
            from src.github_service import repo_status
            result = await repo_status(None, repo_id)
            return {"response": f"{result['full_name']} @ {result['branch']}", **result, "exit_code": 0}

        if action == "repo_diff":
            from src.github_service import repo_diff
            diff = await repo_diff(None, repo_id)
            return {"response": diff or "No changes", "exit_code": 0}

        if action == "commit":
            from src.github_service import commit_changes
            message = args.get("message", "")
            result = await commit_changes(None, repo_id, message)
            return {"response": "Committed" if result.get("committed") else result.get("message", "Nothing to commit"), "exit_code": 0}

        if action == "push":
            from src.github_service import push_changes
            result = await push_changes(None, repo_id)
            return {"response": f"Pushed branch {result['branch']}", "exit_code": 0}

        if action == "create_branch":
            from src.github_service import create_branch
            name = args.get("branch_name", "") or args.get("name", "")
            if not name:
                return {"error": "branch_name is required", "exit_code": 1}
            result = await create_branch(None, repo_id, name)
            return {"response": f"Checked out new branch '{result['branch']}'", "exit_code": 0}

        if action == "create_pr":
            from src.github_service import open_pull_request
            title = args.get("title", "")
            if not title:
                return {"error": "title is required", "exit_code": 1}
            result = await open_pull_request(None, repo_id, title, args.get("body", ""), args.get("base"))
            return {"response": f"Opened PR #{result['number']}: {result['url']}", **result, "exit_code": 0}

        if action == "checks":
            from src.github_service import pr_checks
            result = await pr_checks(None, repo_id, args.get("ref"))
            return {"response": f"{result.get('total_count', 0)} check runs", **result, "exit_code": 0}

        return {"error": f"Unknown action: {action}", "exit_code": 1}

    except GithubServiceError as exc:
        return {"error": str(exc), "exit_code": 1}
    except Exception as exc:
        logger.error(f"manage_github error: {exc}")
        return {"error": str(exc), "exit_code": 1}


def _owner_adapter(fn):
    async def _execute(content: str, ctx: dict) -> dict:
        return await fn(content, ctx.get("owner"))
    return _execute


GITHUB_TOOL_HANDLERS = {
    "manage_github": _owner_adapter(do_manage_github),
}
