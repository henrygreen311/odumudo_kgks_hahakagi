import logging
import os

from supabase import create_client
from module.offer import fetch_and_log_offers

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
    return create_client(
        config["SUPABASE_URL"],
        config["SUPABASE_KEY"],
    )


def main():
    print("Fetching offers...")
    supabase = create_supabase_client()
    fetch_and_log_offers(supabase, account_id=1)


if __name__ == "__main__":
    main()