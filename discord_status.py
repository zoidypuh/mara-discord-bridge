#!/usr/bin/env python3
"""Get/set Discord user custom status via the user settings endpoint.

Designed for Gis's user account: prefers DISCORD_GISMAR_TOKEN by default.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

API = "https://discord.com/api/v10"
DEFAULT_TOKEN_ENVS = ("DISCORD_GISMAR_TOKEN", "DISCORD_USER_TOKEN", "DISCORD_MARA_TOKEN", "DISCORD")
VALID_STATUSES = {"online", "idle", "dnd", "invisible"}


def token_from_env(token_env: str | None = None) -> tuple[str, str]:
    names = (token_env,) if token_env else DEFAULT_TOKEN_ENVS
    for name in names:
        if name and os.environ.get(name):
            return os.environ[name], name
    raise SystemExit(f"Missing Discord token env; checked: {', '.join(n for n in names if n)}")


def request_json(method: str, path: str, token: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        API + path,
        data=data,
        method=method,
        headers={
            "Authorization": token,
            "User-Agent": "Mozilla/5.0",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:1000]
        raise RuntimeError(f"Discord {method} {path} failed HTTP {e.code}: {body}") from e


def status_view(settings: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": settings.get("status"),
        "custom_status": settings.get("custom_status"),
    }


def build_text(text: str | None, link: str | None) -> str | None:
    if text is None:
        return None
    text = text.strip()
    if link:
        text = f"{text}\n{link.strip()}"
    return text


def main() -> int:
    ap = argparse.ArgumentParser(description="Get/set Discord user custom status via /users/@me/settings")
    ap.add_argument("text", nargs="?", help="custom status text; use --link to append a newline + URL")
    ap.add_argument("--link", help="append URL on a new line after text")
    ap.add_argument("--emoji", help="unicode emoji or custom emoji name for the separate Discord status emoji slot")
    ap.add_argument("--status", choices=sorted(VALID_STATUSES), help="set base user status too: online/idle/dnd/invisible")
    ap.add_argument("--expires-at", help="ISO8601 expiration timestamp; omit for never expire")
    ap.add_argument("--clear", action="store_true", help="clear custom status")
    ap.add_argument("--get", action="store_true", help="read current status only")
    ap.add_argument("--dry-run", action="store_true", help="print intended payload but do not PATCH")
    ap.add_argument("--token-env", help="explicit token env name; default prefers DISCORD_GISMAR_TOKEN")
    args = ap.parse_args()

    token, token_env_used = token_from_env(args.token_env)
    before = request_json("GET", "/users/@me/settings", token)

    if args.get and not (args.text or args.link or args.status or args.clear):
        print(json.dumps({"ok": True, "token_env_used": token_env_used, **status_view(before)}, ensure_ascii=False))
        return 0

    if args.clear:
        custom_status = None
    else:
        text = build_text(args.text, args.link)
        if text is None and not args.status:
            raise SystemExit("Provide text, --status, --clear, or --get")
        custom_status = None if text is None else {
            "text": text,
            "emoji_name": args.emoji,
            "emoji_id": None,
            "expires_at": args.expires_at,
        }

    payload: dict[str, Any] = {}
    if args.status:
        payload["status"] = args.status
    if args.clear or custom_status is not None:
        payload["custom_status"] = custom_status

    if args.dry_run:
        print(json.dumps({
            "ok": True,
            "dry_run": True,
            "token_env_used": token_env_used,
            "before": status_view(before),
            "payload": payload,
        }, ensure_ascii=False, indent=2))
        return 0

    after = request_json("PATCH", "/users/@me/settings", token, payload)
    print(json.dumps({
        "ok": True,
        "token_env_used": token_env_used,
        "before": status_view(before),
        "after": status_view(after),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
