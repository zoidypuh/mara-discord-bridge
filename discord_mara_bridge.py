#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
import os
import signal
from typing import Any

from aiohttp import WSMsgType, web
import discord


def _csv_ids(value: str | None) -> set[int]:
    out: set[int] = set()
    for item in str(value or "").replace(";", ",").split(","):
        text = item.strip()
        if not text:
            continue
        out.add(int(text))
    return out


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _snowflake(value: Any, *, field: str) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise web.HTTPBadRequest(text=f"{field} must be a Discord snowflake id") from exc


def _message_reference_dict(reference: discord.MessageReference | None) -> dict[str, Any] | None:
    if reference is None:
        return None
    return {
        "message_id": str(reference.message_id) if reference.message_id else None,
        "channel_id": str(reference.channel_id) if reference.channel_id else None,
        "guild_id": str(reference.guild_id) if reference.guild_id else None,
    }


def _attachment_dict(attachment: discord.Attachment) -> dict[str, Any]:
    return {
        "id": str(attachment.id),
        "filename": attachment.filename,
        "url": attachment.url,
        "content_type": attachment.content_type,
        "size": attachment.size,
    }


def _author_dict(author: discord.abc.User) -> dict[str, Any]:
    return {
        "id": str(author.id),
        "name": author.name,
        "display_name": getattr(author, "display_name", author.name),
        "bot": bool(author.bot),
    }


def message_to_event(message: discord.Message, *, event_type: str = "message_create") -> dict[str, Any]:
    referenced_message = None
    resolved = getattr(message.reference, "resolved", None) if message.reference else None
    if isinstance(resolved, discord.Message):
        referenced_message = {
            "id": str(resolved.id),
            "channel_id": str(resolved.channel.id),
            "author": _author_dict(resolved.author),
            "content": resolved.content,
            "jump_url": resolved.jump_url,
        }

    guild = message.guild
    channel = message.channel
    return {
        "type": event_type,
        "event_id": f"{event_type}:{message.id}:{int(datetime.now(timezone.utc).timestamp() * 1000)}",
        "seen_at": _utc_now(),
        "id": str(message.id),
        "guild_id": str(guild.id) if guild else None,
        "guild_name": guild.name if guild else None,
        "channel_id": str(channel.id),
        "channel_name": getattr(channel, "name", None),
        "author": _author_dict(message.author),
        "content": message.content,
        "clean_content": message.clean_content,
        "created_at": message.created_at.astimezone(timezone.utc).isoformat(),
        "edited_at": message.edited_at.astimezone(timezone.utc).isoformat() if message.edited_at else None,
        "jump_url": message.jump_url,
        "attachments": [_attachment_dict(item) for item in message.attachments],
        "embeds_count": len(message.embeds),
        "mentions": [_author_dict(user) for user in message.mentions],
        "reference": _message_reference_dict(message.reference),
        "referenced_message": referenced_message,
        "is_reply": message.reference is not None,
    }


class EventBus:
    def __init__(self, max_events: int = 500):
        self.events: deque[dict[str, Any]] = deque(maxlen=max_events)
        self.clients: set[web.WebSocketResponse] = set()

    async def publish(self, event: dict[str, Any]) -> None:
        self.events.append(event)
        dead: list[web.WebSocketResponse] = []
        for ws in self.clients:
            try:
                await ws.send_json(event)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        capped = max(0, min(int(limit), self.events.maxlen or 500))
        return list(self.events)[-capped:]


@dataclass
class BridgeConfig:
    host: str
    port: int
    api_key: str
    allowed_channel_ids: set[int]
    allowed_user_ids: set[int]
    include_self: bool


