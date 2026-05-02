#!/usr/bin/env python3
"""Post the currently playing YouTube Tray media session to a Discord channel.

WSL/Windows-host helper for Gis/Mara:
- reads Windows GSMTC media sessions via PowerShell
- picks com.gismar.youtube-tray, preferring PlaybackStatus=Playing
- resolves a YouTube URL with yt-dlp ytsearch1
- posts a compact emoji message through Discord REST
"""
from __future__ import annotations

import argparse
import base64
import fcntl
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any

POWERSHELL = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
DEFAULT_APP_ID = "com.gismar.youtube-tray"
DEFAULT_CHANNEL_ID = "730692714642800650"  # Gis Discord #general / fomobros
DISCORD_SUPPRESS_EMBEDS_FLAG = 1 << 2

PS_MEDIA_QUERY = r'''
$ErrorActionPreference='Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager, Windows.Media.Control, ContentType=WindowsRuntime]
$null = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionMediaProperties, Windows.Media.Control, ContentType=WindowsRuntime]
function AwaitOperation($Op, [Type]$ResultType) {
  $method = [System.WindowsRuntimeSystemExtensions].GetMethods() |
    Where-Object { $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' } |
    Select-Object -First 1
  $task = $method.MakeGenericMethod($ResultType).Invoke($null, @($Op))
  $task.Wait() | Out-Null
  return $task.Result
}
$mgr = AwaitOperation ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager]::RequestAsync()) ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager])
$out = @()
foreach ($s in $mgr.GetSessions()) {
  $props = AwaitOperation ($s.TryGetMediaPropertiesAsync()) ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionMediaProperties])
  $out += [pscustomobject]@{
    SourceAppUserModelId = $s.SourceAppUserModelId
    PlaybackStatus = $s.GetPlaybackInfo().PlaybackStatus.ToString()
    Title = $props.Title
    Artist = $props.Artist
    AlbumTitle = $props.AlbumTitle
  }
}
$out | ConvertTo-Json -Depth 4
'''


def run_powershell(script: str) -> str:
    enc = base64.b64encode(script.encode("utf-16le")).decode()
    proc = subprocess.run(
        [POWERSHELL, "-NoProfile", "-EncodedCommand", enc],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"PowerShell failed ({proc.returncode}): {proc.stderr.strip()}")
    return proc.stdout.strip()


def media_sessions() -> list[dict[str, Any]]:
    raw = run_powershell(PS_MEDIA_QUERY)
    if not raw:
        return []
    data = json.loads(raw)
    if isinstance(data, dict):
        return [data]
    return data


def pick_session(sessions: list[dict[str, Any]], app_id: str) -> dict[str, Any]:
    matches = [s for s in sessions if s.get("SourceAppUserModelId") == app_id]
    if not matches:
        raise SystemExit(f"No media session found for {app_id}. Sessions: {json.dumps(sessions, ensure_ascii=False)}")
    playing = [s for s in matches if s.get("PlaybackStatus") == "Playing"]
    return (playing or matches)[0]


def resolve_youtube_url(title: str, artist: str = "") -> str:
    query = " ".join(x for x in [title, artist] if x).strip()
    if not query:
        raise SystemExit("Cannot search YouTube: empty title/artist")
    proc = subprocess.run(
        ["yt-dlp", "--quiet", "--no-warnings", "--dump-single-json", f"ytsearch1:{query}"],
        capture_output=True,
        text=True,
        timeout=45,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"yt-dlp search failed ({proc.returncode}): {proc.stderr.strip()}")
    info = json.loads(proc.stdout)
    entries = info.get("entries") or []
    item = entries[0] if entries else info
    url = item.get("webpage_url") or item.get("original_url")
    if not url and item.get("id"):
        url = f"https://www.youtube.com/watch?v={item['id']}"
    if not url:
        raise SystemExit(f"yt-dlp returned no URL for query: {query}")
    return url


def discord_token() -> str:
    token = os.environ.get("DISCORD_USER_TOKEN") or os.environ.get("DISCORD_MARA_TOKEN") or os.environ.get("DISCORD")
    if not token:
        raise SystemExit("Missing Discord token env: DISCORD_USER_TOKEN, DISCORD_MARA_TOKEN, or DISCORD")
    return token


