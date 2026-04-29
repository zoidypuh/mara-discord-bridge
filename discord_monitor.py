#!/usr/bin/env python3
"""Discord channel monitor — polls for new messages and queues replies."""
import json
import time
from datetime import datetime, timezone

import requests

# Channels to watch (channel_id: display_name)
WATCH_CHANNELS = {
    "730692714642800650": "FomoBros™ #general",
    "1312091998781440064": "degen-trading #degen-trading",
}

MY_USER_ID = "903312683477106688"  # hunter_thompson_ / Goblin Council
MY_USERNAME = "hunter_thompson_"
BRIDGE_BASE = "http://127.0.0.1:8787"
POLL_INTERVAL = 5  # seconds


def poll_messages():
    """Poll all watched channels for new messages."""
    seen_ids = set()

    # Load previously seen IDs from file
    try:
        with open("/tmp/discord_seen_ids.json", "r") as f:
            seen_ids = set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    print("🚀 Monitor starting...", flush=True)
    print(f"🔍 Watching channels:")
    for cid, name in WATCH_CHANNELS.items():
        print(f"   [{cid}] {name}")
    print(
        f"🎯 Alerting on: mentions of @{MY_USERNAME}, replies to me, all new messages queued"
    )
    print("Press Ctrl+C to stop.\n", flush=True)

    while True:
        for channel_id in WATCH_CHANNELS:
            try:
                resp = requests.get(
                    f"{BRIDGE_BASE}/messages/{channel_id}?limit=5", timeout=10
                )
                data = resp.json()
                messages = data.get("messages", [])

                if not messages:
                    continue

                for msg in messages:
                    msg_id = msg.get("id", "")
                    if msg_id in seen_ids:
                        continue

                    # Mark as seen
                    seen_ids.add(msg_id)
                    author_id = msg.get("author", {}).get("id", "")
                    content = msg.get("clean_content", "").lower()
                    raw_content = msg.get("content", "")

                    # Check for mention or reply to Mara
                    is_mention = (
                        f"<@{MY_USER_ID}>" in raw_content
                        or f"@{MY_USERNAME}" in content
                    )
                    is_reply_to_mara = (
                        msg.get("referenced_message")
                        and msg["referenced_message"].get("author", {})
                        .get("id")
                        == MY_USER_ID
                    )

                    # Format alert for mentions/replies only
                    author_name = msg.get("author", {}).get(
                        "display_name", "?"
                    )
                    channel_name = WATCH_CHANNELS.get(channel_id, channel_id)

                    if is_mention or is_reply_to_mara:
                        alert = f"[{channel_name}] {author_name}: {msg.get('clean_content', '(empty)')}"
                        if is_mention and is_reply_to_mara:
                            alert += "\n🔔 Mention + Reply!"
                        elif is_reply_to_mara:
                            alert += "\n↩️  Reply to you"
                        else:
                            alert += "\n@ Mentioned"

                        print(alert, flush=True)

                        # Write to log
                        with open("/tmp/discord_monitor.log", "a") as f:
                            f.write(
                                f"{datetime.now(timezone.utc).isoformat()} | {alert}\n"
                            )

                    # Queue ALL new messages in watched channels for potential reply
                    queue_entry = {
                        "channel_id": channel_id,
                        "message_id": msg_id,
                        "author_id": author_id,
                        "author_name": author_name,
                        "content": msg.get("clean_content", ""),
                        "is_reply_to_mara": is_reply_to_mara or is_mention,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                    with open("/tmp/discord_reply_queue.jsonl", "a") as f:
                        f.write(json.dumps(queue_entry) + "\n")
                    print(f"  → Queued message {msg_id[:12]}...", flush=True)

            except Exception as e:
                print(f"⚠️  Error polling {channel_id}: {e}", flush=True)

        # Save seen IDs
        with open("/tmp/discord_seen_ids.json", "w") as f:
            json.dump(list(seen_ids), f)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    poll_messages()
