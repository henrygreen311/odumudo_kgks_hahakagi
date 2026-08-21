# src/topic_tasker.py

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Any, Optional

import yt_dlp

from src.task_utils import follow_affiliate_link, get_tune_flow, get_affiliate_url
from src.proxy_client import proxy_request

logger = logging.getLogger(__name__)

FINAL_POST_DELAY = 2.5 * 60  # 2.5 minutes
MAX_WORKERS = 1

# Caches
_youtube_cache = {}
_challenge_cache = {}


# ---------- YouTube helpers ----------
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


def search_youtube(query: str, max_results: int = 50) -> List[Dict[str, str]]:
    cache_key = f"{query}_{max_results}"
    if cache_key in _youtube_cache:
        return _youtube_cache[cache_key]

    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': False,
        'playlistend': max_results,
        'skip_download': True,
        'ignoreerrors': True,
    }
    search_url = f"ytsearch{max_results}:{query}"
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(search_url, download=False)
            entries = info.get('entries', [])
            results = []
            for entry in entries:
                if entry is None:
                    continue
                results.append({
                    'url': entry.get('webpage_url'),
                    'title': entry.get('title'),
                    'description': entry.get('description') or '',
                })
            _youtube_cache[cache_key] = results
            logger.info(f"Found {len(results)} videos for query '{query}'")
            return results
    except Exception as e:
        logger.error(f"YouTube search failed for '{query}': {e}")
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
    # Fallback: grab ~100 chars after timestamp
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


def get_topic_for_offer(instructions: str) -> Optional[str]:
    query = extract_search_query(instructions)
    if not query:
        return None
    timestamp = extract_timestamp(instructions)
    if not timestamp:
        return None

    logger.info(f"Searching YouTube for '{query}' with timestamp {timestamp}...")
    videos = search_youtube(query, max_results=50)
    for video in videos:
        topic = extract_topic_from_description(video['description'], timestamp)
        if topic:
            logger.info(f"Found topic for {timestamp}: '{topic}' (from video: {video['title']})")
            return topic
    logger.warning(f"No topic found for timestamp {timestamp} in any of {len(videos)} videos.")
    return None


# ---------- Database helpers with retries ----------
def get_existing_challenge(supabase, offer_id: str) -> Optional[str]:
    """Query failed_text_offers with caching and retries."""
    if offer_id in _challenge_cache:
        return _challenge_cache[offer_id]

    for attempt in range(1, 4):
        try:
            resp = (
                supabase.table("failed_text_offers")
                .select("challenge")
                .eq("task_id", offer_id)
                .execute()
            )
            challenge = resp.data[0]["challenge"] if resp.data else None
            _challenge_cache[offer_id] = challenge
            return challenge
        except Exception as e:
            if attempt == 3:
                logger.warning(f"Failed to fetch challenge for {offer_id} after 3 attempts: {e}")
                _challenge_cache[offer_id] = None
                return None
            else:
                logger.warning(f"Supabase error (attempt {attempt}) for {offer_id}: {e}. Retrying in {attempt}s...")
                time.sleep(attempt)  # 1s, 2s
    return None


def delete_failed_offer(supabase, offer_id: str) -> None:
    try:
        supabase.table("failed_text_offers").delete().eq("task_id", offer_id).execute()
        logger.info(f"Deleted offer {offer_id} from DB.")
        _challenge_cache.pop(offer_id, None)
    except Exception as e:
        logger.error(f"Error deleting offer {offer_id}: {e}")


def store_failed_topic_offer(supabase, offer_id: str, instructions: str, search_query: str, timestamp: str) -> None:
    """Store failed topic offer details for manual review."""
    table = "failed_text_offers"
    # Check if already exists with a challenge (use retry)
    existing = None
    for attempt in range(1, 4):
        try:
            resp = (
                supabase.table(table)
                .select("task_id, challenge")
                .eq("task_id", offer_id)
                .execute()
            )
            existing = resp.data
            break
        except Exception as e:
            if attempt == 3:
                logger.error(f"Failed to check existing row for {offer_id}: {e}")
                return
            time.sleep(attempt)

    if existing and existing[0].get("challenge") is not None:
        logger.info(f"Offer {offer_id} already has a challenge, skipping storage.")
        return

    data = {
        "task_id": offer_id,
        "instruction": instructions,
        "image_url": None,
        "challenge": None,
        "search_query": search_query,
        "timestamp": timestamp,
        "type": "topic",
    }
    try:
        if existing:
            supabase.table(table).update(data).eq("task_id", offer_id).execute()
        else:
            supabase.table(table).insert(data).execute()
        logger.info(f"Stored failed topic offer {offer_id} in DB.")
    except Exception as e:
        logger.error(f"Failed to store offer {offer_id}: {e}")


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

    # Check DB for manually filled challenge
    existing = get_existing_challenge(supabase, offer_id)
    if existing:
        challenge = existing
        used_db = True
        logger.info(f"Using existing challenge from DB: {challenge}")
    else:
        search_query = extract_search_query(instructions)
        if not search_query:
            logger.info("Skipping: could not extract search query")
            return False
        timestamp = extract_timestamp(instructions)
        if not timestamp:
            logger.info("Skipping: could not extract timestamp")
            return False
        logger.info(f"Search query: '{search_query}', timestamp: {timestamp}")

        challenge = get_topic_for_offer(instructions)
        if not challenge:
            logger.warning(f"No topic found for {offer_id}. Storing for manual review.")
            store_failed_topic_offer(supabase, offer_id, instructions, search_query, timestamp)
            return False
        used_db = False

    uid = account.get("uid", "").strip()
    if not uid:
        logger.error(f"uid is empty for offer {offer_id}. Cannot proceed.")
        return False

    user_agent = account["user_agent"]
    max_retries = 3

    for attempt in range(1, max_retries + 1):
        logger.info(f"Attempt {attempt} for topic offer {offer_id}")
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
            if not used_db:
                store_failed_topic_offer(supabase, offer_id, instructions, search_query, timestamp)
            else:
                logger.warning(f"Offer {offer_id} had DB challenge but it's invalid.")
            return False

        if status == 200 and data and data.get("offer_id") == offer_id:
            logger.info(f"Status: {status}, Successful: {data}")
            if used_db:
                delete_failed_offer(supabase, offer_id)
            return True

        if status is not None:
            logger.warning(f"Attempt {attempt} failed with status {status}")

    logger.error(f"Topic offer {offer_id} failed after {max_retries} attempts.")
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