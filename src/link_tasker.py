# src/link_tasker.py
import logging
from typing import List, Dict, Any, Optional
from src.task_utils import (
    get_account_credentials,
    get_affiliate_url,
    follow_affiliate_link,
    get_tune_flow,
)

logger = logging.getLogger(__name__)

def post_tune_flow(offer_id: str, user_id: str, auth: str, user_agent: str) -> Optional[Dict[str, Any]]:
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
    payload = {"user_id": user_id}
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=15)
        if resp.status_code != 200:
            logger.error(f"POST tune flow returned {resp.status_code}")
            return None
        data = resp.json()
        if data.get("offer_id") == offer_id and data.get("user_id") == user_id:
            return data
        else:
            logger.error(f"Unexpected response: {data}")
            return None
    except Exception as e:
        logger.error(f"POST tune flow failed: {e}")
        return None

def process_single_offer(supabase, account_id: int, offer_id: str) -> bool:
    max_retries = 3
    for attempt in range(1, max_retries + 1):
        logger.info(f"Attempt {attempt} for offer {offer_id}")
        affiliate_url = get_affiliate_url(supabase, account_id, offer_id)
        if not affiliate_url:
            continue
        creds = get_account_credentials(supabase, account_id)
        user_agent = creds["user_agent"]
        uid = creds["uid"]
        auth = creds["auth"]

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

        result = post_tune_flow(offer_id, uid, auth, user_agent)
        if result:
            logger.info(f"Offer {offer_id} completed: {result}")
            return True
    logger.error(f"Offer {offer_id} failed after {max_retries} attempts")
    return False

def process_link_offers(supabase, account_id: int, offer_ids: List[str]) -> None:
    if not offer_ids:
        logger.info("No link offers to process.")
        return
    logger.info(f"Link offers to process: {len(offer_ids)}")
    logger.info(f"Offer IDs: {', '.join(offer_ids)}")
    for idx, offer_id in enumerate(offer_ids, 1):
        logger.info(f"Processing offer {idx}/{len(offer_ids)}: {offer_id}")
        success = process_single_offer(supabase, account_id, offer_id)
        if success:
            logger.info(f"Offer {offer_id} completed successfully.")
        else:
            logger.error(f"Offer {offer_id} failed.")