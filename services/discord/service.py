"""
services/discord/service.py

Discord bot integration — admin-only (see THREAT_MODEL.md; todo.md §1/§7).
Wraps discord.py (optional dependency, see requirements-optional.txt) behind
a small async service so routes/discord_routes.py and
src/agent_tools/discord_tools.py both call the same code.

The bot connects lazily: nothing happens until an admin saves a token and
enables it in Settings > Integrations > Discord Bot. discord.py is imported
lazily inside methods, never at module top-level, so the app boots fine and
degrades to a clear "not installed" error when the package is absent.

Reading message history and "who's online" both need PRIVILEGED intents
(Message Content, Server Members, Presence) enabled on the bot in the
Discord Developer Portal — without that, Discord silently omits the data
even though the gateway connection itself succeeds.
"""

import asyncio
import logging
from typing import Optional, Dict, Any, List

logger = logging.getLogger(__name__)


class DiscordServiceError(Exception):
    """Raised for user-facing errors (not installed, not configured, Discord API failure)."""


def _discord_available() -> bool:
    try:
        import discord  # noqa: F401
        return True
    except ImportError:
        return False


class DiscordBotService:
    def __init__(self) -> None:
        self._client = None
        self._task: Optional[asyncio.Task] = None
        self._ready = asyncio.Event()
        self._start_error: Optional[str] = None

    # ------------------------------------------------------------------
    # Config (DB-backed, single shared row — see core.database.DiscordConfig)
    # ------------------------------------------------------------------

    def _load_config(self):
        from core.database import SessionLocal, DiscordConfig
        db = SessionLocal()
        try:
            return db.query(DiscordConfig).first()
        finally:
            db.close()

    def get_config(self) -> Dict[str, Any]:
        cfg = self._load_config()
        if not cfg:
            return {"configured": False, "enabled": False, "default_guild_id": None}
        return {
            "configured": bool(cfg.bot_token),
            "enabled": cfg.enabled,
            "default_guild_id": cfg.default_guild_id,
        }

    def save_config(self, bot_token: Optional[str], default_guild_id: Optional[str], enabled: bool) -> None:
        import uuid
        from core.database import SessionLocal, DiscordConfig
        db = SessionLocal()
        try:
            cfg = db.query(DiscordConfig).first()
            if not cfg:
                cfg = DiscordConfig(id=uuid.uuid4().hex[:12])
                db.add(cfg)
            if bot_token:
                cfg.bot_token = bot_token  # EncryptedText encrypts on write
            cfg.default_guild_id = default_guild_id or None
            cfg.enabled = bool(enabled)
            db.commit()
        finally:
            db.close()

    def clear_config(self) -> None:
        from core.database import SessionLocal, DiscordConfig
        db = SessionLocal()
        try:
            db.query(DiscordConfig).delete()
            db.commit()
        finally:
            db.close()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the bot if configured+enabled. Safe to call at app startup —
        no-op if unconfigured, and failures are logged, never raised, so a bad
        token can't crash the whole app."""
        if self._task and not self._task.done():
            return
        if not _discord_available():
            logger.info("Discord bot not started: discord.py is not installed (optional dependency)")
            return
        cfg = self._load_config()
        if not cfg or not cfg.enabled or not cfg.bot_token:
            return

        import discord

        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.presences = True
        client = discord.Client(intents=intents)
        self._client = client
        self._ready.clear()
        self._start_error = None

        @client.event
        async def on_ready():
            self._ready.set()
            logger.info("Discord bot connected as %s", client.user)

        async def _run():
            try:
                await client.start(cfg.bot_token)
            except Exception as exc:
                self._start_error = str(exc)
                logger.warning("Discord bot connection failed: %s", exc)

        self._task = asyncio.create_task(_run())

    async def stop(self) -> None:
        if self._client:
            try:
                await self._client.close()
            except Exception:
                pass
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._client = None
        self._task = None
        self._ready.clear()

    async def restart(self) -> None:
        await self.stop()
        await self.start()

    def is_connected(self) -> bool:
        return bool(self._client and self._ready.is_set())

    async def status(self) -> Dict[str, Any]:
        available = _discord_available()
        cfg = self.get_config()
        result: Dict[str, Any] = {
            "installed": available,
            "configured": cfg["configured"],
            "enabled": cfg["enabled"],
            "connected": self.is_connected(),
            "error": self._start_error,
        }
        if self.is_connected():
            result["bot_user"] = str(self._client.user)
            result["guild_count"] = len(self._client.guilds)
        return result

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _require_client(self):
        if not _discord_available():
            raise DiscordServiceError(
                "discord.py is not installed. Add it via requirements-optional.txt and restart."
            )
        if not self.is_connected():
            raise DiscordServiceError(
                "Discord bot is not connected. Configure and enable it in Settings > Integrations > Discord Bot."
            )
        return self._client

    def _get_guild(self, guild_id: Optional[str]):
        client = self._require_client()
        gid = guild_id or self.get_config().get("default_guild_id")
        if not gid:
            raise DiscordServiceError("guild_id is required (no default guild configured)")
        guild = client.get_guild(int(gid))
        if not guild:
            raise DiscordServiceError(f"Bot is not in guild {gid} (or it hasn't finished syncing yet)")
        return guild

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    async def list_guilds(self) -> List[Dict[str, Any]]:
        client = self._require_client()
        return [
            {"id": str(g.id), "name": g.name, "member_count": g.member_count}
            for g in client.guilds
        ]

    async def guild_summary(self, guild_id: Optional[str] = None) -> Dict[str, Any]:
        guild = self._get_guild(guild_id)
        online = idle = dnd = offline = 0
        for member in guild.members:
            status = str(getattr(member, "status", "offline"))
            if status == "online":
                online += 1
            elif status == "idle":
                idle += 1
            elif status == "dnd":
                dnd += 1
            else:
                offline += 1
        return {
            "id": str(guild.id),
            "name": guild.name,
            "member_count": guild.member_count,
            "online": online,
            "idle": idle,
            "dnd": dnd,
            "offline": offline,
        }

    async def list_members(self, guild_id: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
        guild = self._get_guild(guild_id)
        members = []
        for member in list(guild.members)[:limit]:
            members.append({
                "id": str(member.id),
                "name": str(member),
                "display_name": member.display_name,
                "status": str(getattr(member, "status", "offline")),
                "bot": member.bot,
            })
        return members

    async def list_channels(self, guild_id: Optional[str] = None) -> List[Dict[str, Any]]:
        import discord
        guild = self._get_guild(guild_id)
        return [
            {"id": str(ch.id), "name": ch.name}
            for ch in guild.channels
            if isinstance(ch, discord.TextChannel)
        ]

    async def search_messages(
        self, query: str, guild_id: Optional[str] = None,
        channel_id: Optional[str] = None, limit: int = 50, history_per_channel: int = 500,
    ) -> List[Dict[str, Any]]:
        import discord
        client = self._require_client()
        query_lower = (query or "").lower().strip()

        if channel_id:
            channel = client.get_channel(int(channel_id))
            if not channel:
                raise DiscordServiceError(f"Channel {channel_id} not found")
            channels = [channel]
        else:
            guild = self._get_guild(guild_id)
            channels = [ch for ch in guild.channels if isinstance(ch, discord.TextChannel)]

        matches: List[Dict[str, Any]] = []
        for channel in channels:
            if len(matches) >= limit:
                break
            try:
                async for message in channel.history(limit=history_per_channel):
                    if not query_lower or query_lower in message.content.lower():
                        matches.append({
                            "channel": channel.name,
                            "channel_id": str(channel.id),
                            "author": str(message.author),
                            "content": message.content,
                            "timestamp": message.created_at.isoformat(),
                            "jump_url": message.jump_url,
                        })
                        if len(matches) >= limit:
                            break
            except Exception as exc:
                logger.debug("Skipping channel %s in message search: %s", channel.name, exc)
        return matches

    async def send_message(self, channel_id: str, content: str) -> Dict[str, Any]:
        client = self._require_client()
        channel = client.get_channel(int(channel_id))
        if not channel:
            raise DiscordServiceError(f"Channel {channel_id} not found (bot may lack access)")
        message = await channel.send(content)
        return {"id": str(message.id), "channel": channel.name, "jump_url": message.jump_url}


_service: Optional[DiscordBotService] = None


def get_discord_service() -> DiscordBotService:
    global _service
    if _service is None:
        _service = DiscordBotService()
    return _service
