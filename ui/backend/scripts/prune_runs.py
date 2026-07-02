"""Prune agent runs and steps from SQLite chat database.

Usage:
    # Online mode (calls HTTP API):
    python -m scripts.prune_runs --days 30 --url http://localhost:8000 --token mytoken
    
    # Offline/Direct mode (writes directly to SQLite DB):
    python -m scripts.prune_runs --days 30 --direct
"""

import argparse
import sys
import os
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

def prune_online(url: str, token: str, days: int) -> int:
    import urllib.request
    import urllib.parse
    import json
    
    # Normalize URL
    url = url.rstrip('/')
    prune_url = f"{url}/api/agent-runs/prune?days={days}"
    
    req = urllib.request.Request(prune_url, method="POST")
    req.add_header("X-Prune-Token", token)
    
    try:
        with urllib.request.urlopen(req) as response:
            res_body = json.loads(response.read().decode())
            deleted = res_body.get("deleted_runs", 0)
            print(f"Successfully pruned {deleted} runs via API endpoint.")
            return 0
    except Exception as e:
        print(f"Error calling prune endpoint {prune_url}: {e}", file=sys.stderr)
        return 1

def prune_direct(days: int) -> int:
    import db
    db.init_db()
    deleted = db.prune_agent_runs(retention_days=days)
    print(f"Successfully pruned {deleted} runs directly from database at {db.DB_PATH}.")
    return 0

def main() -> int:
    parser = argparse.ArgumentParser(description="Prune agent runs from database")
    parser.add_argument("--days", type=int, default=30, help="Delete runs older than this many days (default: 30)")
    parser.add_argument("--direct", action="store_true", help="Write directly to DB instead of calling API")
    parser.add_argument("--url", default=os.environ.get("BACKEND_URL"), help="Backend API URL (for online mode)")
    parser.add_argument("--token", default=os.environ.get("PRUNE_TOKEN"), help="API auth token (for online mode)")
    args = parser.parse_args()
    
    if args.direct:
        return prune_direct(args.days)
        
    url = args.url or "http://localhost:8000"
    token = args.token
    
    if not token:
        print("PRUNE_TOKEN is required for online mode. Pass --token or set PRUNE_TOKEN env var.", file=sys.stderr)
        print("To write directly to the database file instead, use --direct.", file=sys.stderr)
        return 2
        
    return prune_online(url, token, args.days)

if __name__ == "__main__":
    raise SystemExit(main())
