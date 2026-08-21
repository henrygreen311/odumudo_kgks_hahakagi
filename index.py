import logging
import os
import signal
import sys
from supabase import create_client

from module.balance import update_balance          # re-enabled
from module.offer import fetch_and_log_offers
from module.account_manager import AccountManager

logging.basicConfig(level=logging.INFO, format="%(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


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


def main():
    supabase = create_supabase_client()

    # Acquire an account (you can specify account_id or let it pick any)
    manager = AccountManager(supabase, account_id=1)  # or None for any
    if not manager.acquire():
        logging.error("Failed to acquire account, exiting.")
        sys.exit(1)

    manager.start_heartbeat()

    # Signal handler for graceful exit
    def signal_handler(sig, frame):
        logging.info("Interrupt received, cleaning up...")
        manager.release()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        account_id = manager.account_id

        # 1. Update balance
        logging.info(f"Updating balance for account {account_id}...")
        update_balance(supabase, account_id=account_id)

        # 2. Fetch offers and process them
        logging.info(f"Fetching offers for account {account_id}...")
        fetch_and_log_offers(supabase, account_id=account_id)

        logging.info("Job finished successfully.")

    except Exception as e:
        logging.error(f"Job failed: {e}")
    finally:
        # Always release the account
        manager.release()


if __name__ == "__main__":
    main()