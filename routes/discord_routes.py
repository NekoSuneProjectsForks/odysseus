"""Discord bot integration routes — /api/discord/*.

Admin-only, full stop (see THREAT_MODEL.md / todo.md §1/§7): a bot token grants
read access to message content and member presence in every server the bot
has joined, so configuring it, reading history, and sending messages are all
gated behind `require_admin`. Single shared config row, same pattern as
GitHub's connected account.

Routes here are thin: all real logic lives in services/discord/service.py.
"""

import logging

from fastapi import APIRouter, Request, HTTPException, Query
from pydantic import BaseModel

from core.middleware import require_admin
from services.discord.service import get_discord_service, DiscordServiceError

logger = logging.getLogger(__name__)


class ConfigBody(BaseModel):
    bot_token: str | None = None
    default_guild_id: str | None = None
    enabled: bool = True


class SendMessageBody(BaseModel):
    channel_id: str
    content: str


def setup_discord_routes() -> APIRouter:
    router = APIRouter(prefix="/api/discord", tags=["discord"])
    svc = get_discord_service()

    @router.get("/status")
    async def status(request: Request):
        require_admin(request)
        return await svc.status()

    @router.post("/config")
    async def save_config(request: Request, body: ConfigBody):
        require_admin(request)
        svc.save_config(body.bot_token, body.default_guild_id, body.enabled)
        if body.enabled:
            await svc.restart()
        else:
            await svc.stop()
        return await svc.status()

    @router.post("/disconnect")
    async def disconnect(request: Request):
        require_admin(request)
        await svc.stop()
        svc.clear_config()
        return {"ok": True}

    @router.get("/guilds")
    async def guilds(request: Request):
        require_admin(request)
        try:
            return {"guilds": await svc.list_guilds()}
        except DiscordServiceError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.get("/guilds/{guild_id}/summary")
    async def guild_summary(request: Request, guild_id: str):
        require_admin(request)
        try:
            return await svc.guild_summary(guild_id)
        except DiscordServiceError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.get("/guilds/{guild_id}/members")
    async def members(request: Request, guild_id: str, limit: int = Query(200, le=1000)):
        require_admin(request)
        try:
            return {"members": await svc.list_members(guild_id, limit)}
        except DiscordServiceError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.get("/guilds/{guild_id}/channels")
    async def channels(request: Request, guild_id: str):
        require_admin(request)
        try:
            return {"channels": await svc.list_channels(guild_id)}
        except DiscordServiceError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.get("/messages/search")
    async def search_messages(
        request: Request,
        query: str = Query(""),
        guild_id: str = Query(None),
        channel_id: str = Query(None),
        limit: int = Query(50, le=200),
    ):
        require_admin(request)
        try:
            return {"messages": await svc.search_messages(query, guild_id, channel_id, limit)}
        except DiscordServiceError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.post("/messages/send")
    async def send_message(request: Request, body: SendMessageBody):
        require_admin(request)
        try:
            return await svc.send_message(body.channel_id, body.content)
        except DiscordServiceError as exc:
            raise HTTPException(400, str(exc)) from exc

    return router
