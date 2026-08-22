# src/task_utils.py
import logging
import requests
from typing import Dict, Any, Optional
from urllib.parse import urlparse, parse_qs
from src.proxy_client import proxy_request

logger = logging.getLogger(__name__)

def get_account(supabase, account_id: int) -> Dict[str, Any]:
    result = (
        supabase.table("jumptask")
        .select("auth, user_agent, uid, proxy_url, cookie, youtube_api_key")
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
    """Follow the affiliate link and extract transaction_id and other params."""
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
        status = resp.status_code

        if status == 302:
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

            # Require at least transaction_id and user_id
            if not transaction_id or not user_id:
                logger.error(f"Missing transaction_id or user_id in Location: {location}")
                return None

            # Set defaults for missing optional fields
            if not destination:
                destination = "https://youtube.com"
                logger.warning(f"destination missing, using default: {destination}")
            if not advertiser:
                advertiser = "jtyoutube"
                logger.warning(f"advertiser missing, using default: {advertiser}")

            return {
                "transaction_id": transaction_id,
                "destination": destination,
                "advertiser": advertiser,
                "user_id": user_id,
            }

        elif status == 200:
            # ... (keep existing parsing logic, but also apply same defaults)
            logger.warning("Affiliate link returned 200 instead of 302. Attempting to parse response.")
            body = resp.text
            patterns = [
                r'<meta[^>]+url=([^"\']+)[\'"]',
                r'<a[^>]+href=(["\'])([^"\']+)\1',
                r'window\.location\s*=\s*["\']([^"\']+)["\']',
                r'location\.href\s*=\s*["\']([^"\']+)["\']',
            ]
            target_url = None
            for pattern in patterns:
                match = re.search(pattern, body, re.IGNORECASE)
                if match:
                    target_url = match.group(1) if len(match.groups()) == 1 else match.group(2)
                    break

            if target_url:
                parsed = urlparse(target_url)
                query = parse_qs(parsed.query)
                transaction_id = query.get("transaction_id", [None])[0]
                destination = query.get("destination", [None])[0]
                advertiser = query.get("advertiser", [None])[0]
                user_id = query.get("user_id", [None])[0]

                if transaction_id and user_id:
                    if not destination:
                        destination = "https://youtube.com"
                    if not advertiser:
                        advertiser = "jtyoutube"
                    return {
                        "transaction_id": transaction_id,
                        "destination": destination,
                        "advertiser": advertiser,
                        "user_id": user_id,
                    }

            # Fallback: search body for transaction_id and user_id
            tx_match = re.search(r'transaction_id["\']?\s*[:=]\s*["\']?([a-f0-9]+)["\']?', body, re.IGNORECASE)
            user_match = re.search(r'user_id["\']?\s*[:=]\s*["\']?([^"\']+)["\']?', body, re.IGNORECASE)
            if tx_match and user_match:
                transaction_id = tx_match.group(1)
                user_id = user_match.group(1)
                dest_match = re.search(r'destination["\']?\s*[:=]\s*["\']?([^"\']+)["\']?', body, re.IGNORECASE)
                adv_match = re.search(r'advertiser["\']?\s*[:=]\s*["\']?([^"\']+)["\']?', body, re.IGNORECASE)
                destination = dest_match.group(1) if dest_match else "https://youtube.com"
                advertiser = adv_match.group(1) if adv_match else "jtyoutube"
                return {
                    "transaction_id": transaction_id,
                    "destination": destination,
                    "advertiser": advertiser,
                    "user_id": user_id,
                }
            logger.error("Could not extract transaction_id and user_id from 200 response.")
            return None

        else:
            logger.error(f"Affiliate link returned unexpected status: {status}")
            return None

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