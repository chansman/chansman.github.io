#!/usr/bin/env python3
"""Fetch daily prices for the class fixed income portfolio.

Treasury note: end-of-day prices from TreasuryDirect's FedInvest price table.
VMBS: daily closes and distributions from Yahoo Finance's chart feed.

Results are merged into data/prices.json. Standard library only, so the
GitHub Action needs no installs.
"""

import datetime as dt
import html
import http.cookiejar
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
PRICES = HERE / "data" / "prices.json"

CUSIP = "91282CQQ7"
TICKER = "VMBS"
HISTORY_START = dt.date(2026, 9, 1)  # store some history before the purchase
MAX_TREASURY_DAYS_PER_RUN = 40       # be polite to TreasuryDirect when backfilling

UA = "Mozilla/5.0 (compatible; econ446-fixedincome/1.0; +https://chansman.github.io/fixedincome/)"
FEDINVEST = "https://www.treasurydirect.gov/GA-FI/FedInvest/selectSecurityPriceDate"


def load():
    if PRICES.exists():
        return json.loads(PRICES.read_text())
    return {"treasury": {"cusip": CUSIP, "prices": []},
            "vmbs": {"ticker": TICKER, "prices": [], "distributions": []}}


def fedinvest_prices(day):
    """Return {'buy','sell','eod'} for CUSIP on `day`, or None if no table that day."""
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.addheaders = [("User-Agent", UA)]

    form = opener.open(FEDINVEST, timeout=60).read().decode("utf-8", "replace")
    token = re.search(r'name="_csrf" value="([^"]+)"', form)
    if not token:
        raise RuntimeError("FedInvest form token not found")

    body = urllib.parse.urlencode({
        "priceDate": day.isoformat(),
        "submit": "Show Prices",
        "_csrf": token.group(1),
    }).encode()
    page = opener.open(FEDINVEST, data=body, timeout=60).read().decode("utf-8", "replace")

    heading = re.search(r"<h2>Prices For:\s*([^<]+)</h2>", page)
    if not heading:
        return None
    shown = dt.datetime.strptime(heading.group(1).strip(), "%B %d, %Y").date()
    if shown != day:
        return None  # holiday or weekend: FedInvest shows a different date

    row = re.search(r"<td>\s*%s\s*</td>(.*?)</tr>" % CUSIP, page, re.S)
    if not row:
        return None
    cells = [html.unescape(re.sub(r"<[^>]+>", "", c)).strip()
             for c in re.findall(r"<td[^>]*>(.*?)</td>", row.group(1), re.S)]
    # cells after CUSIP: type, rate, maturity, call date, buy, sell, end of day
    buy, sell, eod = (float(x) for x in cells[4:7])
    return {"buy": buy, "sell": sell, "eod": eod}


def update_treasury(data, today):
    prices = data["treasury"]["prices"]
    have = {p["date"] for p in prices}
    day = HISTORY_START
    fetched = 0
    while day < today and fetched < MAX_TREASURY_DAYS_PER_RUN:
        if day.weekday() < 5 and day.isoformat() not in have:
            try:
                row = fedinvest_prices(day)
            except Exception as err:  # keep going; try again tomorrow
                print(f"FedInvest {day}: {err}", file=sys.stderr)
                row = None
            fetched += 1
            if row:
                prices.append({"date": day.isoformat(), **row})
                print(f"Treasury {day}: {row}")
            time.sleep(1)
        day += dt.timedelta(days=1)
    # also try today, in case prices are already posted
    if today.weekday() < 5 and today.isoformat() not in have:
        try:
            row = fedinvest_prices(today)
            if row:
                prices.append({"date": today.isoformat(), **row})
                print(f"Treasury {today}: {row}")
        except Exception as err:
            print(f"FedInvest {today}: {err}", file=sys.stderr)
    prices.sort(key=lambda p: p["date"])


def update_vmbs(data):
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/%s"
           "?range=2y&interval=1d&events=div" % TICKER)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    for attempt in range(3):
        try:
            chart = json.load(urllib.request.urlopen(req, timeout=60))
            break
        except Exception as err:
            print(f"Yahoo attempt {attempt + 1}: {err}", file=sys.stderr)
            time.sleep(5)
    else:
        return

    result = chart["chart"]["result"][0]
    offset = dt.timedelta(seconds=result["meta"]["gmtoffset"])

    def to_date(ts):
        return (dt.datetime.fromtimestamp(ts, dt.timezone.utc) + offset).date()

    closes = result["indicators"]["quote"][0]["close"]
    prices = {p["date"]: p for p in data["vmbs"]["prices"]}
    for ts, close in zip(result.get("timestamp", []), closes):
        day = to_date(ts)
        if close is None or day < HISTORY_START:
            continue
        prices[day.isoformat()] = {"date": day.isoformat(), "close": round(close, 4)}
    data["vmbs"]["prices"] = sorted(prices.values(), key=lambda p: p["date"])

    # keep two years of distributions so the page can compute a 12-month yield
    divs = result.get("events", {}).get("dividends", {}).values()
    data["vmbs"]["distributions"] = sorted(
        ({"exDate": to_date(d["date"]).isoformat(), "amount": round(d["amount"], 6)} for d in divs),
        key=lambda d: d["exDate"])


def main():
    data = load()
    before = json.dumps(data, sort_keys=True)
    today = dt.datetime.now(dt.timezone(dt.timedelta(hours=-4))).date()
    update_treasury(data, today)
    update_vmbs(data)
    if json.dumps(data, sort_keys=True) != before:
        data["updated"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
        PRICES.write_text(json.dumps(data, indent=1) + "\n")
        print("prices.json updated")
    else:
        print("no new prices")


if __name__ == "__main__":
    main()
