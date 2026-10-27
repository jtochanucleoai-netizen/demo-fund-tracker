"""Shared helpers: HTTP fetching from Yahoo Finance, FRED and SEC EDGAR (stdlib only)."""
import csv
import io
import json
import os
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BROWSER_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"


def http_get(url, user_agent=BROWSER_UA, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": user_agent, "Accept-Encoding": "identity"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8")


def yahoo_quote(symbol):
    """Return dict(price, prev_close, change_pct, time, currency) or None.

    prev_close is the last daily close strictly before the current session,
    so change_pct matches what quote screens show.
    """
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/"
           + urllib.parse.quote(symbol) + "?range=10d&interval=1d")
    try:
        data = json.loads(http_get(url))
        res = data["chart"]["result"][0]
    except Exception:
        return None
    meta = res["meta"]
    price = meta.get("regularMarketPrice")
    mtime = meta.get("regularMarketTime")
    if price is None or mtime is None:
        return None
    session_day = datetime.fromtimestamp(mtime, timezone.utc).date()
    closes = res["indicators"]["quote"][0].get("close") or []
    prev = None
    for ts, c in zip(res.get("timestamp", []), closes):
        if c is not None and datetime.fromtimestamp(ts, timezone.utc).date() < session_day:
            prev = c
    chg = (price / prev - 1) * 100 if prev else None
    return {"price": price, "prev_close": prev, "change_pct": chg,
            "time": datetime.fromtimestamp(mtime, timezone.utc), "currency": meta.get("currency")}


def yahoo_history(symbol, rng="5y"):
    """Daily closes as list of (date, close)."""
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/"
           + urllib.parse.quote(symbol) + "?range=" + rng + "&interval=1d")
    res = json.loads(http_get(url))["chart"]["result"][0]
    out = []
    for ts, c in zip(res["timestamp"], res["indicators"]["quote"][0]["close"]):
        if c is not None:
            out.append((datetime.fromtimestamp(ts, timezone.utc).date(), c))
    return out


def fred_latest(series_id):
    """Return (date, value, prev_value) for a FRED series, skipping missing values."""
    start = (date.today() - timedelta(days=45)).isoformat()
    url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=" + series_id + "&cosd=" + start
    try:
        # FRED stalls on browser-like user agents but serves curl-style ones instantly.
        rows = list(csv.reader(io.StringIO(http_get(url, user_agent="curl/8.7.1"))))[1:]
    except Exception:
        return None
    vals = [(r[0], float(r[1])) for r in rows if len(r) > 1 and r[1] not in ("", ".")]
    if not vals:
        return None
    prev = vals[-2][1] if len(vals) > 1 else None
    return vals[-1][0], vals[-1][1], prev


def sec_user_agent():
    path = os.path.join(ROOT, "config", "sec_user_agent.txt")
    ua = os.environ.get("SEC_USER_AGENT")
    if not ua and os.path.exists(path):
        ua = open(path).read().strip()
    if not ua:
        raise SystemExit("Set a contact in config/sec_user_agent.txt (SEC requires 'name email').")
    return ua


def sec_get_json(url):
    return json.loads(http_get(url, user_agent=sec_user_agent()))
