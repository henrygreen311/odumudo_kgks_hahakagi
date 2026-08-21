import logging
import requests
from typing import Dict, Any, Optional
from urllib.parse import urlparse, parse_qs
from src.proxy_client import proxy_request

logger = logging.getLogger(__name__)

def get_account(supabase, account_id: int) -> Dict[str, Any]:
    result = (
        supabase.table("jumptask")
        .select("auth, user_agent, uid, proxy_url, cookie")
        .eq("id", account_id)
        .single()
        .execute()
    )
    account = result.data
    if not account:
        raise RuntimeError(f"Account id={account_id} not found")
    return account

def get_affiliate_url(account, offer_id: str) -> Optional[str]:
    endpoint = f"offerwall/offers/jumpoffers/url/{offer_id}"
    response = proxy_request(account, endpoint)
    if response.status_code != 200:
        logger.error(f"Failed to get affiliate URL: {response.text}")
        return None
    data = response.json()
    return data.get("url")

def follow_affiliate_link(affiliate_url: str, user_agent: str) -> Optional[Dict[str, str]]:
    # Direct request to go2cloud.org – no proxy
    headers = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate, br",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "cross-site",
        "Sec-Fetch-User": "?1",
        "Sec-Gpc": "1",
        "Priority": "u=0, i",
        "Te": "trailers",
        "Connection": "keep-alive",
    }
    try:
        resp = requests.get(affiliate_url, headers=headers, timeout=15, allow_redirects=False)
        if resp.status_code != 302:
            logger.error(f"Affiliate link did not return 302, got {resp.status_code}")
            return None
        location = resp.headers.get("Location")
        if not location:
            logger.error("No Location header in 302 response")
            return None
        parsed = urlparse(location)
        query = parse_qs(parsed.query)
        transaction_id = query.get("transaction_id", [None])[0]
        destination = query.get("destination", [None])[0]
        advertiser = query.get("advertiser", [None])[0]
        user_id = query.get("user_id", [None])[0]
        if not all([transaction_id, destination, advertiser, user_id]):
            logger.error(f"Missing required parameters in Location: {location}")
            return None
        return {
            "transaction_id": transaction_id,
            "destination": destination,
            "advertiser": advertiser,
            "user_id": user_id,
        }
    except Exception as e:
        logger.error(f"Failed to follow affiliate link: {e}")
        return None

def get_tune_flow(account, offer_id: str, transaction_id: str, user_id: str, destination: str, advertiser: str) -> bool:
    endpoint = f"marketplace/tune/{offer_id}/flows"
    params = {
        "offer_id": offer_id,
        "transaction_id": transaction_id,
        "user_id": user_id,
        "destination": destination,
        "advertiser": advertiser,
    }
    response = proxy_request(account, endpoint, params=params)
    # Accept 200 (proxy may force it) or 302 (original behavior)
    if response.status_code in (200, 302):
        return True
    else:
        logger.error(f"Tune flow GET returned {response.status_code}, expected 200 or 302")
        return False