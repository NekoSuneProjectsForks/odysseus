"""Discord agent tool (manage_discord) — admin-only.

Thin dispatcher over services/discord/service.py so the agent can check
status, list guilds/members/channels, search message history, and send
messages — the same actions available in Settings > Integrations > Discord Bot.
"""
import logging
from typing import Optional, Dict

from src.tool_utils import _parse_tool_args

logger = logging.getLogger(__name__)


async def do_manage_discord(content: str, owner: Optional[str] = None) -> Dict:
    """Manage the Discord bot: status, list guilds/members/channels, search
    message history, and send messages."""
    from services.discord.service import get_discord_service, DiscordServiceError

    try:
        args = _parse_tool_args(content)
    except ValueError:
        return {"error": "Invalid JSON arguments", "exit_code": 1}

    action = args.get("action", "status")
    svc = get_discord_service()
    try:
        if action == "status":
            return {"response": "Discord bot status", **await svc.status(), "exit_code": 0}

        if action == "list_guilds":
            guilds = await svc.list_guilds()
            return {"response": f"{len(guilds)} guild(s)", "guilds": guilds, "exit_code": 0}

        if action == "guild_summary":
            summary = await svc.guild_summary(args.get("guild_id"))
            return {
                "response": f"{summary['name']}: {summary['member_count']} members, {summary['online']} online",
                **summary, "exit_code": 0,
            }

        if action == "list_members":
            members = await svc.list_members(args.get("guild_id"), args.get("limit", 200))
            return {"response": f"{len(members)} member(s)", "members": members, "exit_code": 0}

        if action == "list_channels":
            channels = await svc.list_channels(args.get("guild_id"))
            return {"response": f"{len(channels)} channel(s)", "channels": channels, "exit_code": 0}

        if action == "search_messages":
            messages = await svc.search_messages(
                args.get("query", ""), args.get("guild_id"), args.get("channel_id"), args.get("limit", 50),
            )
            return {"response": f"{len(messages)} matching message(s)", "messages": messages, "exit_code": 0}

        if action == "send_message":
            channel_id = args.get("channel_id", "")
            text = args.get("content", "")
            if not channel_id or not text:
                return {"error": "channel_id and content are required", "exit_code": 1}
            result = await svc.send_message(channel_id, text)
            return {"response": f"Sent to #{result['channel']}", **result, "exit_code": 0}

        return {"error": f"Unknown action: {action}", "exit_code": 1}

    except DiscordServiceError as exc:
        return {"error": str(exc), "exit_code": 1}
    except Exception as exc:
        logger.error(f"manage_discord error: {exc}")
        return {"error": str(exc), "exit_code": 1}


def _owner_adapter(fn):
    async def _execute(content: str, ctx: dict) -> dict:
        return await fn(content, ctx.get("owner"))
    return _execute


DISCORD_TOOL_HANDLERS = {
    "manage_discord": _owner_adapter(do_manage_discord),
}
