#!/usr/bin/env python3
"""Discord agent-reply watcher for Mara Discord Bridge.

Usage:
  discord.py [--duration 0] [--replace] [channel_id] [prompt...]
  discord.py --resolve-only [channel_id]
  discord.py --stop [channel_id]

[channel_id] may also be a guild/server ID; [prompt] is the message to post. Duration <=0 watches until stopped.

Programmatic only: REST + Discord Gateway WebSocket. No browser.
Reads DISCORD_USER_TOKEN first, then DISCORD.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
import urllib.error
import urllib.request
import re
from pathlib import Path
from typing import Any

from websockets.sync.client import connect

API = "https://discord.com/api/v10"
GATEWAY = "wss://gateway.discord.gg/?v=10&encoding=json"
STATE_DIR = Path.home() / ".hermes" / "state"
TEXT_CHANNEL_TYPES = {0, 5}  # guild text, announcement
PREFERRED_CHANNEL_NAMES = ("general", "degen-trading", "chat", "lounge", "main")
CHANNEL_ALIASES_PATH = Path.home() / ".hermes" / "discord-channel-aliases.json"


def resolve_channel_alias(target: str) -> str:
    """Map friendly aliases like 'fomobros' to Discord channel IDs when configured."""
    raw = (target or "").strip()
    if raw.isdigit():
        return raw
    try:
        data = json.loads(CHANNEL_ALIASES_PATH.read_text(encoding="utf-8"))
    except Exception:
        return raw
    aliases = data.get("aliases") or {}
    return str(aliases.get(raw) or aliases.get(raw.lower()) or raw)


def emit(prefix: str | None, obj: dict[str, Any]) -> None:
    line = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    print(f"{prefix} {line}" if prefix else line, flush=True)


def get_token() -> str:
    tok = os.environ.get("DISCORD_USER_TOKEN") or os.environ.get("DISCORD")
    if not tok:
        emit("BLOCKED", {"error": "token_missing", "detail": "set DISCORD_USER_TOKEN or DISCORD"})
        raise SystemExit(1)
    return tok




def load_dotenv_value(key: str) -> str:
    for path in (Path.home() / ".hermes" / ".env",):
        try:
            for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() == key:
                    return v.strip().strip('"').strip("'")
        except FileNotFoundError:
            continue
    return ""


def api_server_key() -> str:
    return os.environ.get("API_SERVER_KEY") or load_dotenv_value("API_SERVER_KEY")


def api_server_url() -> str:
    host = os.environ.get("API_SERVER_HOST") or load_dotenv_value("API_SERVER_HOST") or "127.0.0.1"
    port = os.environ.get("API_SERVER_PORT") or load_dotenv_value("API_SERVER_PORT") or "8642"
    return f"http://{host}:{port}/v1/chat/completions"

def likely_needs_research(text: str) -> bool:
    text_l = (text or "").lower()
    needles = (
        "when ", "what time", "schedule", "event", "events", "next ",
        "today", "tomorrow", "gmt", "timezone", "latest", "current",
        "news", "price", "weather", "calendar", "look up", "research",
        "who won", "release", "is out", "f1",
    )
    return any(item in text_l for item in needles)


def research_ack_text() -> str:
    return "on it — I’ll look that up and report back."

def rest(method: str, path: str, payload: dict[str, Any] | None = None, *, fail: bool = True) -> Any:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {
        "Authorization": get_token(),
        "User-Agent": "Mozilla/5.0",
        "Origin": "https://discord.com",
    }
    if payload is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(API + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            body = json.loads(raw)
        except Exception:
            body = raw[:1000]
        if fail:
            emit("BLOCKED", {"error": "discord_http", "method": method, "path": path, "status": e.code, "body": body})
            raise SystemExit(1)
        return {"_http_error": e.code, "body": body}


def me() -> dict[str, Any]:
    return rest("GET", "/users/@me")


def resolve_target(target_id: str) -> dict[str, Any]:
    """Resolve a channel ID or guild ID to {guild_id, channel_id, channel_name}."""
    ch = rest("GET", f"/channels/{target_id}", fail=False)
    if isinstance(ch, dict) and not ch.get("_http_error") and ch.get("id"):
        return {
            "target_kind": "channel",
            "target_id": target_id,
            "guild_id": str(ch.get("guild_id") or "@me"),
            "channel_id": str(ch.get("id")),
            "channel_name": ch.get("name") or ch.get("id"),
            "channel_type": ch.get("type"),
        }

    channels = rest("GET", f"/guilds/{target_id}/channels")
    text = [c for c in channels if c.get("type") in TEXT_CHANNEL_TYPES]
    if not text:
        emit("BLOCKED", {"error": "no_text_channels", "guild_id": target_id})
        raise SystemExit(1)

    def rank(c: dict[str, Any]) -> tuple[int, int, str]:
        name = (c.get("name") or "").lower()
        try:
            pref = PREFERRED_CHANNEL_NAMES.index(name)
        except ValueError:
            pref = 999
        return (pref, int(c.get("position") or 9999), name)

    chosen = sorted(text, key=rank)[0]
    return {
        "target_kind": "guild",
        "target_id": target_id,
        "guild_id": str(target_id),
        "channel_id": str(chosen.get("id")),
        "channel_name": chosen.get("name"),
        "channel_type": chosen.get("type"),
        "available_text_channels": [
            {"id": str(c.get("id")), "name": c.get("name"), "type": c.get("type"), "position": c.get("position")}
            for c in sorted(text, key=lambda x: int(x.get("position") or 9999))[:20]
        ],
    }


def send(channel_id: str, content: str, reply_to: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"content": content, "tts": False}
    if reply_to:
        payload["message_reference"] = {"message_id": reply_to, "channel_id": channel_id, "fail_if_not_exists": False}
        payload["allowed_mentions"] = {"parse": [], "replied_user": True}
    return rest("POST", f"/channels/{channel_id}/messages", payload)


def state_base(guild_id: str, channel_id: str) -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR / f"discord_skill_{guild_id}_{channel_id}"


def load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def stop_existing(pid_path: Path, *, missing_ok: bool = True) -> bool:
    if not pid_path.exists():
        if not missing_ok:
            emit("STOPPED", {"status": "not_running", "pid_path": str(pid_path)})
        return False
    try:
        pid = int(pid_path.read_text().strip() or "0")
    except Exception:
        pid_path.unlink(missing_ok=True)
        if not missing_ok:
            emit("STOPPED", {"status": "stale_pid_removed", "pid_path": str(pid_path)})
        return False
    stopped = False
    if pid and process_alive(pid) and pid != os.getpid():
        try:
            os.kill(pid, signal.SIGTERM)
            time.sleep(0.5)
            if process_alive(pid):
                os.kill(pid, signal.SIGKILL)
            stopped = True
        except Exception as e:
            emit("BLOCKED", {"error": "failed_to_stop_existing_watcher", "pid": pid, "message": str(e)})
            raise SystemExit(1)
    pid_path.unlink(missing_ok=True)
    if not missing_ok:
        emit("STOPPED", {"status": "stopped" if stopped else "not_running", "pid": pid, "pid_path": str(pid_path)})
    return stopped


def replace_existing(pid_path: Path) -> None:
    stop_existing(pid_path, missing_ok=True)


def write_pid(pid_path: Path) -> None:
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(os.getpid()) + "\n", encoding="utf-8")


def snowflake(v: Any) -> int:
    try:
        return int(v or 0)
    except Exception:
        return 0


def identify_payload(tok: str) -> dict[str, Any]:
    return {
        "op": 2,
        "d": {
            "token": tok,
            "capabilities": 16381,
            "properties": {
                "os": "Linux",
                "browser": "Chrome",
                "device": "",
                "system_locale": "en-US",
                "browser_user_agent": "Mozilla/5.0",
                "browser_version": "120.0.0.0",
                "os_version": "",
                "referrer": "",
                "referring_domain": "",
                "release_channel": "stable",
                "client_build_number": 9999,
                "client_event_source": None,
            },
            "presence": {"status": "online", "since": 0, "activities": [], "afk": False},
            "compress": False,
            "client_state": {"guild_versions": {}},
        },
    }


class Watcher:
    def __init__(self, resolved: dict[str, Any], duration: float, reply_template: str, verbose: bool = False, agent_reply: bool = False):
        self.guild_id = str(resolved["guild_id"])
        self.channel_id = str(resolved["channel_id"])
        self.channel_name = resolved.get("channel_name")
        self.duration = duration
        self.reply_template = reply_template
        self.verbose = verbose
        self.agent_reply = agent_reply
        self.me = me()
        self.me_id = str(self.me.get("id"))
        self.started = time.time()
        self.running = True
        self.seq: int | None = None
        base = state_base(self.guild_id, self.channel_id)
        self.state_path = base.with_suffix(".json")
        self.pid_path = base.with_suffix(".pid")
        self.state = load_json(self.state_path)
        self.watch_ids: set[str] = set(str(x) for x in self.state.get("watch_message_ids", []))
        self.replied_to: set[str] = set(str(x) for x in self.state.get("replied_to", []))

    def persist(self) -> None:
        self.state.update({
            "guild_id": self.guild_id,
            "channel_id": self.channel_id,
            "channel_name": self.channel_name,
            "me_id": self.me_id,
            "watch_message_ids": sorted(self.watch_ids, key=snowflake),
            "replied_to": sorted(self.replied_to, key=snowflake),
            "pid": os.getpid(),
            "updated_at": time.time(),
        })
        save_json(self.state_path, self.state)

    def should_stop(self) -> bool:
        return self.duration > 0 and time.time() - self.started >= self.duration

    def post_starter(self, content: str) -> dict[str, Any]:
        msg = send(self.channel_id, content)
        self.watch_ids.add(str(msg.get("id")))
        self.persist()
        emit("POSTED", {
            "guild_id": self.guild_id,
            "channel_id": self.channel_id,
            "channel_name": self.channel_name,
            "message_id": msg.get("id"),
            "content": msg.get("content"),
            "jump_url": f"https://discord.com/channels/{self.guild_id}/{self.channel_id}/{msg.get('id')}",
        })
        return msg

    def heartbeat(self, ws: Any, interval_ms: int) -> None:
        interval = interval_ms / 1000.0
        while self.running and not self.should_stop():
            time.sleep(interval)
            try:
                ws.send(json.dumps({"op": 1, "d": self.seq}))
                if self.verbose:
                    emit(None, {"event": "HEARTBEAT_SENT", "seq": self.seq})
            except Exception:
                return

    def should_reply(self, data: dict[str, Any]) -> tuple[bool, str]:
        msg_id = str(data.get("id") or "")
        if not msg_id or msg_id in self.replied_to:
            return False, "already_replied"
        if str(data.get("guild_id") or "") != self.guild_id:
            return False, "wrong_guild"
        if str(data.get("channel_id") or "") != self.channel_id:
            return False, "wrong_channel"
        author = data.get("author") or {}
        if str(author.get("id") or "") == self.me_id:
            return False, "self"
        ref = data.get("message_reference") or {}
        if str(ref.get("message_id") or "") in self.watch_ids:
            return True, "reply_to_watched_message"
        ref_msg = data.get("referenced_message") or {}
        if str(ref_msg.get("id") or "") in self.watch_ids:
            return True, "referenced_message"
        mentions = data.get("mentions") or []
        if any(str(u.get("id") or "") == self.me_id for u in mentions):
            return True, "mention"
        content_l = (data.get("content") or "").lower()
        me_name = (self.me.get("global_name") or self.me.get("username") or "").lower()
        if "@mara" in content_l or (me_name and f"@{me_name}" in content_l):
            return True, "text_mention"
        return False, "not_addressed_to_me"

    def recent_messages(self, limit: int = 8) -> list[dict[str, Any]]:
        try:
            msgs = rest("GET", f"/channels/{self.channel_id}/messages?limit={limit}") or []
            msgs.reverse()
            out = []
            for m in msgs:
                a = m.get("author") or {}
                out.append({
                    "author": a.get("global_name") or a.get("username") or a.get("id"),
                    "author_id": a.get("id"),
                    "content": m.get("content") or "",
                })
            return out
        except Exception:
            return []

    def generate_agent_reply(self, data: dict[str, Any], reason: str) -> str:
        author = data.get("author") or {}
        name = author.get("global_name") or author.get("username") or "there"
        content = (data.get("content") or "").strip()
        # Clean raw Discord mention tokens before feeding context.
        content_clean = re.sub(r"<@!?\d+>", "@mara", content).strip()
        history = self.recent_messages()
        hist_lines = "\n".join(f"{m['author']}: {re.sub(r'<@!?\\d+>', '@mara', m['content'])}" for m in history if m.get("content"))
        prompt = (
            "You are Mara replying in a Discord channel as yourself. "
            "Be concise, playful, direct, and actually answer the latest message. "
            "Do not say 'gotcha' or ask 'say more' unless genuinely needed. "
            "One short message, no markdown essay.\n\n"
            f"Channel recent context:\n{hist_lines}\n\n"
            f"Latest trigger reason: {reason}\n"
            f"Latest message from {name}: {content_clean}\n\n"
            "Write the Discord reply now."
        )
        key = api_server_key()
        if not key:
            return self.reply_template.format(author=name, content=content, reason=reason)
        payload = {
            "model": "hermes-agent",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.7,
            "max_tokens": 220,
        }
        req = urllib.request.Request(
            api_server_url(),
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                body = json.loads(resp.read().decode("utf-8", "replace"))
            text = (((body.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
            return text[:1800] if text else self.reply_template.format(author=name, content=content, reason=reason)
        except Exception as e:
            emit("BLOCKED", {"error": "agent_reply_failed", "message": str(e)})
            return ""

    def format_reply(self, data: dict[str, Any], reason: str) -> str:
        if self.agent_reply:
            return self.generate_agent_reply(data, reason)
        author = data.get("author") or {}
        name = author.get("global_name") or author.get("username") or "you"
        content = (data.get("content") or "").strip()
        return self.reply_template.format(author=name, content=content, reason=reason)

    def on_message(self, data: dict[str, Any]) -> None:
        ok, reason = self.should_reply(data)
        if not ok:
            return
        incoming_id = str(data.get("id"))
        author = data.get("author") or {}
        incoming_content = data.get("content") or ""
        ack_id = None
        if self.agent_reply and likely_needs_research(incoming_content):
            ack = send(self.channel_id, research_ack_text(), reply_to=incoming_id)
            ack_id = str(ack.get("id"))
            self.watch_ids.add(ack_id)
            self.persist()
            emit("ACKED", {
                "reason": "research_needed",
                "incoming_id": incoming_id,
                "ack_id": ack_id,
                "ack_content": ack.get("content"),
                "jump_url": f"https://discord.com/channels/{self.guild_id}/{self.channel_id}/{ack_id}",
            })
        reply_content = self.format_reply(data, reason)
        if not reply_content:
            self.replied_to.add(incoming_id)
            self.persist()
            emit("NO_REPLY", {
                "reason": "empty_agent_reply",
                "incoming_id": incoming_id,
                "ack_id": ack_id,
            })
            return
        out = send(self.channel_id, reply_content, reply_to=incoming_id)
        out_id = str(out.get("id"))
        self.watch_ids.add(out_id)
        self.replied_to.add(incoming_id)
        self.persist()
        emit("AUTO_REPLIED", {
            "reason": reason,
            "incoming_id": incoming_id,
            "incoming_author_id": author.get("id"),
            "incoming_author": author.get("global_name") or author.get("username"),
            "incoming_content": incoming_content,
            "ack_id": ack_id,
            "reply_id": out_id,
            "reply_content": out.get("content"),
            "jump_url": f"https://discord.com/channels/{self.guild_id}/{self.channel_id}/{out_id}",
        })

    def gateway_once(self) -> None:
        tok = get_token()
        with connect(GATEWAY, additional_headers={"User-Agent": "Mozilla/5.0"}) as ws:
            hello = json.loads(ws.recv())
            if hello.get("op") != 10:
                emit("BLOCKED", {"error": "expected_hello", "payload": hello})
                return
            interval = int((hello.get("d") or {}).get("heartbeat_interval") or 41250)
            ws.send(json.dumps(identify_payload(tok)))
            threading.Thread(target=self.heartbeat, args=(ws, interval), daemon=True).start()
            while self.running and not self.should_stop():
                try:
                    raw = ws.recv(timeout=5)
                except TimeoutError:
                    continue
                payload = json.loads(raw)
                if payload.get("s") is not None:
                    self.seq = payload.get("s")
                op = payload.get("op")
                if op == 0:
                    event = payload.get("t")
                    data = payload.get("d") or {}
                    if event == "READY":
                        u = data.get("user") or {}
                        emit(None, {"event": "READY", "user": {"id": u.get("id"), "username": u.get("username"), "global_name": u.get("global_name")}, "state": str(self.state_path)})
                    elif event == "MESSAGE_CREATE":
                        self.on_message(data)
                    elif self.verbose:
                        emit(None, {"event": event})
                elif op == 7:
                    emit("BLOCKED", {"event": "RECONNECT_REQUESTED"})
                    return
                elif op == 9:
                    emit("BLOCKED", {"event": "INVALID_SESSION", "resumable": payload.get("d")})
                    return
                elif self.verbose:
                    emit(None, {"op": op, "t": payload.get("t")})

    def run(self, starter: str | None) -> int:
        emit(None, {
            "event": "START",
            "guild_id": self.guild_id,
            "channel_id": self.channel_id,
            "channel_name": self.channel_name,
            "me": {"id": self.me.get("id"), "username": self.me.get("username"), "global_name": self.me.get("global_name")},
            "agent_reply": self.agent_reply,
        })
        write_pid(self.pid_path)
        self.persist()
        if starter:
            self.post_starter(starter)
        delay = 1.0
        try:
            while self.running and not self.should_stop():
                try:
                    self.gateway_once()
                    delay = 1.0
                except KeyboardInterrupt:
                    self.running = False
                    break
                except Exception as e:
                    emit("BLOCKED", {"error": type(e).__name__, "message": str(e), "reconnect_in": delay})
                    time.sleep(delay)
                    delay = min(delay * 2, 30)
        finally:
            if self.pid_path.exists():
                try:
                    if int(self.pid_path.read_text().strip() or "0") == os.getpid():
                        self.pid_path.unlink(missing_ok=True)
                except Exception:
                    pass
        emit(None, {"event": "DONE", "state": str(self.state_path)})
        return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Discord watcher: post/watch Discord and optionally generate real replies via Hermes API Server.")
    p.add_argument("--duration", type=float, default=0.0, help="Watcher duration in seconds; <=0 means forever until --stop")
    p.add_argument("--reply-template", default="gotcha, {author} — say more?", help="Reply template fields: {author}, {content}, {reason}")
    p.add_argument("--replace", dest="replace", action="store_true", default=True, help="Replace prior watcher for same guild/channel (default)")
    p.add_argument("--no-replace", dest="replace", action="store_false")
    p.add_argument("--resolve-only", action="store_true", help="Resolve target and exit without posting/watching")
    p.add_argument("--stop", action="store_true", help="Stop the existing watcher for this resolved guild/channel and exit")
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--agent-reply", action="store_true", help="Generate real replies through the local Hermes API server instead of static template")
    p.add_argument("--watch-only", action="store_true", help="Start/replace watcher without posting a starter message")
    p.add_argument("target_id", metavar="channel_id", help="Discord channel ID; guild/server ID also accepted and auto-resolved")
    p.add_argument("message", metavar="prompt", nargs="*", help="Prompt/message words to post")
    args = p.parse_args()

    target_id = resolve_channel_alias(args.target_id)
    resolved = resolve_target(target_id)
    base = state_base(str(resolved["guild_id"]), str(resolved["channel_id"]))
    pid_path = base.with_suffix(".pid")
    if args.stop:
        stop_existing(pid_path, missing_ok=False)
        return 0

    if args.resolve_only:
        emit(None, {"ok": True, "resolved": resolved})
        return 0

    starter = " ".join(args.message).strip()
    if args.watch_only:
        starter = None
    elif not starter:
        emit("BLOCKED", {"error": "missing_message", "usage": "discord.py [channel_id] [prompt] or --watch-only"})
        return 1

    if args.replace:
        replace_existing(pid_path)

    return Watcher(resolved, args.duration, args.reply_template, args.verbose, args.agent_reply).run(starter)


if __name__ == "__main__":
    raise SystemExit(main())
