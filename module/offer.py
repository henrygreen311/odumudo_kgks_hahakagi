# module/offer.py
import logging
from typing import List, Dict, Any, Optional, Tuple
from src.proxy_client import proxy_request

logger = logging.getLogger(__name__)


def get_account(supabase, account_id: int = 1) -> Dict[str, Any]:
    """Fetch all required account credentials from the jumptask table."""
    result = (
        supabase.table("jumptask")
        .select("id, auth, user_agent, uid, proxy_url, cookie, youtube_api_key")  # <-- added youtube_api_key
        .eq("id", account_id)
        .single()
        .execute()
    )
    account = result.data
    if not account:
        raise RuntimeError(f"Account id={account_id} not found")
    return account


def fetch_offers(account, tag: str = "Watch & Profit", order: str = "jmpt_amount", direction: str = "desc") -> Optional[List[Dict[str, Any]]]:
    params = {
        "direction": direction,
        "order": order,
        "search": "",
        "tags[]": tag,
    }
    response = proxy_request(account, "offerwall/offers", params=params)
    if response.status_code != 200:
        logger.error(f"Failed to fetch offers: {response.status_code}")
        return None
    data = response.json()
    offers = data.get("data", {}).get("offers")
    if not isinstance(offers, list):
        logger.warning("Response does not contain an offers list")
        return None
    return offers


def classify_offers(offers: List[Dict[str, Any]]) -> Tuple[List[str], List[Dict[str, str]], List[Dict[str, str]]]:
    click_ids = []
    text_tasks = []
    topic_tasks = []
    for offer in offers:
        offer_id = offer.get("offer_id")
        if not offer_id:
            continue
        instr = offer.get("instructions", "")
        instr_lower = instr.lower()
        if "click on the last link" in instr_lower or ("click" in instr_lower and "link" in instr_lower):
            click_ids.append(offer_id)
            continue
        if "topic name" in instr_lower and "starts at" in instr_lower:
            topic_tasks.append({"offer_id": offer_id, "instructions": instr})
            continue
        if "enter" in instr_lower and "text" in instr_lower and "field below" in instr_lower:
            text_tasks.append({"offer_id": offer_id, "instructions": instr})
    return click_ids, text_tasks, topic_tasks


def fetch_and_log_offers(supabase, account_id: int = 1) -> Optional[List[Dict[str, Any]]]:
    account = get_account(supabase, account_id)
    offers = fetch_offers(account)
    if offers is None:
        return None
    count = len(offers)
    logger.info(f"Total offers fetched: {count}")

    click_ids, text_tasks, topic_tasks = classify_offers(offers)
    logger.info(f"Click link task: {len(click_ids)}")
    logger.info(f"Enter text task: {len(text_tasks)}")
    logger.info(f"Topic task: {len(topic_tasks)}")

    if click_ids:
        from src.link_tasker import process_link_offers
        process_link_offers(account, click_ids)

    if text_tasks:
        from src.text_tasker import process_text_offers
        process_text_offers(supabase, account, text_tasks)

    if topic_tasks:
        from src.topic_tasker import process_topic_offers
        process_topic_offers(supabase, account, topic_tasks)

    return offers


if __name__ == "__main__":
    from index import create_supabase_client
    import logging
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    supabase = create_supabase_client()
    fetch_and_log_offers(supabase, account_id=1)