def post_discord(channel_id: str, content: str, *, suppress_embeds: bool = True) -> dict[str, Any]:
    payload: dict[str, Any] = {"content": content}
    if suppress_embeds:
        payload["flags"] = DISCORD_SUPPRESS_EMBEDS_FLAG
    req = urllib.request.Request(
        f"https://discord.com/api/v10/channels/{channel_id}/messages",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": discord_token(),
            "User-Agent": "Mozilla/5.0",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Discord POST failed HTTP {e.code}: {body}") from e


def discord_recent_messages(channel_id: str, limit: int = 10) -> list[dict[str, Any]]:
    req = urllib.request.Request(
        f"https://discord.com/api/v10/channels/{channel_id}/messages?limit={limit}",
        headers={"Authorization": discord_token(), "User-Agent": "Mozilla/5.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.load(resp)
            return data if isinstance(data, list) else []
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Discord recent-message check failed HTTP {e.code}: {body}") from e


def snowflake_epoch_seconds(snowflake: str) -> float:
    return (((int(snowflake) >> 22) + 1420070400000) / 1000.0)


def find_recent_duplicate(channel_id: str, content: str, window_seconds: int) -> dict[str, Any] | None:
    if window_seconds <= 0:
        return None
    cutoff = time.time() - window_seconds
    for msg in discord_recent_messages(channel_id, limit=10):
        if msg.get("content") != content:
            continue
        try:
            if snowflake_epoch_seconds(str(msg.get("id", "0"))) < cutoff:
                continue
        except Exception:
            continue
        return msg
    return None


def channel_lock(channel_id: str):
    state_dir = os.path.expanduser("~/.hermes/state")
    os.makedirs(state_dir, exist_ok=True)
    return open(os.path.join(state_dir, f"discord_post_song_{channel_id}.lock"), "w")


def resolve_channel_alias(channel: str) -> str:
    """Map friendly aliases like 'fomobros' to Discord channel IDs when configured."""
    raw = (channel or "").strip()
    if raw.isdigit():
        return raw
    path = os.path.expanduser("~/.hermes/discord-channel-aliases.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return raw
    aliases = data.get("aliases") or {}
    return str(aliases.get(raw) or aliases.get(raw.lower()) or raw)


def main() -> int:
    ap = argparse.ArgumentParser(description="Post YouTube Tray now-playing to Discord")
    ap.add_argument("channel_id", nargs="?", default=os.environ.get("DISCORD_POST_SONG_CHANNEL", DEFAULT_CHANNEL_ID), help=f"Discord channel ID (default: {DEFAULT_CHANNEL_ID}, or DISCORD_POST_SONG_CHANNEL)")
    ap.add_argument("--app-id", default=DEFAULT_APP_ID, help=f"media SourceAppUserModelId (default: {DEFAULT_APP_ID})")
    ap.add_argument("--url", help="override/resolved YouTube URL")
    ap.add_argument("--allow-embeds", action="store_true", help="allow Discord to render URL embeds/previews; default suppresses video embeds")
    ap.add_argument("--dry-run", action="store_true", help="print payload but do not post")
    ap.add_argument("--dedupe-window", type=int, default=int(os.environ.get("DISCORD_POST_SONG_DEDUPE_WINDOW", "45")), help="skip if the identical message already exists in this channel within N seconds (default: 45; 0 disables)")
    args = ap.parse_args()
    args.channel_id = resolve_channel_alias(args.channel_id)

    session = pick_session(media_sessions(), args.app_id)
    title = (session.get("Title") or "").strip()
    artist = (session.get("Artist") or "").strip()
    if not title:
        raise SystemExit(f"Selected media session has no title: {json.dumps(session, ensure_ascii=False)}")
    url = args.url or resolve_youtube_url(title, artist)
    content = f"🎵 **{title}**\n🔗 {url}"

    if args.dry_run:
        print(json.dumps({
            "channel_id": args.channel_id,
            "session": session,
            "content": content,
            "flags": 0 if args.allow_embeds else DISCORD_SUPPRESS_EMBEDS_FLAG,
            "suppress_embeds": not args.allow_embeds,
        }, ensure_ascii=False, indent=2))
        return 0

    with channel_lock(args.channel_id) as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        dup = find_recent_duplicate(args.channel_id, content, args.dedupe_window)
        if dup:
            print(json.dumps({
                "id": dup.get("id"),
                "author": (dup.get("author") or {}).get("global_name") or (dup.get("author") or {}).get("username"),
                "content": dup.get("content"),
                "skipped_duplicate": True,
            }, ensure_ascii=False))
            return 0
        msg = post_discord(args.channel_id, content, suppress_embeds=not args.allow_embeds)
    print(json.dumps({"id": msg.get("id"), "author": (msg.get("author") or {}).get("global_name") or (msg.get("author") or {}).get("username"), "content": msg.get("content"), "flags": msg.get("flags")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
