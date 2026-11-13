"""Place a crypto market order on Alpaca. Usage: python crypto_order.py XRP/USD buy 1"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

from connect import load_env


def call(method, url, headers, body=None):
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={**headers, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code}: {e.read().decode(errors='replace')}")


def main():
    symbol, side, qty = sys.argv[1], sys.argv[2], sys.argv[3]
    load_env()
    base = os.environ["APCA_API_BASE_URL"].rstrip("/")
    h = {"APCA-API-KEY-ID": os.environ["APCA_API_KEY_ID"],
         "APCA-API-SECRET-KEY": os.environ["APCA_API_SECRET_KEY"]}

    asset = call("GET", f"{base}/v2/assets/{symbol.replace('/', '%2F')}", h)
    if not asset.get("tradable"):
        sys.exit(f"{symbol} is not tradable")

    q = call("GET", "https://data.alpaca.markets/v1beta3/crypto/us/latest/quotes?symbols="
             + symbol.replace("/", "%2F"), h)["quotes"][symbol]
    print(f"{symbol} quote: bid ${q['bp']}  ask ${q['ap']}")

    order = call("POST", f"{base}/v2/orders", h, {
        "symbol": symbol, "qty": qty, "side": side, "type": "market", "time_in_force": "gtc"})
    print(f"Order submitted: {order['id']} ({order['status']})")

    for _ in range(10):
        time.sleep(1)
        order = call("GET", f"{base}/v2/orders/{order['id']}", h)
        if order["status"] in ("filled", "canceled", "rejected", "expired"):
            break
    print(f"Status: {order['status']}  filled {order['filled_qty']} @ ${order['filled_avg_price']}")


if __name__ == "__main__":
    main()