class MaraDiscordBot(discord.Client):
    def __init__(self, *, config: BridgeConfig, bus: EventBus):
        super().__init__()
        self.config = config
        self.bus = bus

    async def on_ready(self) -> None:
        await self.bus.publish(
            {
                "type": "ready",
                "seen_at": _utc_now(),
                "user": _author_dict(self.user) if self.user else None,
                "guilds": [{"id": str(guild.id), "name": guild.name} for guild in self.guilds],
            }
        )

    def _message_allowed(self, message: discord.Message) -> bool:
        if not self.config.include_self and self.user and message.author.id == self.user.id:
            return False
        if self.config.allowed_channel_ids and message.channel.id not in self.config.allowed_channel_ids:
            return False
        if self.config.allowed_user_ids and message.author.id not in self.config.allowed_user_ids:
            return False
        return True

    async def on_message(self, message: discord.Message) -> None:
        if self._message_allowed(message):
            event = message_to_event(message)
            event["mentions_self"] = bool(self.user and self.user in message.mentions)
            await self.bus.publish(event)

    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        if self._message_allowed(after):
            event = message_to_event(after, event_type="message_edit")
            event["before_content"] = before.content
            await self.bus.publish(event)

    async def on_message_delete(self, message: discord.Message) -> None:
        if self._message_allowed(message):
            await self.bus.publish(message_to_event(message, event_type="message_delete"))

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if self.config.allowed_channel_ids and payload.channel_id not in self.config.allowed_channel_ids:
            return
        if self.config.allowed_user_ids and payload.user_id not in self.config.allowed_user_ids:
            return
        await self.bus.publish(
            {
                "type": "reaction_add",
                "seen_at": _utc_now(),
                "guild_id": str(payload.guild_id) if payload.guild_id else None,
                "channel_id": str(payload.channel_id),
                "message_id": str(payload.message_id),
                "user_id": str(payload.user_id),
                "emoji": str(payload.emoji),
            }
        )

    async def get_messageable(self, channel_id: int) -> discord.abc.Messageable:
        channel = self.get_channel(channel_id)
        if channel is None:
            channel = await self.fetch_channel(channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            raise web.HTTPBadRequest(text=f"channel {channel_id} is not messageable")
        return channel


def allowed_mentions_from_payload(value: Any) -> discord.AllowedMentions:
    mode = str(value or "users").strip().lower()
    if mode in {"none", "false", "off"}:
        return discord.AllowedMentions.none()
    if mode in {"all", "true", "on"}:
        return discord.AllowedMentions(everyone=True, users=True, roles=True, replied_user=True)
    return discord.AllowedMentions(everyone=False, users=True, roles=False, replied_user=False)


async def read_json(request: web.Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except Exception as exc:
        raise web.HTTPBadRequest(text="request body must be JSON") from exc
    if not isinstance(payload, dict):
        raise web.HTTPBadRequest(text="request body must be a JSON object")
    return payload


def create_app(bot: MaraDiscordBot, bus: EventBus, config: BridgeConfig) -> web.Application:
    @web.middleware
    async def auth_middleware(request: web.Request, handler):
        if config.api_key and request.path != "/health":
            header = request.headers.get("Authorization", "")
            token = header.removeprefix("Bearer ").strip()
            supplied = token or request.headers.get("X-Bridge-Key", "").strip() or request.query.get("key", "").strip()
            if supplied != config.api_key:
                raise web.HTTPUnauthorized(text="missing or invalid bridge key")
        return await handler(request)

    app = web.Application(middlewares=[auth_middleware])

    async def health(_request: web.Request) -> web.Response:
        return web.json_response(
            {
                "ok": True,
                "discord_ready": bot.is_ready(),
                "user": _author_dict(bot.user) if bot.user else None,
                "guild_count": len(bot.guilds),
                "event_count": len(bus.events),
                "ws_clients": len(bus.clients),
            }
        )

    async def events(request: web.Request) -> web.Response:
        limit = int(request.query.get("limit", "50"))
        return web.json_response({"events": bus.recent(limit)})

    async def ws_feed(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        bus.clients.add(ws)
        await ws.send_json({"type": "hello", "seen_at": _utc_now(), "recent": bus.recent(25)})
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT and msg.data.strip().lower() == "ping":
                    await ws.send_str("pong")
                elif msg.type in {WSMsgType.ERROR, WSMsgType.CLOSE, WSMsgType.CLOSED}:
                    break
        finally:
            bus.clients.discard(ws)
        return ws

    async def guilds(_request: web.Request) -> web.Response:
        return web.json_response(
            {
                "guilds": [
                    {"id": str(guild.id), "name": guild.name, "member_count": guild.member_count}
                    for guild in bot.guilds
                ]
            }
        )

    async def channels(request: web.Request) -> web.Response:
        guild_id = request.query.get("guild_id")
        guild_list = [bot.get_guild(int(guild_id))] if guild_id else bot.guilds
        out: list[dict[str, Any]] = []
        for guild in guild_list:
            if guild is None:
                continue
            for channel in guild.text_channels:
                out.append(
                    {
                        "guild_id": str(guild.id),
                        "guild_name": guild.name,
                        "id": str(channel.id),
                        "name": channel.name,
                        "category": channel.category.name if channel.category else None,
                        "jump_hint": f"https://discord.com/channels/{guild.id}/{channel.id}",
                    }
                )
        return web.json_response({"channels": out})

    async def history(request: web.Request) -> web.Response:
        channel_id = _snowflake(request.match_info["channel_id"], field="channel_id")
        limit = max(1, min(int(request.query.get("limit", "25")), 100))
        channel = await bot.get_messageable(channel_id)
        messages = [message_to_event(item, event_type="history") async for item in channel.history(limit=limit)]
        messages.reverse()
        return web.json_response({"messages": messages})

    async def send(request: web.Request) -> web.Response:
        payload = await read_json(request)
        channel_id = _snowflake(payload.get("channel_id"), field="channel_id")
        if config.allowed_channel_ids and channel_id not in config.allowed_channel_ids:
            raise web.HTTPForbidden(text=f"channel {channel_id} is not in allowed channels")
        content = str(payload.get("content") or "").strip()
        if not content:
            raise web.HTTPBadRequest(text="content is required")
        channel = await bot.get_messageable(channel_id)
        kwargs: dict[str, Any] = {
            "allowed_mentions": allowed_mentions_from_payload(payload.get("allowed_mentions")),
        }
        if payload.get("reply_to_message_id"):
            reply_to = _snowflake(payload.get("reply_to_message_id"), field="reply_to_message_id")
            if hasattr(channel, "get_partial_message"):
                kwargs["reference"] = channel.get_partial_message(reply_to)
                kwargs["mention_author"] = bool(payload.get("mention_author", False))
        message = await channel.send(content, **kwargs)
        return web.json_response({"ok": True, "message": message_to_event(message, event_type="sent")})

    async def dm(request: web.Request) -> web.Response:
        payload = await read_json(request)
        user_id = _snowflake(payload.get("user_id"), field="user_id")
        if config.allowed_user_ids and user_id not in config.allowed_user_ids:
            raise web.HTTPForbidden(text=f"user {user_id} is not in allowed users")
        content = str(payload.get("content") or "").strip()
        if not content:
            raise web.HTTPBadRequest(text="content is required")
        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
        message = await user.send(
            content,
            allowed_mentions=allowed_mentions_from_payload(payload.get("allowed_mentions")),
        )
        return web.json_response({"ok": True, "message": message_to_event(message, event_type="sent_dm")})

    async def typing(request: web.Request) -> web.Response:
        payload = await read_json(request)
        channel_id = _snowflake(payload.get("channel_id"), field="channel_id")
        seconds = max(0.1, min(float(payload.get("seconds", 1.0)), 10.0))
        channel = await bot.get_messageable(channel_id)
        async with channel.typing():
            await asyncio.sleep(seconds)
        return web.json_response({"ok": True})

    async def react(request: web.Request) -> web.Response:
        payload = await read_json(request)
        channel_id = _snowflake(payload.get("channel_id"), field="channel_id")
        message_id = _snowflake(payload.get("message_id"), field="message_id")
        emoji = str(payload.get("emoji") or "").strip()
        if not emoji:
            raise web.HTTPBadRequest(text="emoji is required")
        channel = await bot.get_messageable(channel_id)
        if not hasattr(channel, "fetch_message"):
            raise web.HTTPBadRequest(text="channel does not support fetch_message")
        message = await channel.fetch_message(message_id)
        await message.add_reaction(emoji)
        return web.json_response({"ok": True})

    app.router.add_get("/health", health)
    app.router.add_get("/events", events)
    app.router.add_get("/ws", ws_feed)
    app.router.add_get("/guilds", guilds)
    app.router.add_get("/channels", channels)
    app.router.add_get("/messages/{channel_id}", history)
    app.router.add_post("/send", send)
    app.router.add_post("/dm", dm)
    app.router.add_post("/typing", typing)
    app.router.add_post("/react", react)
    return app


async def run(args: argparse.Namespace) -> None:
    token = args.token or os.environ.get("DISCORD_USER_TOKEN", "")
    if not token:
        raise SystemExit("Set DISCORD_USER_TOKEN or pass --token.")

    config = BridgeConfig(
        host=args.host,
        port=args.port,
        api_key=args.api_key or os.environ.get("MARA_DISCORD_BRIDGE_KEY", ""),
        allowed_channel_ids=_csv_ids(args.allowed_channels or os.environ.get("DISCORD_ALLOWED_CHANNEL_IDS")),
        allowed_user_ids=_csv_ids(args.allowed_users or os.environ.get("DISCORD_ALLOWED_USER_IDS")),
        include_self=args.include_self,
    )

    bus = EventBus(max_events=args.max_events)
    bot = MaraDiscordBot(config=config, bus=bus)
    app = create_app(bot, bus, config)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, config.host, config.port)
    await site.start()
    print(f"HTTP API listening on http://{config.host}:{config.port}")
    if config.api_key:
        print("HTTP API auth enabled: use Authorization: Bearer <MARA_DISCORD_BRIDGE_KEY>")
    else:
        print("HTTP API auth disabled. Bind host is local by default.")

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    bot_task = asyncio.create_task(bot.start(token))
    stop_task = asyncio.create_task(stop_event.wait())
    done, pending = await asyncio.wait({bot_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    if bot_task in done:
        exc = bot_task.exception()
        if exc:
            raise exc
    await bot.close()
    await runner.cleanup()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local Discord bridge for Mara.")
    parser.add_argument("--token", default="", help="Discord user token. Prefer DISCORD_USER_TOKEN env.")
    parser.add_argument("--host", default=os.environ.get("DISCORD_BRIDGE_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("DISCORD_BRIDGE_PORT", "8787")))
    parser.add_argument("--api-key", default="", help="Local API key. Prefer MARA_DISCORD_BRIDGE_KEY env.")
    parser.add_argument("--allowed-channels", default="", help="Comma-separated channel ids to watch/send to.")
    parser.add_argument("--allowed-users", default="", help="Comma-separated user ids to watch/DM.")
    parser.add_argument("--include-self", action="store_true", help="Also publish your own messages in the feed.")
    parser.add_argument("--max-events", type=int, default=int(os.environ.get("DISCORD_BRIDGE_MAX_EVENTS", "500")))
    return parser.parse_args()


def main() -> None:
    asyncio.run(run(parse_args()))


if __name__ == "__main__":
    main()
