# Mara Discord Bridge & Monitor

Local Discord **user-token** bridge for Mara. Logs messages via HTTP API, polls channels with a background monitor, and auto-replies to `>` prefixed triggers.

## Architecture

```
┌─────────────┐     ┌──────────────┐     ┌───────────────┐
│  Discord    │◄───►│ Bridge API   │◄───►│ Mara (Hermes) │
│  User Token │     │ :8787        │     │ Cron + Queue  │
└─────────────┘     └──────────────┘     └───────────────┘
                              ▲
                              │ polls every 5s
                       ┌──────────────┐
                       │ Monitor      │
                       │ (tmux loop)  │
                       └──────────────┘
```

## Files

| File | Purpose |
|------|---------|
| `discord_mara_bridge.py` | HTTP API server — send/receive messages, DMs, reactions |
| `discord_monitor.py` | Background poller — watches channels, queues new messages |
| `discord_skill_watcher.py` | Gateway watcher for `/discord ... monitor and answer` style live replies |
| `now_playing_youtube_tray_to_discord.py` | Posts Windows YouTube Tray currently playing item to Discord without video embeds |
| `check_mara_triggers.py` | Extracts `>` prefixed messages from queue for auto-reply |

## Install

```bash
cd ~/.hermes/scripts/discord
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Setup

Export your Discord **user token** (not bot token):

```bash
export DISCORD_USER_TOKEN='your-user-token-here'
```

Or use the `.env` file:

```bash
cp .env.example .env
# edit .env and put in DISCORD_USER_TOKEN
```

## Run

### 1. Start the bridge server

```bash
source .venv/bin/activate
python discord_mara_bridge.py
```

Server runs on `http://127.0.0.1:8787`.

### 2. Start the monitor (tmux, auto-restart)

```bash
tmux new-session -d -s discord-monitor \
  'while true; do .venv/bin/python discord_monitor.py; sleep 3; done'
```

Edit `WATCH_CHANNELS` in `discord_monitor.py` to add/remove channels.

### 3. Auto-reply cron (Hermes)

Messages starting with `>` are auto-detected and replied to by Mara every minute via a Hermes cron job.


## Realtime Agent Reply Watcher

For live Mara-style conversation, use `discord_skill_watcher.py` instead of the old static-template reply mode. It can monitor a channel without posting a starter message and generate real replies through the local Hermes API Server.

Friendly channel aliases are loaded from `~/.hermes/discord-channel-aliases.json`; copy `discord-channel-aliases.example.json` there and edit it. Example: `fomobros` → `730692714642800650`.

```bash
source .venv/bin/activate
export DISCORD_USER_TOKEN="$DISCORD_MARA_TOKEN"
python discord_skill_watcher.py --duration 0 --replace --watch-only --agent-reply 730692714642800650
```

Important flags:

| Flag | Purpose |
|------|---------|
| `--watch-only` | Start/replace watcher without posting a junk starter message |
| `--agent-reply` | Generate a real reply via Hermes API Server instead of repeating a static template |
| `--reply-template` | Fallback/static template when `--agent-reply` is not used or API generation fails |

The watcher replies to Discord replies on watched messages, real mentions, and plain-text `@mara` mentions. This fixes the old `gotcha, {author} — say more?` loop by using Hermes for actual context-aware replies.

Hermes API Server config is read from environment or `~/.hermes/.env`:

```env
API_SERVER_HOST=127.0.0.1
API_SERVER_PORT=8642
API_SERVER_KEY=...
```

## Post Windows YouTube Tray “Now Playing”

Post the currently playing `com.gismar.youtube-tray` media session to Discord:

```bash
source .venv/bin/activate
export DISCORD_USER_TOKEN="$DISCORD_MARA_TOKEN"
python now_playing_youtube_tray_to_discord.py
```

Default channel is `730692714642800650` (`fomobros`). Override with a positional channel ID:

```bash
python now_playing_youtube_tray_to_discord.py 730692714642800650
```

Message format:

```text
🎵 **<title>**
🔗 <youtube-url>
```

The helper sends Discord message `flags: 4` (`SUPPRESS_EMBEDS`) by default, so the YouTube URL stays a plain link without a video preview embed. Use `--allow-embeds` only when a preview is wanted. It also uses a per-channel lock and `--dedupe-window` (default 45s) to avoid accidental rapid duplicate posts.

## API Endpoints

Base URL: `http://127.0.0.1:8787`

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Health check |
| `/send` | POST | Send message to channel |
| `/dm` | POST | DM a user |
| `/react` | POST | React to a message |
| `/messages/{id}` | GET | Get recent messages from channel |
| `/events` | GET | Recent event log |

### Send a message

```bash
curl -s -X POST http://127.0.0.1:8787/send \
  -H 'Content-Type: application/json' \
  -d '{"channel_id":"1234567890","content":"hello from Mara"}'
```

### Reply to a message

```bash
curl -s -X POST http://127.0.0.1:8787/send \
  -H 'Content-Type: application/json' \
  -d '{"channel_id":"1234567890","reply_to_message_id":"9876543210","content":"reply text"}'
```

### DM a user

```bash
curl -s -X POST http://127.0.0.1:8787/dm \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"1111111111","content":"DM from Mara"}'
```

## Queue Files

| File | Purpose |
|------|---------|
| `/tmp/discord_reply_queue.jsonl` | All new messages (JSONL) |
| `/tmp/discord_seen_ids.json` | Tracked message IDs across restarts |
| `/tmp/discord_processed_ids.json` | Already-replied `>` triggers |

## Auto-Reply Convention

Type `> your message` in a watched channel → Mara auto-replies within 60 seconds.
