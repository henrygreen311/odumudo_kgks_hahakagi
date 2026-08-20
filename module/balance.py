import requests
from decimal import Decimal, ROUND_DOWN

API_URL = "https://api.jumptask.io/accounting/balances"


def update_balance(supabase, account_id=1):
    result = (
        supabase.table("jumptask")
        .select("id, auth, user_agent")
        .eq("id", account_id)
        .single()
        .execute()
    )

    account = result.data

    if not account:
        raise RuntimeError(f"Account id={account_id} not found")

    headers = {
        "User-Agent": account["user_agent"],
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate, br",
        "Authorization": f"Bearer {account['auth']}",
        "Origin": "https://app.jumptask.io",
        "Referer": "https://app.jumptask.io/",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
        "Sec-Gpc": "1",
        "Te": "trailers",
    }

    response = requests.get(API_URL, headers=headers, timeout=15)
    response.raise_for_status()

    raw_balance = Decimal(response.json()["data"]["total"])

    balance = raw_balance.quantize(
        Decimal("0.001"),
        rounding=ROUND_DOWN,
    )

    supabase.table("jumptask").update(
        {"balance": str(balance)}
    ).eq("id", account_id).execute()

    print(f"ID={account_id} Balance = {balance}")
    print(f"Updated = {balance}")