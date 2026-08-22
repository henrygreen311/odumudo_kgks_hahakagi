# src/topic_tasker.py

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Any, Optional
from urllib.parse import urlparse, parse_qs

import requests

from src.task_utils import get_tune_flow, get_affiliate_url
from src.proxy_client import proxy_request

logger = logging.getLogger(__name__)

FINAL_POST_DELAY = 2.5 * 60  # 2.5 minutes
MAX_WORKERS = 5
MAX_CANDIDATES = 5
YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"

# Cache for YouTube results only (no DB cache)
_youtube_cache = {}


# ---------- YouTube API helpers ----------
def extract_search_query(instructions: str) -> Optional[str]:
    clean = instructions.replace('*', '')
    patterns = [
        r'(?:Type\s+in|Search)\s+["“]([^"”]+)["”]',
        r'(?:Type\s+in|Search)\s+[“"]([^"”]+)[”"]',
    ]
    for pattern in patterns:
        match = re.search(pattern, clean, re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return None


def extract_timestamp(instructions: str) -> Optional[str]:
    match = re.search(r'\b(\d{1,2}:\d{2}(?::\d{2})?)\b', instructions)
    if match:
        return match.group(1)
    return None


def search_youtube_api(query: str, api_key: str, max_results: int = 50, proxy: Optional[str] = None) -> List[Dict[str, str]]:
    cache_key = f"{query}_{max_results}_{api_key[:4]}"
    if cache_key in _youtube_cache:
        return _youtube_cache[cache_key]

    # Search for videos
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

        # Fetch video details (descriptions)
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

    except requests.exceptions.RequestException as e:
        logger.error(f"YouTube API request failed for '{query}': {e}")
        return []
    except KeyError as e:
        logger.error(f"Unexpected API response structure for '{query}': {e}")
        return []


def extract_topic_from_description(description: str, timestamp: str) -> Optional[str]:
    if not description or not timestamp:
        return None
    ts_escaped = re.escape(timestamp)
    pattern = re.compile(rf'{ts_escaped}\s*[-:]\s*(.*?)(?:\n|\.|$)', re.IGNORECASE)
    match = pattern.search(description)
    if match:
        topic = match.group(1).strip()
        if topic:
            return topic
    # Fallback: grab 100 chars after timestamp
    idx = description.lower().find(timestamp.lower())
    if idx != -1:
        start = idx + len(timestamp)
        while start < len(description) and not description[start].isalnum():
            start += 1
        end = min(start + 100, len(description))
        snippet = description[start:end].strip()
        for delim in ['.', '\n', '!', '?']:
            pos = snippet.find(delim)
            if pos != -1:
                snippet = snippet[:pos].strip()
                break
        if snippet:
            return snippet
    return None


def get_topic_candidates(instructions: str, api_key: str, proxy: Optional[str] = None, max_candidates: int = MAX_CANDIDATES) -> List[str]:
    query = extract_search_query(instructions)
    if not query:
        logger.warning("Could not extract search query from instructions.")
        return []
    timestamp = extract_timestamp(instructions)
    if not timestamp:
        logger.warning("Could not extract timestamp from instructions.")
        return []

    logger.info(f"Searching YouTube API for '{query}' with timestamp {timestamp}...")
    videos = search_youtube_api(query, api_key, max_results=50, proxy=proxy)
    candidates = []
    seen = set()
    for video in videos:
        topic = extract_topic_from_description(video['description'], timestamp)
        if topic and topic not in seen:
            candidates.append(topic)
            seen.add(topic)
            if len(candidates) >= max_candidates:
                break
    if candidates:
        logger.info(f"Found {len(candidates)} topic candidates for {timestamp}: {candidates}")
    else:
        logger.warning(f"No topic candidates found for timestamp {timestamp} in {len(videos)} videos.")
    return candidates


# ---------- Affiliate link handler (with fallback for 200) ----------
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


def process_topic_offer(supabase, account, offer_id: str, instructions: str, idx: int, total: int) -> bool:
    logger.info(f"ID={offer_id} Processing {idx}/{total}")

    # Get API key and proxy
    api_key = account.get("youtube_api_key", "").strip()
    if not api_key:
        logger.error(f"No YouTube API key set for account. Please add youtube_api_key to the jumptask table.")
        return False

    proxy_for_requests = None
    proxy_url = account.get("proxy_url", "")
    if proxy_url and "infinityfree" not in proxy_url and "jumptask" not in proxy_url:
        if not proxy_url.startswith(('http://', 'https://')):
            proxy_url = 'http://' + proxy_url
        proxy_for_requests = proxy_url
        logger.info(f"Using proxy for API requests: {proxy_for_requests}")

    # Get topic candidates
    search_query = extract_search_query(instructions)
    timestamp = extract_timestamp(instructions)
    if not search_query or not timestamp:
        logger.warning(f"Could not extract search query or timestamp for {offer_id}. Skipping.")
        return False

    candidates = get_topic_candidates(instructions, api_key, proxy=proxy_for_requests)
    if not candidates:
        logger.warning(f"No topic candidates found for {offer_id}. Skipping.")
        return False

    uid = account.get("uid", "").strip()
    if not uid:
        logger.error(f"uid is empty for offer {offer_id}. Cannot proceed.")
        return False

    user_agent = account["user_agent"]
    max_retries = 2  # only 2 attempts per challenge

    for challenge in candidates:
        logger.info(f"Trying challenge: '{challenge}' for offer {offer_id}")

        for attempt in range(1, max_retries + 1):
            logger.info(f"Attempt {attempt}/{max_retries} for topic offer {offer_id} (challenge: {challenge[:30]}...)")
            affiliate_url = get_affiliate_url(account, offer_id)
            if not affiliate_url:
                continue

            flow_params = follow_affiliate_link(affiliate_url, user_agent)
            if not flow_params:
                continue

            if not get_tune_flow(account, offer_id,
                                 flow_params["transaction_id"],
                                 flow_params["user_id"],
                                 flow_params["destination"],
                                 flow_params["advertiser"]):
                continue

            logger.info(f"Waiting {FINAL_POST_DELAY/60:.1f} minutes before final POST for {offer_id}...")
            time.sleep(FINAL_POST_DELAY)

            status, data, invalid = post_tune_flow_with_challenge(account, offer_id, uid, challenge)
            if invalid:
                logger.info(f"Status: {status}, Invalid challenge: {data}")
                break  # try next candidate

            if status == 200 and data and data.get("offer_id") == offer_id:
                logger.info(f"Status: {status}, Successful: {data}")
                return True

            if status is not None:
                logger.warning(f"Attempt {attempt} failed with status {status}")

        # If we get here, all retries for this challenge failed
        logger.info(f"Challenge '{challenge}' failed after {max_retries} attempts, trying next candidate...")

    logger.error(f"Topic offer {offer_id} failed after trying all candidates.")
    return False


def process_topic_offers(supabase, account, topic_tasks: List[Dict[str, str]]) -> None:
    if not topic_tasks:
        logger.info("No topic offers to process.")
        return

    total = len(topic_tasks)
    logger.info(f"Processing {total} topic tasks with {MAX_WORKERS} concurrent workers...")
    tasks = [(task['offer_id'], task['instructions'], idx, total) for idx, task in enumerate(topic_tasks, 1)]

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {}
        for offer_id, instructions, idx, total in tasks:
            future = executor.submit(
                process_topic_offer,
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