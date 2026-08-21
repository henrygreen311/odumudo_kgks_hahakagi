# src/topic_tasker.py

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Any, Optional
from urllib.parse import urlparse, parse_qs

import yt_dlp
import requests

from src.task_utils import get_tune_flow, get_affiliate_url
from src.proxy_client import proxy_request

logger = logging.getLogger(__name__)

FINAL_POST_DELAY = 2.5 * 60  # 2.5 minutes
MAX_WORKERS = 5
MAX_CANDIDATES = 5  # try up to 5 topics per offer

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


def get_topic_candidates(instructions: str, max_candidates: int = MAX_CANDIDATES) -> List[str]:
    """
    Search YouTube for the query, collect topics from descriptions, return unique candidates.
    """
    query = extract_search_query(instructions)
    if not query:
        return []
    timestamp = extract_timestamp(instructions)
    if not timestamp:
        return []

    logger.info(f"Searching YouTube for '{query}' with timestamp {timestamp}...")
    videos = search_youtube(query, max_results=50)
    candidates = []
    seen = set()
    for video in videos:
        topic = extract_topic_from_description(video['description'], timestamp)
        if topic and topic not in seen:
            candidates.append(topic)
            seen.add(topic)
            if len(candidates) >= max_candidates:
                break
    logger.info(f"Found {len(candidates)} topic candidates for {timestamp}: {candidates}")
    return candidates


# ---------- Affiliate link handler (with fallback for 200) ----------
def follow_affiliate_link(affiliate_url: str, user_agent: str) -> Optional[Dict[str, str]]:
    """
    Follow the affiliate link (go2cloud.org) to get transaction_id and other params.
    Handles both 302 and 200 responses.
    """
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

        # Case 1: 302 redirect – extract Location
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

        # Case 2: 200 OK – try to extract params from body
        elif status == 200:
            logger.warning("Affiliate link returned 200 instead of 302. Attempting to parse response.")
            body = resp.text

            # Look for a meta refresh or a link with the target URL
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

            if not target_url:
                # Try to find transaction_id directly in the body (maybe it's in a JSON)
                transaction_id_match = re.search(r'transaction_id["\']?\s*[:=]\s*["\']?([a-f0-9]+)["\']?', body, re.IGNORECASE)
                if transaction_id_match:
                    transaction_id = transaction_id_match.group(1)
                    # We still need other params; we can try to find them too
                    destination_match = re.search(r'destination["\']?\s*[:=]\s*["\']?([^"\']+)["\']?', body, re.IGNORECASE)
                    advertiser_match = re.search(r'advertiser["\']?\s*[:=]\s*["\']?([^"\']+)["\']?', body, re.IGNORECASE)
                    user_id_match = re.search(r'user_id["\']?\s*[:=]\s*["\']?([^"\']+)["\']?', body, re.IGNORECASE)
                    destination = destination_match.group(1) if destination_match else "https://youtube.com"
                    advertiser = advertiser_match.group(1) if advertiser_match else "jtyoutube"
                    user_id = user_id_match.group(1) if user_id_match else None
                    if transaction_id and user_id:
                        return {
                            "transaction_id": transaction_id,
                            "destination": destination,
                            "advertiser": advertiser,
                            "user_id": user_id,
                        }
                # If we found a URL, parse it
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

            logger.error("Could not extract transaction_id from 200 response.")
            return None

        else:
            logger.error(f"Affiliate link returned unexpected status: {status}")
            return None

    except Exception as e:
        logger.error(f"Failed to follow affiliate link: {e}")
        return None


# ---------- Database helpers with retries ----------
def get_existing_challenge(supabase, offer_id: str) -> Optional[str]:
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
                time.sleep(attempt)
    return None


def delete_failed_offer(supabase, offer_id: str) -> None:
    try:
        supabase.table("failed_text_offers").delete().eq("task_id", offer_id).execute()
        logger.info(f"Deleted offer {offer_id} from DB.")
        _challenge_cache.pop(offer_id, None)
    except Exception as e:
        logger.error(f"Error deleting offer {offer_id}: {e}")


def store_failed_offer(supabase, offer_id: str, instructions: str) -> None:
    """Store failed offer details – keep it simple, only task_id, instruction, challenge (null)."""
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
        "challenge": None,  # leave null for manual fill
    }
    try:
        if existing:
            supabase.table(table).update(data).eq("task_id", offer_id).execute()
        else:
            supabase.table(table).insert(data).execute()
        logger.info(f"Stored failed offer {offer_id} in DB.")
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
        # Get list of topic candidates
        candidates = get_topic_candidates(instructions)
        if not candidates:
            logger.warning(f"No topic candidates found for {offer_id}. Storing for manual review.")
            store_failed_offer(supabase, offer_id, instructions)
            return False
        used_db = False

    uid = account.get("uid", "").strip()
    if not uid:
        logger.error(f"uid is empty for offer {offer_id}. Cannot proceed.")
        return False

    user_agent = account["user_agent"]
    max_retries = 3

    # If using DB challenge, we try it once (maybe with retries). Otherwise, iterate candidates.
    if used_db:
        challenges_to_try = [existing]
    else:
        challenges_to_try = candidates

    for challenge in challenges_to_try:
        logger.info(f"Trying challenge: '{challenge}' for offer {offer_id}")

        for attempt in range(1, max_retries + 1):
            logger.info(f"Attempt {attempt} for topic offer {offer_id} (challenge: {challenge[:30]}...)")
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
                # If it's invalid and we are not using DB, try next candidate
                break  # break out of retry loop to try next challenge

            if status == 200 and data and data.get("offer_id") == offer_id:
                logger.info(f"Status: {status}, Successful: {data}")
                if used_db:
                    delete_failed_offer(supabase, offer_id)
                return True

            if status is not None:
                logger.warning(f"Attempt {attempt} failed with status {status}")

        # If we get here, this challenge failed; move to next candidate
        if not used_db:
            logger.info(f"Challenge '{challenge}' failed, trying next candidate...")
        else:
            # DB challenge failed (all retries), we stop
            break

    # If we exhausted all candidates, store the offer for manual review
    if not used_db:
        logger.warning(f"All {len(candidates)} candidates failed for {offer_id}. Storing for manual review.")
        store_failed_offer(supabase, offer_id, instructions)

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