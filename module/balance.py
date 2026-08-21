import requests
from decimal import Decimal, ROUND_DOWN
from src.proxy_client import proxy_request

def update_balance(supabase, account_id=1):
    # Fetch account data (proxy_url, cookie, etc.)
    result = (
        supabase.table("jumptask")
        .select("id, auth, user_agent, cookie, proxy_url")
        .eq("id", account_id)
        .single()
        .execute()
    )
    account = result.data
    if not account:
        raise RuntimeError(f"Account id={account_id} not found")

    # Use proxy helper
    response = proxy_request(account, "accounting/balances", params={})
    response.raise_for_status()

    data = response.json()
    raw_balance = Decimal(data["data"]["total"])
    balance = raw_balance.quantize(Decimal("0.001"), rounding=ROUND_DOWN)

    supabase.table("jumptask").update({"balance": str(balance)}).eq("id", account_id).execute()

    print(f"ID={account_id} Balance = {balance}")
    print(f"Updated = {balance}")