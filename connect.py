"""Check the Alpaca API connection: prints account status, buying power, and market clock."""
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


def load_env(path=Path(__file__).with_name(".env")):
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def get(base, path, headers):
    req = urllib.request.Request(base + path, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def main():
    load_env()
    key = os.environ.get("APCA_API_KEY_ID")
    secret = os.environ.get("APCA_API_SECRET_KEY")
    base = os.environ.get("APCA_API_BASE_URL", "https://paper-api.alpaca.markets").rstrip("/")
    if not key or not secret or key == "your_key_id":
        sys.exit("Missing keys: copy .env.example to .env and add your Alpaca API key and secret.")

    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    try:
        acct = get(base, "/v2/account", headers)
        clock = get(base, "/v2/clock", headers)
    except urllib.error.HTTPError as e:
        sys.exit(f"Connection failed: HTTP {e.code} {e.read().decode(errors='replace')}")

    print(f"Connected to {base}")
    print(f"  Account:      {acct['account_number']} ({acct['status']})")
    print(f"  Equity:       ${float(acct['equity']):,.2f}")
    print(f"  Cash:         ${float(acct['cash']):,.2f}")
    print(f"  Buying power: ${float(acct['buying_power']):,.2f}")
    print(f"  Market open:  {clock['is_open']} (next open {clock['next_open']})")


if __name__ == "__main__":
    main()
