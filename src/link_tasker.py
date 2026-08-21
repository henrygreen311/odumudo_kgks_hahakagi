import logging
from typing import List, Dict, Any, Optional
from src.task_utils import follow_affiliate_link, get_tune_flow, get_affiliate_url
from src.proxy_client import proxy_request

logger = logging.getLogger(__name__)

def post_tune_flow(account, offer_id: str, user_id: str) -> Optional[Dict[str, Any]]:
    endpoint = f"marketplace/tune/{offer_id}/flows"
    payload = {"user_id": user_id}
    response = proxy_request(account, endpoint, method='POST', json_data=payload)
    if response.status_code != 200:
        logger.error(f"POST tune flow returned {response.status_code}")
        return None
    data = response.json()
    if data.get("offer_id") == offer_id and data.get("user_id") == user_id:
        return data
    else:
        logger.error(f"Unexpected response: {data}")
        return None

def process_single_offer(account, offer_id: str) -> bool:
    max_retries = 3
    user_agent = account["user_agent"]
    uid = account["uid"]
    for attempt in range(1, max_retries + 1):
        logger.info(f"Attempt {attempt} for offer {offer_id}")
        affiliate_url = get_affiliate_url(account, offer_id)
        if not affiliate_url:
            continue
        flow_params = follow_affiliate_link(affiliate_url, user_agent)
        if not flow_params:
            continue
        if not get_tune_flow(account, offer_id, flow_params["transaction_id"], flow_params["user_id"], flow_params["destination"], flow_params["advertiser"]):
            continue
        result = post_tune_flow(account, offer_id, uid)
        if result:
            logger.info(f"Offer {offer_id} completed: {result}")
            return True
    logger.error(f"Offer {offer_id} failed after {max_retries} attempts")
    return False

def process_link_offers(account, offer_ids: List[str]) -> None:
    if not offer_ids:
        logger.info("No link offers to process.")
        return
    logger.info(f"Link offers to process: {len(offer_ids)}")
    logger.info(f"Offer IDs: {', '.join(offer_ids)}")
    for idx, offer_id in enumerate(offer_ids, 1):
        logger.info(f"Processing offer {idx}/{len(offer_ids)}: {offer_id}")
        success = process_single_offer(account, offer_id)
        if success:
            logger.info(f"Offer {offer_id} completed successfully.")
        else:
            logger.error(f"Offer {offer_id} failed.")