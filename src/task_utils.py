# src/task_utils.py
import logging
import requests
from typing import Dict, Any, Optional
from urllib.parse import urlparse, parse_qs

logger = logging.getLogger(__name__)

def get_account_credentials(supabase, account_id: int) -> Dict[str, str]:
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

def build_headers(user_agent: str, auth: Optional[str] = None) -> Dict[str, str]:
    headers = {
        "User-Agent": user_agent,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate, br",
        "Origin": "https://app.jumptask.io",
        "Referer": "https://app.jumptask.io/",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
        "Sec-Gpc": "1",
        "Te": "trailers",
    }
    if auth:
        headers["Authorization"] = f"Bearer {auth}"
    return headers

def get_affiliate_url(supabase, account_id: int, offer_id: str) -> Optional[str]:
    creds = get_account_credentials(supabase, account_id)
    headers = build_headers(creds["user_agent"], creds["auth"])
    url = f"https://api.jumptask.io/offerwall/offers/jumpoffers/url/{offer_id}"
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        affiliate_url = data.get("url")
        if not affiliate_url:
            logger.error(f"No 'url' field in response for offer {offer_id}")
            return None
        return affiliate_url
    except Exception as e:
        logger.error(f"Failed to get affiliate URL for offer {offer_id}: {e}")
        return None

def follow_affiliate_link(affiliate_url: str, user_agent: str) -> Optional[Dict[str, str]]:
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

def get_tune_flow(
    offer_id: str,
    transaction_id: str,
    user_id: str,
    destination: str,
    advertiser: str,
    user_agent: str,
) -> bool:
    url = f"https://api.jumptask.io/marketplace/tune/{offer_id}/flows"
    params = {
        "offer_id": offer_id,
        "transaction_id": transaction_id,
        "user_id": user_id,
        "destination": destination,
        "advertiser": advertiser,
    }
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
    }
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=15, allow_redirects=False)
        if resp.status_code != 302:
            logger.error(f"Tune flow GET returned {resp.status_code}, expected 302")
            return False
        return True
    except Exception as e:
        logger.error(f"Tune flow GET failed: {e}")
        return False