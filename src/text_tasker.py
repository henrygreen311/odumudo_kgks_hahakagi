import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Any, Optional

from src.task_utils import follow_affiliate_link, get_tune_flow, get_affiliate_url
from src.proxy_client import proxy_request

logger = logging.getLogger(__name__)

FINAL_POST_DELAY = 2.5 * 60  # 2.5 minutes

def get_existing_challenge(supabase, offer_id: str) -> Optional[str]:
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
    try:
        supabase.table("failed_text_offers").delete().eq("task_id", offer_id).execute()
        logger.info(f"Deleted offer {offer_id} from DB.")
    except Exception as e:
        logger.error(f"Error deleting offer {offer_id}: {e}")

def extract_challenge(instructions: str) -> Optional[str]:
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
    table = "failed_text_offers"
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

    data = {"task_id": offer_id, "instruction": instructions, "image_url": image_url}
    try:
        if existing:
            supabase.table(table).update(data).eq("task_id", offer_id).execute()
            logger.info(f"Updated failed offer {offer_id} in DB.")
        else:
            supabase.table(table).insert(data).execute()
            logger.info(f"Stored failed offer {offer_id} in DB.")
    except Exception as e:
        logger.error(f"Failed to store offer {offer_id}: {e}")

def post_tune_flow_with_challenge(account, offer_id: str, user_id: str, challenge: str) -> tuple:
    endpoint = f"marketplace/tune/{offer_id}/flows"
    payload = {"user_id": user_id, "challenge": challenge}
    response = proxy_request(account, endpoint, method='POST', json_data=payload)
    status = response.status_code
    data = None
    try:
        data = response.json()
    except ValueError:
        data = {"raw": response.text}
    if status == 400 and isinstance(data, dict) and "invalid_challenge" in data.get("title", "").lower():
        return status, data, True
    return status, data, False

def process_offer_with_delay(supabase, account, offer_id: str, instructions: str, idx: int, total: int) -> bool:
    logger.info(f"ID={offer_id} Processing {idx}/{total}")

    # Check DB for existing challenge
    challenge_from_db = get_existing_challenge(supabase, offer_id)
    used_db_challenge = False
    if challenge_from_db:
        challenge = challenge_from_db
        used_db_challenge = True
        logger.info(f"Using existing challenge from DB: {challenge}")
    else:
        if "hashtag" not in instructions.lower() or "description" not in instructions.lower():
            logger.info("Skipping: missing 'hashtag' and 'description'")
            return False
        challenge = extract_challenge(instructions)
        if not challenge:
            logger.info("Skipping: could not extract challenge")
            return False
        logger.info(f"Extracted challenge: {challenge}")

    user_agent = account["user_agent"]
    uid = account["uid"]

    max_retries = 3
    for attempt in range(1, max_retries + 1):
        affiliate_url = get_affiliate_url(account, offer_id)
        if not affiliate_url:
            continue
        flow_params = follow_affiliate_link(affiliate_url, user_agent)
        if not flow_params:
            continue
        if not get_tune_flow(account, offer_id, flow_params["transaction_id"], flow_params["user_id"], flow_params["destination"], flow_params["advertiser"]):
            continue

        logger.info(f"Waiting {FINAL_POST_DELAY/60:.1f} minutes before final POST for {offer_id}...")
        time.sleep(FINAL_POST_DELAY)

        status, data, invalid = post_tune_flow_with_challenge(account, offer_id, uid, challenge)
        if invalid:
            logger.info(f"Status: {status}")
            logger.info(f"Invalid challenge: {data}")
            if not used_db_challenge:
                store_failed_offer(supabase, offer_id, instructions)
            else:
                logger.warning(f"Offer {offer_id} had a DB challenge but it's still invalid.")
            return False

        if status == 200 and data and data.get("offer_id") == offer_id:
            logger.info(f"Status: {status}")
            logger.info(f"Successful: {data}")
            if used_db_challenge:
                delete_failed_offer(supabase, offer_id)
            return True

        if status is not None:
            logger.info(f"Attempt {attempt} failed with status {status}")

    logger.info(f"Failed after {max_retries} attempts")
    return False

def process_text_offers(supabase, account, text_tasks: List[Dict[str, str]]) -> None:
    if not text_tasks:
        logger.info("No text offers to process.")
        return

    total = len(text_tasks)
    logger.info(f"Processing {total} text tasks with 5 concurrent workers...")
    tasks = [(task['offer_id'], task['instructions'], idx, total) for idx, task in enumerate(text_tasks, 1)]
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {}
        for offer_id, instructions, idx, total in tasks:
            future = executor.submit(process_offer_with_delay, supabase, account, offer_id, instructions, idx, total)
            futures[future] = offer_id
        for future in as_completed(futures):
            offer_id = futures[future]
            try:
                success = future.result()
                if not success:
                    logger.error(f"Offer {offer_id} ultimately failed.")
            except Exception as e:
                logger.error(f"Unexpected error processing offer {offer_id}: {e}")