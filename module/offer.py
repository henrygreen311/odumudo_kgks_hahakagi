import logging
import requests
from typing import List, Dict, Any, Optional, Tuple

logger = logging.getLogger(__name__)

def get_account_credentials(supabase, account_id: int = 1) -> Dict[str, str]:
    result = (
        supabase.table("jumptask")
        .select("auth, user_agent, uid")
        .eq("id", account_id)
        .single()
        .execute()
    )
    account = result.data
    if not account:
        raise RuntimeError(f"Account id={account_id} not found")
    return account


def fetch_offers(
    supabase,
    account_id: int = 1,
    tag: str = "Watch & Profit",
    order: str = "jmpt_amount",
    direction: str = "desc",
) -> Optional[List[Dict[str, Any]]]:
    creds = get_account_credentials(supabase, account_id)
    headers = {
        "User-Agent": creds["user_agent"],
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate, br",
        "Authorization": f"Bearer {creds['auth']}",
        "Origin": "https://app.jumptask.io",
        "Referer": "https://app.jumptask.io/",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
        "Sec-Gpc": "1",
        "Te": "trailers",
    }
    base_url = "https://api.jumptask.io/offerwall/offers"
    params = {
        "direction": direction,
        "order": order,
        "search": "",
        "tags[]": tag,
    }

    try:
        resp = requests.get(base_url, headers=headers, params=params, timeout=15)
        # Log status and basic info for debugging
        logger.info(f"Offers API status: {resp.status_code}")
        if resp.status_code != 200:
            logger.error(f"Unexpected status: {resp.status_code}, body: {resp.text[:500]}")
            return None

        # Ensure JSON content
        if 'application/json' not in resp.headers.get('Content-Type', ''):
            logger.error(f"Non-JSON response: {resp.text[:200]}")
            return None

        data = resp.json()
    except requests.exceptions.JSONDecodeError as e:
        logger.error(f"Invalid JSON: {e}")
        logger.error(f"Status: {resp.status_code}, Body preview: {resp.text[:500]}")
        return None
    except requests.RequestException as e:
        logger.error(f"Request failed: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        return None

    offers = data.get("data", {}).get("offers")
    if not isinstance(offers, list):
        logger.warning("Response does not contain an offers list")
        return None
    return offers


def classify_offers(offers: List[Dict[str, Any]]) -> Tuple[List[str], List[Dict[str, str]]]:
    click_ids = []
    text_tasks = []
    for offer in offers:
        offer_id = offer.get("offer_id")
        if not offer_id:
            continue
        instr = offer.get("instructions", "")
        instr_lower = instr.lower()
        if "click on the last link" in instr_lower or ("click" in instr_lower and "link" in instr_lower):
            click_ids.append(offer_id)
        if "enter" in instr_lower and "text" in instr_lower and "field below" in instr_lower:
            text_tasks.append({"offer_id": offer_id, "instructions": instr})
    return click_ids, text_tasks


def fetch_and_log_offers(supabase, account_id: int = 1) -> Optional[List[Dict[str, Any]]]:
    offers = fetch_offers(supabase, account_id)
    if offers is None:
        return None
    count = len(offers)
    logger.info(f"Total offers fetched: {count}")

    click_ids, text_tasks = classify_offers(offers)
    logger.info(f"Click link task: {len(click_ids)}")
    logger.info(f"Enter text task: {len(text_tasks)}")

    if click_ids:
        logger.info(f"Processing {len(click_ids)} link tasks...")
        from src.link_tasker import process_link_offers
        process_link_offers(supabase, account_id, click_ids)

    if text_tasks:
        logger.info(f"Processing {len(text_tasks)} text tasks...")
        from src.text_tasker import process_text_offers
        process_text_offers(supabase, account_id, text_tasks)

    return offers