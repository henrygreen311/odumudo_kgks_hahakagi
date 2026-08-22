# src/hashtag_tasker.py

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Any, Optional, Set
from urllib.parse import urlparse, parse_qs

import requests

from src.task_utils import follow_affiliate_link, get_tune_flow, get_affiliate_url
from src.proxy_client import proxy_request

logger = logging.getLogger(__name__)

FINAL_POST_DELAY = 2.5 * 60  # 2.5 minutes (only for the first attempt)
MAX_WORKERS = 5
YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"

# Cache for YouTube results
_youtube_cache = {}


# ---------- YouTube API helpers ----------
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


def search_youtube_api(query: str, api_key: str, max_results: int = 50, proxy: Optional[str] = None) -> List[Dict[str, str]]:
    cache_key = f"{query}_{max_results}_{api_key[:4]}"
    if cache_key in _youtube_cache:
        return _youtube_cache[cache_key]

    search_url = f"{YOUTUBE_API_BASE}/search"
    params = {
        'part': 'snippet',
        'q': query,
        'type': 'video',
        'maxResults': max_results,
        'key': api_key,
    }
    try:
        session = requests.Session()
        if proxy:
            session.proxies = {'http': proxy, 'https': proxy}
        response = session.get(search_url, params=params, timeout=15)
        response.raise_for_status()
        data = response.json()

        video_ids = []
        for item in data.get('items', []):
            video_id = item.get('id', {}).get('videoId')
            if video_id:
                video_ids.append(video_id)

        if not video_ids:
            logger.warning(f"No videos found for query '{query}'")
            return []

        details_url = f"{YOUTUBE_API_BASE}/videos"
        details_params = {
            'part': 'snippet',
            'id': ','.join(video_ids),
            'key': api_key,
        }
        response = session.get(details_url, params=details_params, timeout=15)
        response.raise_for_status()
        details_data = response.json()

        results = []
        for item in details_data.get('items', []):
            snippet = item.get('snippet', {})
            results.append({
                'url': f"https://www.youtube.com/watch?v={item['id']}",
                'title': snippet.get('title', ''),
                'description': snippet.get('description', ''),
            })
        _youtube_cache[cache_key] = results
        logger.info(f"Found {len(results)} videos for query '{query}'")
        return results

    except Exception as e:
        logger.error(f"YouTube API request failed for '{query}': {e}")
        return []


def extract_hashtags(description: str) -> Set[str]:
    return set(re.findall(r'(#\w+)', description))


def get_hashtag_candidates(main_hashtag: str, api_key: str, proxy: Optional[str] = None) -> List[str]:
    query = main_hashtag.lstrip('#')
    videos = search_youtube_api(query, api_key, max_results=50, proxy=proxy)
    if not videos:
        return [main_hashtag]

    candidates = set()
    for video in videos:
        desc = video.get('description', '')
        if main_hashtag in desc:
            candidates.update(extract_hashtags(desc))

    result = [main_hashtag] + sorted(candidates - {main_hashtag})

    if len(result) > 10:
        preview = result[:5]
        others = len(result) - 5
        logger.info(f"Found {len(result)} hashtag candidates: {preview} and {others} others.")
    else:
        logger.info(f"Found {len(result)} hashtag candidates: {result}")

    return result


# ---------- Affiliate link handler (unchanged) ----------
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
            if not all([transaction_id, destination, advertiser, user_id]):
                logger.error(f"Missing required parameters in Location: {location}")
                return None
            return {
                "transaction_id": transaction_id,
                "destination": destination,
                "advertiser": advertiser,
                "user_id": user_id,
            }

        elif status == 200:
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
                if all([transaction_id, destination, advertiser, user_id]):
                    return {
                        "transaction_id": transaction_id,
                        "destination": destination,
                        "advertiser": advertiser,
                        "user_id": user_id,
                    }

            tx_match = re.search(r'transaction_id["\']?\s*[:=]\s*["\']?([a-f0-9]+)["\']?', body, re.IGNORECASE)
            if tx_match:
                transaction_id = tx_match.group(1)
                dest_match = re.search(r'destination["\']?\s*[:=]\s*["\']?([^"\']+)["\']?', body, re.IGNORECASE)
                adv_match = re.search(r'advertiser["\']?\s*[:=]\s*["\']?([^"\']+)["\']?', body, re.IGNORECASE)
                user_match = re.search(r'user_id["\']?\s*[:=]\s*["\']?([^"\']+)["\']?', body, re.IGNORECASE)
                destination = dest_match.group(1) if dest_match else "https://youtube.com"
                advertiser = adv_match.group(1) if adv_match else "jtyoutube"
                user_id = user_match.group(1) if user_match else None
                if transaction_id and user_id:
                    return {
                        "transaction_id": transaction_id,
                        "destination": destination,
                        "advertiser": advertiser,
                        "user_id": user_id,
                    }
            logger.error("Could not extract transaction_id from 200 response.")
            return None

        else:
            logger.error(f"Affiliate link returned unexpected status: {status}")
            return None

    except Exception as e:
        logger.error(f"Failed to follow affiliate link: {e}")
        return None


