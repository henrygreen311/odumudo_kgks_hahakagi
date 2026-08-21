import requests
import json
from urllib.parse import urlparse

def proxy_request(account, endpoint, method='GET', params=None, json_data=None):
    """
    Send a request through the account's proxy to api.jumptask.io.
    account: dict with 'proxy_url', 'auth', 'user_agent', 'cookie'
    endpoint: API path (e.g., 'accounting/balances')
    method: 'GET' or 'POST'
    params: dict of query parameters (for GET) or body (for POST)
    json_data: dict to send as JSON body (POST only)
    Returns a requests.Response object.
    """
    proxy_base = account.get("proxy_url", "").strip()
    if not proxy_base:
        raise ValueError("proxy_url missing for account")
    if not proxy_base.startswith(("http://", "https://")):
        proxy_base = "https://" + proxy_base
    proxy_base = proxy_base.rstrip('/')
    proxy_url = f"{proxy_base}/proxy.php"
    parsed = urlparse(proxy_base)
    proxy_host = parsed.netloc

    # Build payload for the proxy
    payload = {
        "endpoint": endpoint,
        "auth": account["auth"],
        "user_agent": account["user_agent"],
        "query": json.dumps(params or {})
    }

    # Proxy‑level headers (as in test.py)
    headers = {
        "Host": proxy_host,
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": proxy_url,
        "Connection": "keep-alive",
    }

    session = requests.Session()
    session.headers.update(headers)
    if account.get("cookie"):
        session.cookies.set("__test", account["cookie"])

    if method.upper() == 'POST':
        return session.post(proxy_url, params=payload, json=json_data, timeout=15)
    else:
        return session.get(proxy_url, params=payload, timeout=15)
