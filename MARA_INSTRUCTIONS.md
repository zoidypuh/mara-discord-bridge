# Mara Discord Bridge Contract

Use this local bridge when the user asks you to watch or interact with Discord.

Base URL:

```text
http://127.0.0.1:8787
```

If `MARA_DISCORD_BRIDGE_KEY` is configured, include:

```text
Authorization: Bearer <key>
```

Core actions:

- `GET /health`: check readiness.
- `GET /guilds`: list guilds the bot can see.
- `GET /channels`: list text channels the bot can see.
- `GET /messages/{channel_id}?limit=25`: read recent channel messages.
- `GET /events?limit=50`: read recent captured events.
- `WS /ws`: live feed of new messages, edits, deletes, replies, and reactions.
- `POST /send`: send a channel message or reply.
- `POST /dm`: send a direct message.
- `POST /typing`: show typing in a channel.
- `POST /react`: add a reaction.

Send JSON:

```json
{"channel_id":"123", "content":"message text"}
```

Reply JSON:

```json
{"channel_id":"123", "reply_to_message_id":"456", "content":"reply text"}
```

DM JSON:

```json
{"user_id":"123", "content":"message text"}
```

Incoming message events include `channel_id`, `author.id`, `content`, `clean_content`, `is_reply`, `reference`, and `jump_url`. Use the allowlists in the bridge environment to limit which channels or users are watched.
