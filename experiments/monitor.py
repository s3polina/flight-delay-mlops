import argparse
import base64
import csv
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

API = "http://localhost:15672/api"
AUTH = "Basic " + base64.b64encode(b"guest:guest").decode()


def get(path: str) -> dict | list:
    request = urllib.request.Request(f"{API}{path}", headers={"Authorization": AUTH})
    with urllib.request.urlopen(request, timeout=2) as response:
        return json.load(response)


def snapshot(queue: str) -> dict:
    q = get(f"/queues/%2F/{urllib.parse.quote(queue, safe='')}")
    node = get("/nodes")[0]
    stats = q.get("message_stats", {})
    return {
        "ts": round(time.time(), 3),
        "messages": q.get("messages", 0),
        "ready": q.get("messages_ready", 0),
        "unacked": q.get("messages_unacknowledged", 0),
        "consumers": q.get("consumers", 0),
        "publish_rate": stats.get("publish_details", {}).get("rate", 0.0),
        "ack_rate": stats.get("ack_details", {}).get("rate", 0.0),
        "rabbit_mem_mb": round(node.get("mem_used", 0) / 2**20, 1),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", required=True)
    parser.add_argument("--queue", default="flight.tasks")
    parser.add_argument("--duration", type=float, default=180)
    parser.add_argument("--out", default="experiments/results")
    args = parser.parse_args()

    path = Path(args.out) / args.label / "rabbit.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()
    with path.open("w", newline="") as f:
        writer = None
        while time.time() - started < args.duration:
            try:
                row = snapshot(args.queue)
            except Exception as exc:
                print(f"monitor: {exc}")
                time.sleep(1)
                continue
            if writer is None:
                writer = csv.DictWriter(f, fieldnames=list(row))
                writer.writeheader()
            writer.writerow(row)
            f.flush()
            time.sleep(1)
    print(f"saved {path}")


if __name__ == "__main__":
    main()
