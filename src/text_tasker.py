import logging
import re
import requests
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Any, Optional

from src.task_utils import (
    get_account_credentials,
    get_affiliate_url,
    follow_affiliate_link,
    get_tune_flow,
)

logger = logging.getLogger(__name__)

FINAL_POST_DELAY = 2.5 * 60  # 2.5 minutes


def get_existing_challenge(supabase, offer_id: str) -> Optional[str]:
    """Query failed_text_offers for a non-null challenge; return it if found."""
    try:
        resp = (
            supabase.table("failed_text_offers")
            .select("challenge")
            .eq("task_id", offer_id)
            .execute()
        )
        if resp.data and resp.data[0].get("challenge") is not None:
            return resp.data[0]["challenge"]
    except Exception as e:
        logger.error(f"Error fetching existing challenge for {offer_id}: {e}")
    return None


def delete_failed_offer(supabase, offer_id: str) -> None:
    """Delete row from failed_text_offers if it exists."""
    try:
        supabase.table("failed_text_offers").delete().eq("task_id", offer_id).execute()
        logger.info(f"Deleted offer {offer_id} from failed_text_offers.")
    except Exception as e:
        logger.error(f"Error deleting offer {offer_id}: {e}")


def extract_challenge(instructions: str) -> Optional[str]:
    """Extract search phrase from instructions and convert to hashtag."""
    clean = instructions.replace('*', '')
    patterns = [
        r'(?:Search|Type\s+in|Type)\s+["“]([^"”]+)["”]',
        r'(?:Search|Type\s+in|Type)\s+[“"]([^"”]+)[”"]',
    ]
    for pattern in patterns:
        match = re.search(pattern, clean, re.IGNORECASE)
        if match:
            query = match.group(1).strip()
            cleaned = re.sub(r'[^a-zA-Z0-9]', '', query)
            return f"#{cleaned.lower()}"
    return None


def extract_image_url(instructions: str) -> Optional[str]:
    """Extract image URL from instructions like [see example](url) or [looks like this](url)."""
    patterns = [
        r'\[(?:see example|looks like this)\]\((https?://[^)]+)\)',
        r'\[see example\]\((https?://[^)]+)\)',
        r'\[looks like this\]\((https?://[^)]+)\)',
    ]
    for pattern in patterns:
        match = re.search(pattern, instructions, re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def store_failed_offer(supabase, offer_id: str, instructions: str) -> None:
    """
    Store failed offer details in Supabase table 'failed_text_offers'.
    Only store if no row exists or challenge is null.
    """
    table = "failed_text_offers"
    # Check if row exists with a non-null challenge
    try:
        resp = (
            supabase.table(table)
            .select("task_id, challenge")
            .eq("task_id", offer_id)
            .execute()
        )
        existing = resp.data
        if existing and existing[0].get("challenge") is not None:
            logger.info(f"Offer {offer_id} already has a challenge in DB, skipping storage.")
            return
    except Exception as e:
        logger.error(f"Error checking existing row for {offer_id}: {e}")

    image_url = extract_image_url(instructions)
    if not image_url:
        logger.warning(f"Could not extract image URL from instructions for {offer_id}")

    data = {
        "task_id": offer_id,
        "instruction": instructions,
        "image_url": image_url,
        # challenge remains null
    }

    try:
        if existing:
            supabase.table(table).update(data).eq("task_id", offer_id).execute()
            logger.info(f"Updated failed offer {offer_id} in DB.")
        else:
            supabase.table(table).insert(data).execute()
            logger.info(f"Stored failed offer {offer_id} in DB.")
    except Exception as e:
        logger.error(f"Failed to store offer {offer_id}: {e}")


def post_tune_flow_with_challenge(
    offer_id: str,
    user_id: str,
    challenge: str,
    auth: str,
    user_agent: str,
) -> tuple:
    """POST with challenge, returns (status_code, data, is_invalid)."""
    url = f"https://api.jumptask.io/marketplace/tune/{offer_id}/flows"
    headers = {
        "User-Agent": user_agent,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate, br",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {auth}",
        "Origin": "https://app.jumptask.io",
        "Referer": "https://app.jumptask.io/",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
        "Sec-Gpc": "1",
        "Te": "trailers",
    }
    payload = {"user_id": user_id, "challenge": challenge}
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=15)
        status = resp.status_code
        data = None
        try:
            data = resp.json()
        except ValueError:
            data = {"raw": resp.text}
        if status == 400 and isinstance(data, dict) and "invalid_challenge" in data.get("title", "").lower():
            return status, data, True
        return status, data, False
    except Exception as e:
        logger.error(f"POST request failed: {e}")
        return None, None, False


