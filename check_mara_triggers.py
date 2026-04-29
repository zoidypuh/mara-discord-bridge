#!/usr/bin/env python3
"""Check Discord queue for Mara triggers (> prefix) and return them."""
import json
import sys

QUEUE_FILE = "/tmp/discord_reply_queue.jsonl"
PROCESSED_FILE = "/tmp/discord_processed_ids.json"

def main():
    # Load processed IDs
    processed = set()
    try:
        with open(PROCESSED_FILE) as f:
            processed = set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    # Read queue
    triggers = []
    all_messages = []
    try:
        with open(QUEUE_FILE) as f:
            for line in f:
                msg = json.loads(line.strip())
                all_messages.append(msg)
                if msg["message_id"] not in processed and msg["content"].startswith(">"):
                    triggers.append(msg)
    except FileNotFoundError:
        print("No queue file yet")
        return

    # Output triggers as JSON array
    if triggers:
        # Mark them as processed
        for t in triggers:
            processed.add(t["message_id"])
        with open(PROCESSED_FILE, "w") as f:
            json.dump(list(processed), f)

        print(json.dumps(triggers))
    else:
        print("[]")

if __name__ == "__main__":
    main()
