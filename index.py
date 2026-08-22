import logging
import os
import sys
import signal
from supabase import create_client

# Disabled modules
# from module.balance import update_balance
# from src.link_tasker import process_link_offers
# from src.topic_tasker import process_topic_offers

# Enable hashtag_tasker
from src.hashtag_tasker import process_text_offers

from module.offer import get_account, fetch_offers, classify_offers
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


def parse_account_id():
    for arg in sys.argv[1:]:
        if arg.startswith("id="):
            try:
                return int(arg.split("=", 1)[1])
            except ValueError:
                logging.error(f"Invalid account id format: {arg}, expected id=NUMBER")
                sys.exit(1)
    return 1


def main():
    account_id = parse_account_id()
    supabase = create_supabase_client()

    manager = AccountManager(supabase, account_id=account_id)
    if not manager.acquire():
        logging.error("Failed to acquire an account, exiting.")
        sys.exit(1)

    manager.start_heartbeat()

    def signal_handler(sig, frame):
        logging.info("Interrupt received, cleaning up...")
        manager.release()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        acquired_id = manager.account_id
        logging.info(f"Using account ID: {acquired_id}")

        account = get_account(supabase, acquired_id)
        offers = fetch_offers(account)
        if not offers:
            logging.info("No offers fetched. Exiting.")
            return

        _, text_tasks, _ = classify_offers(offers)
        logging.info(f"Text tasks found: {len(text_tasks)}")

        if text_tasks:
            process_text_offers(supabase, account, text_tasks)
        else:
            logging.info("No text tasks to process.")

        logging.info("Job finished successfully.")

    except Exception as e:
        logging.error(f"Job failed: {e}")
    finally:
        manager.release()


if __name__ == "__main__":
    main()