def post_tune_flow_with_challenge(account, offer_id: str, user_id: str, challenge: str) -> tuple:
    if not user_id or not challenge:
        return None, None, False
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

    main_hashtag = extract_challenge(instructions)
    if not main_hashtag:
        logger.info("Skipping: could not extract challenge")
        return False

    api_key = account.get("youtube_api_key", "").strip()
    if not api_key:
        logger.error(f"No YouTube API key set for account.")
        return False

    proxy_for_requests = None
    proxy_url = account.get("proxy_url", "")
    if proxy_url and "infinityfree" not in proxy_url and "jumptask" not in proxy_url:
        if not proxy_url.startswith(('http://', 'https://')):
            proxy_url = 'http://' + proxy_url
        proxy_for_requests = proxy_url

    candidates = get_hashtag_candidates(main_hashtag, api_key, proxy=proxy_for_requests)
    if not candidates:
        logger.warning(f"No candidates found for {offer_id}. Skipping.")
        return False

    uid = account.get("uid", "").strip()
    if not uid:
        logger.error(f"uid is empty for offer {offer_id}. Cannot proceed.")
        return False

    user_agent = account["user_agent"]
    max_retries = 3
    tried_count = 0

    # First candidate gets the delay and full logging
    for idx_candidate, challenge in enumerate(candidates):
        if idx_candidate == 0:
            logger.info(f"Waiting {FINAL_POST_DELAY/60:.1f} minutes before first POST for {offer_id}...")
            time.sleep(FINAL_POST_DELAY)
            # Log the first candidate attempt (only once)
            logger.info(f"Trying challenge: '{challenge}' for offer {offer_id}")
        else:
            # For subsequent candidates, log nothing unless success
            pass

        for attempt in range(1, max_retries + 1):
            # Only log attempts for the first candidate
            if idx_candidate == 0:
                logger.info(f"Attempt {attempt}/{max_retries} for text offer {offer_id}")

            affiliate_url = get_affiliate_url(account, offer_id)
            if not affiliate_url:
                continue

            flow_params = follow_affiliate_link(affiliate_url, user_agent)
            if not flow_params:
                continue

            if not get_tune_flow(account, offer_id, flow_params["transaction_id"],
                                 flow_params["user_id"], flow_params["destination"],
                                 flow_params["advertiser"]):
                continue

            status, data, invalid = post_tune_flow_with_challenge(account, offer_id, uid, challenge)

            if invalid:
                # Only log invalid for first candidate
                if idx_candidate == 0:
                    logger.info(f"Status: {status}, Invalid challenge: {data}")
                break  # try next candidate

            if status == 200 and data and data.get("offer_id") == offer_id:
                logger.info(f"Status: {status}, Successful: {data}")
                return True

            # If we get here, something else failed (not invalid challenge)
            if status is not None:
                # Only log if it's the first candidate
                if idx_candidate == 0:
                    logger.warning(f"Attempt {attempt} failed with status {status}")

        # If we exhausted retries for this candidate, move on
        tried_count += 1

    # If we exit the loop, all candidates failed
    logger.error(f"Offer {offer_id} failed after trying {tried_count} candidates.")
    return False


def process_text_offers(supabase, account, text_tasks: List[Dict[str, str]]) -> None:
    if not text_tasks:
        logger.info("No text offers to process.")
        return

    total = len(text_tasks)
    logger.info(f"Processing {total} text tasks with {MAX_WORKERS} concurrent workers...")
    tasks = [(task['offer_id'], task['instructions'], idx, total) for idx, task in enumerate(text_tasks, 1)]

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {}
        for offer_id, instructions, idx, total in tasks:
            future = executor.submit(
                process_offer_with_delay,
                supabase,
                account,
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