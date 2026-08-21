import time
import threading
import logging
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)


class AccountManager:
    def __init__(self, supabase, account_id=None, heartbeat_interval=10, timeout=60):
        self.supabase = supabase
        self.account_id = account_id
        self.heartbeat_interval = heartbeat_interval
        self.timeout = timeout
        self.active = False
        self.heartbeat_thread = None
        self._stop_heartbeat = threading.Event()

    def acquire(self) -> bool:
        """
        Acquire an account.
        - If account_id is given, try to lock that specific account.
        - Otherwise, find any available account (active=False or stale heartbeat).
        """
        if self.account_id is not None:
            return self._acquire_specific()
        else:
            return self._acquire_any()

    def _acquire_specific(self) -> bool:
        """Lock a specific account if it's available."""
        resp = self.supabase.table("jumptask") \
            .select("active, last_heartbeat") \
            .eq("id", self.account_id) \
            .execute()
        if not resp.data:
            logger.error(f"Account id {self.account_id} not found")
            return False
        row = resp.data[0]
        if self._is_account_available(row):
            return self._claim_account(self.account_id)
        else:
            logger.error(f"Account {self.account_id} is in use (active or recent heartbeat)")
            return False

    def _acquire_any(self) -> bool:
        """Find and lock any available account; retry until one is found."""
        while True:
            resp = self.supabase.table("jumptask") \
                .select("id, active, last_heartbeat") \
                .execute()
            available = None
            for row in resp.data:
                if self._is_account_available(row):
                    available = row["id"]
                    break
            if available is not None:
                return self._claim_account(available)
            else:
                logger.info("No available account, waiting 5 seconds...")
                time.sleep(5)

    def _is_account_available(self, row: dict) -> bool:
        """Check if account is not active or heartbeat is stale."""
        active = row.get("active", False)
        if not active:
            return True
        last = row.get("last_heartbeat")
        if last is None:
            return False  # active but no heartbeat? treat as in use
        # Parse ISO timestamp (may include 'Z' or timezone)
        last_time = datetime.fromisoformat(last.replace('Z', '+00:00'))
        # If heartbeat older than timeout, it's stale and available
        return datetime.now(last_time.tzinfo) - last_time >= timedelta(seconds=self.timeout)

    def _claim_account(self, account_id: int) -> bool:
        """Set active=True and update last_heartbeat."""
        now = datetime.utcnow().isoformat() + 'Z'
        try:
            self.supabase.table("jumptask") \
                .update({"active": True, "last_heartbeat": now}) \
                .eq("id", account_id) \
                .execute()
            self.account_id = account_id
            self.active = True
            logger.info(f"Account {account_id} acquired.")
            return True
        except Exception as e:
            logger.error(f"Failed to claim account {account_id}: {e}")
            return False

    def release(self):
        """Release the account: set active=False and clear heartbeat."""
        if self.account_id and self.active:
            try:
                self.supabase.table("jumptask") \
                    .update({"active": False, "last_heartbeat": None}) \
                    .eq("id", self.account_id) \
                    .execute()
                self.active = False
                logger.info(f"Account {self.account_id} released.")
            except Exception as e:
                logger.error(f"Failed to release account {self.account_id}: {e}")
        self.stop_heartbeat()

    def start_heartbeat(self):
        """Start a background thread that updates last_heartbeat every interval."""
        if self.heartbeat_thread and self.heartbeat_thread.is_alive():
            return
        self._stop_heartbeat.clear()
        self.heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self.heartbeat_thread.start()
        logger.info(f"Heartbeat started for account {self.account_id}")

    def stop_heartbeat(self):
        """Stop the heartbeat thread."""
        if self.heartbeat_thread and self.heartbeat_thread.is_alive():
            self._stop_heartbeat.set()
            self.heartbeat_thread.join(timeout=2)
            logger.info("Heartbeat stopped.")

    def _heartbeat_loop(self):
        while not self._stop_heartbeat.is_set():
            time.sleep(self.heartbeat_interval)
            if self.account_id and self.active:
                try:
                    now = datetime.utcnow().isoformat() + 'Z'
                    self.supabase.table("jumptask") \
                        .update({"last_heartbeat": now}) \
                        .eq("id", self.account_id) \
                        .execute()
                except Exception as e:
                    logger.error(f"Heartbeat update failed: {e}")
