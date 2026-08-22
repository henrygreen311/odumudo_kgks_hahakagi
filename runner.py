#!/usr/bin/env python3
import subprocess
import time
import sys
import os
import signal
from supabase import create_client

# Reuse the config loading from index.py
def load_db_config():
    here = os.path.dirname(os.path.abspath(__file__))
    for path in (
        os.path.join(here, "db.txt"),
        os.path.join(os.path.dirname(here), "db.txt"),
    ):
        if os.path.exists(path):
            config = {}
            with open(path) as file:
                for line in file:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        key, _, value = line.partition("=")
                        config[key.strip()] = value.strip().strip('"')
            return config
    raise FileNotFoundError("db.txt not found")

def create_supabase_client():
    config = load_db_config()
    return create_client(config["SUPABASE_URL"], config["SUPABASE_KEY"])

def get_available_accounts(supabase):
    """Fetch all account IDs that are not currently active (or have stale heartbeat)."""
    # We'll use the same logic as AccountManager: active=false or heartbeat older than 60s
    # For simplicity, we'll just fetch all ids where active=false or last_heartbeat is null or older than 60 seconds.
    # But we also need to consider that the runner might be the one setting active=true, so we
    # should only pick accounts that are not locked.
    # For safety, we'll select all rows with active=false or (active=true and last_heartbeat < now()-interval '60 seconds')
    # We'll use supabase to query.
    from datetime import datetime, timedelta
    cutoff = datetime.utcnow() - timedelta(seconds=60)
    cutoff_str = cutoff.isoformat() + 'Z'
    
    resp = supabase.table("jumptask") \
        .select("id") \
        .or_(f"active.eq.false,last_heartbeat.lt.{cutoff_str}") \
        .order("id") \
        .execute()
    
    return [row["id"] for row in resp.data]

def main():
    supabase = create_supabase_client()
    account_ids = get_available_accounts(supabase)
    
    if not account_ids:
        print("No available accounts found. Exiting.")
        sys.exit(0)
    
    print(f"Found {len(account_ids)} available accounts: {account_ids}")
    
    processes = []
    
    # Start first instance with logging
    first_id = account_ids[0]
    print(f"Starting first instance with account ID {first_id} (logging enabled)")
    p1 = subprocess.Popen(
        [sys.executable, "index.py", f"id={first_id}"],
        stdout=None,
        stderr=None
    )
    processes.append(p1)
    
    # Start remaining instances with 30s delay each, silenced
    for idx, account_id in enumerate(account_ids[1:], start=1):
        print(f"Waiting 30 seconds before starting account ID {account_id} (silenced)...")
        time.sleep(30)
        
        with open(os.devnull, 'w') as devnull:
            p = subprocess.Popen(
                [sys.executable, "index.py", f"id={account_id}"],
                stdout=devnull,
                stderr=devnull
            )
            processes.append(p)
            print(f"Started account {account_id} (PID: {p.pid})")
    
    # Signal handler for graceful shutdown
    def signal_handler(sig, frame):
        print("\nReceived interrupt, terminating all instances...")
        for p in processes:
            p.terminate()
        for p in processes:
            p.wait()
        print("All instances terminated.")
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Wait for all processes to finish
    try:
        for p in processes:
            p.wait()
        print("All instances finished.")
    except KeyboardInterrupt:
        signal_handler(None, None)

if __name__ == "__main__":
    main()