def process_offer_with_delay(
    supabase,
    account_id: int,
    offer_id: str,
    instructions: str,
    idx: int,
    total: int
) -> bool:
    """Process a single text offer with delay, using DB challenge if available."""
    logger.info(f"ID={offer_id} Processing {idx}/{total}")

    # First, check if we already have a challenge in DB
    challenge_from_db = get_existing_challenge(supabase, offer_id)
    if challenge_from_db:
        challenge = challenge_from_db
        used_db_challenge = True
        logger.info(f"Using existing challenge from DB: {challenge}")
    else:
        used_db_challenge = False
        # No existing challenge – extract from instructions
        if "hashtag" not in instructions.lower() or "description" not in instructions.lower():
            logger.info("Skipping: missing 'hashtag' and 'description'")
            return False

        challenge = extract_challenge(instructions)
        if not challenge:
            logger.info("Skipping: could not extract challenge")
            return False

        logger.info(f"Extracted challenge: {challenge}")

    # Get credentials
    creds = get_account_credentials(supabase, account_id)
    user_agent = creds["user_agent"]
    uid = creds["uid"]
    auth = creds["auth"]

    max_retries = 3
    for attempt in range(1, max_retries + 1):
        affiliate_url = get_affiliate_url(supabase, account_id, offer_id)
        if not affiliate_url:
            continue

        flow_params = follow_affiliate_link(affiliate_url, user_agent)
        if not flow_params:
            continue

        if not get_tune_flow(
            offer_id,
            flow_params["transaction_id"],
            flow_params["user_id"],
            flow_params["destination"],
            flow_params["advertiser"],
            user_agent,
        ):
            continue

        logger.info(f"Waiting {FINAL_POST_DELAY/60:.1f} minutes before final POST for {offer_id}...")
        time.sleep(FINAL_POST_DELAY)

        status, data, invalid = post_tune_flow_with_challenge(
            offer_id, uid, challenge, auth, user_agent
        )

        if invalid:
            logger.info(f"Status: {status}")
            logger.info(f"Invalid challenge: {data}")
            # If we had no existing challenge, store the offer details
            if not used_db_challenge:
                store_failed_offer(supabase, offer_id, instructions)
            else:
                # We already had a challenge from DB; don't overwrite, just log
                logger.warning(f"Offer {offer_id} had a DB challenge but it's still invalid.")
            return False  # discard

        if status == 200 and data and data.get("offer_id") == offer_id:
            logger.info(f"Status: {status}")
            logger.info(f"Successful: {data}")
            # If we used a DB challenge, delete the row now
            if used_db_challenge:
                delete_failed_offer(supabase, offer_id)
            return True

        if status is not None:
            logger.info(f"Attempt {attempt} failed with status {status}")

    logger.info(f"Failed after {max_retries} attempts")
    return False


def process_text_offers(supabase, account_id: int, text_tasks: List[Dict[str, str]]) -> None:
    """Process all text offers concurrently with 5 workers."""
    if not text_tasks:
        logger.info("No text offers to process.")
        return

    total = len(text_tasks)
    logger.info(f"Processing {total} text tasks with 5 concurrent workers...")

    tasks = [
        (task['offer_id'], task['instructions'], idx, total)
        for idx, task in enumerate(text_tasks, 1)
    ]

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {}
        for offer_id, instructions, idx, total in tasks:
            future = executor.submit(
                process_offer_with_delay,
                supabase,
                account_id,
                offer_id,
                instructions,
                idx,
                total
            )
            futures[future] = offer_id

        for future in as_completed(futures):
            offer_id = futures[future]
            try:
                success = future.result()
                if not success:
                    logger.error(f"Offer {offer_id} ultimately failed.")
            except Exception as e:
                logger.error(f"Unexpected error processing offer {offer_id}: {e}")