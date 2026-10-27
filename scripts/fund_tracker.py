"""Lecture demo-fund tracker: values the €50,000 fund daily from market prices (no broker account).

Usage: python3 scripts/fund_tracker.py
Reads data/tracker/fund.json, fetches daily prices from Yahoo Finance since the start date, and writes
  data/tracker/entry.json        entry prices, FX, costs and cash — locked once, after the 27 Oct US close
  data/tracker/nav.csv           daily fund value vs benchmark (the track record, committed to git)
  data/tracker/out/*.json        summary, positions, nav, limits as JSON
  data/tracker/tracker.html      the published tracker page (template + data)
Rules: buy at the 27 Oct official close; 0.5% FX fee on non-EUR purchases and on converting dividends;
dividends credited net of withholding tax on the ex-date; benchmark = IWDA.AS scaled to €50,000.
"""
import csv
import json
import os
import sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import ROOT, http_get  # noqa: E402

DIR = os.path.join(ROOT, "data", "tracker")
OUT = os.path.join(DIR, "out")
CFG = json.load(open(os.path.join(DIR, "fund.json"), encoding="utf-8"))
START = date.fromisoformat(CFG["start_date"])
CAP = CFG["capital_eur"]
FEE = CFG["fx_fee"]


def chart(symbol):
    """Daily closes {date: close}, dividends [(ex_date, amount)], last price/time/currency, previous close."""
    p1 = int(datetime(START.year, START.month, START.day, tzinfo=timezone.utc).timestamp()) - 4 * 86400
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/" + urllib.parse.quote(symbol)
           + f"?period1={p1}&period2={int(datetime.now(timezone.utc).timestamp()) + 86400}&interval=1d&events=div")
    res = json.loads(http_get(url))["chart"]["result"][0]
    meta = res["meta"]
    closes = {}
    for ts, c in zip(res.get("timestamp", []), res["indicators"]["quote"][0].get("close", [])):
        if c is not None:
            closes[datetime.fromtimestamp(ts + meta.get("gmtoffset", 0), timezone.utc).date()] = c
    divs = [(datetime.fromtimestamp(d["date"], timezone.utc).date(), d["amount"])
            for d in (res.get("events") or {}).get("dividends", {}).values()]
    scale = 0.01 if meta.get("currency") == "GBp" else 1.0      # London quotes in pence
    last_t = datetime.fromtimestamp(meta["regularMarketTime"], timezone.utc)
    last_d = (last_t.timestamp() + meta.get("gmtoffset", 0))
    closes[datetime.fromtimestamp(last_d, timezone.utc).date()] = meta["regularMarketPrice"]   # today's live/closing price
    return {"closes": {k: v * scale for k, v in sorted(closes.items())}, "divs": [(d, a * scale) for d, a in divs],
            "time": last_t, "currency": meta.get("currency")}


def at(series, d):
    """Last value on or before d (forward-fill across holidays)."""
    v = None
    for k, x in series.items():
        if k <= d:
            v = x
        else:
            break
    return v


def main():
    pos = CFG["positions"]
    syms = [p["symbol"] for p in pos] + list(CFG["fx_symbols"].values()) + [CFG["benchmark"]["symbol"]]
    with ThreadPoolExecutor(10) as ex:
        data = dict(zip(syms, ex.map(chart, syms)))
    now = datetime.now(timezone.utc)

    def eur_per(ccy, d):          # EUR value of one unit of ccy on day d
        return 1.0 if ccy == "EUR" else 1.0 / at(data[CFG["fx_symbols"][ccy]]["closes"], d)

    # ---- entry: lock once the start day's closes are final
    ep = os.path.join(DIR, "entry.json")
    entry = json.load(open(ep)) if os.path.exists(ep) else None
    if not entry or not entry.get("locked"):
        locked = now >= datetime.fromisoformat(CFG["lock_after_utc"].replace("Z", "+00:00"))
        shares = {p["id"]: p["shares"] for p in pos}
        def build():
            rows, cost = {}, 0.0
            for p in pos:
                px = data[p["symbol"]]["closes"].get(START)
                if px is None:
                    raise SystemExit(f"No {START} close for {p['symbol']} yet")
                fx = eur_per(p["ccy"], START)
                gross = shares[p["id"]] * px * fx
                fee = gross * FEE if p["ccy"] != "EUR" else 0.0
                rows[p["id"]] = {"shares": shares[p["id"]], "px": px, "fx": fx, "gross_eur": round(gross, 2), "fee_eur": round(fee, 2)}
                cost += gross + fee
            return rows, cost
        rows, cost = build()
        adjustments = []
        while CAP - cost < 0:                              # never start with negative cash
            t = CFG["shortfall_trim"]; shares[t] -= 1
            adjustments.append(f"{t} −1 share (cash shortfall)")
            rows, cost = build()
        entry = {"locked": locked, "date": START.isoformat(), "locked_at": now.isoformat(timespec="seconds") if locked else None,
                 "positions": rows, "cost_eur": round(cost, 2), "cash_eur": round(CAP - cost, 2),
                 "fees_eur": round(sum(r["fee_eur"] for r in rows.values()), 2), "adjustments": adjustments}
        json.dump(entry, open(ep, "w"), indent=1)
    E = entry["positions"]

    # ---- daily valuation
    bench = data[CFG["benchmark"]["symbol"]]["closes"]
    b0 = bench[START] if START in bench else at(bench, START)
    days = sorted({d for p in pos for d in data[p["symbol"]]["closes"] if d >= START})
    divs_net = {p["id"]: [] for p in pos}
    for p in pos:
        wh = CFG["withholding"][p["country"]]
        for d, a in data[p["symbol"]]["divs"]:
            if d > START:                                   # ex-date after purchase → entitled
                gross = E[p["id"]]["shares"] * a * eur_per(p["ccy"], d)
                net = gross * (1 - wh) * (1 - (FEE if p["ccy"] != "EUR" else 0))
                divs_net[p["id"]].append((d, net))
    nav = []
    for d in days:
        cash = entry["cash_eur"] + sum(n for v in divs_net.values() for dd, n in v if dd <= d)
        val = sum(E[p["id"]]["shares"] * at(data[p["symbol"]]["closes"], d) * eur_per(p["ccy"], d) for p in pos)
        b = at(bench, d)
        nav.append({"date": d.isoformat(), "fund_eur": round(val + cash, 2), "benchmark_eur": round(CAP * b / b0, 2),
                    "fund_ret": round((val + cash) / CAP - 1, 6), "benchmark_ret": round(b / b0 - 1, 6)})
    for r in nav:
        r["excess_pp"] = round((r["fund_ret"] - r["benchmark_ret"]) * 100, 3)
    today = days[-1]
    peak, mdd = 0, 0.0
    for r in nav:
        peak = max(peak, r["fund_eur"]); mdd = min(mdd, r["fund_eur"] / peak - 1)

    # ---- positions
    nav_now = nav[-1]["fund_eur"]
    prows = []
    for p in pos:
        e, c = E[p["id"]], data[p["symbol"]]["closes"]
        px, fx = at(c, today), eur_per(p["ccy"], today)
        prev_days = [k for k in c if k < today]
        prev = c[prev_days[-1]] if prev_days else e["px"]
        value = e["shares"] * px * fx
        cost = e["gross_eur"] + e["fee_eur"]
        dv = sum(n for _, n in divs_net[p["id"]])
        pnl = value + dv - cost
        prows.append({"id": p["id"], "name": p["name"], "ticker": p["symbol"], "exchange": p["exchange"], "bucket": p["bucket"],
                      "sector": p["sector"], "country": p["country"], "currency": p["ccy"], "shares": e["shares"],
                      "entry_price": round(e["px"], 4), "last_price": round(px, 4), "day_change_pct": round((px / prev - 1) * 100, 2),
                      "cost_eur": round(cost, 2), "value_eur": round(value, 2), "weight_pct": round(value / nav_now * 100, 2),
                      "dividends_eur": round(dv, 2), "pnl_eur": round(pnl, 2), "pnl_pct": round(pnl / cost * 100, 2),
                      "price_ret_pct": round((px / e["px"] - 1) * 100, 2), "fx_ret_pct": round((fx / e["fx"] - 1) * 100, 2),
                      "contribution_pp": round(pnl / CAP * 100, 3), "next_test": p["next_test"],
                      "price_time": data[p["symbol"]]["time"].isoformat(timespec="minutes")})
    prows.sort(key=lambda r: -r["value_eur"])

    # ---- limits
    def tot(f):
        return sum(r["weight_pct"] for r in prows if f(r))
    sectors = {}
    for r in prows:
        sectors[r["sector"]] = sectors.get(r["sector"], 0) + r["weight_pct"]
    top = prows[0]; topsec = max(sectors.items(), key=lambda x: x[1]); L = CFG["limits"]
    ai_ids = {p["id"] for p in pos if p["ai"]}
    def status(v, cap):
        return "Breach" if v > cap else ("Watch" if v > cap - 1 else "OK")
    lim = [("Largest position", f"{top['name']}", top["weight_pct"], L["position"]),
           ("Largest sector", topsec[0], topsec[1], L["sector"]),
           ("French-domiciled", "France", tot(lambda r: r["country"] == "France"), L["france"]),
           ("US-dollar exposure", "USD", tot(lambda r: r["currency"] == "USD"), L["usd"]),
           ("AI-linked", "Microsoft + SAP", tot(lambda r: r["id"] in ai_ids), L["ai"])]
    lrows = [{"limit": a, "detail": b, "value_pct": round(v, 2), "cap_pct": c, "status": status(v, c)} for a, b, v, c in lim]

    prev_nav = nav[-2]["fund_eur"] if len(nav) > 1 else CAP
    best = max(prows, key=lambda r: r["contribution_pp"]); worst = min(prows, key=lambda r: r["contribution_pp"])
    summary = [{
        "as_of": max(d["time"] for d in data.values()).isoformat(timespec="minutes"), "valuation_date": today.isoformat(),
        "start_date": START.isoformat(), "capital_eur": CAP, "fund_eur": nav_now, "benchmark_eur": nav[-1]["benchmark_eur"],
        "return_pct": round(nav[-1]["fund_ret"] * 100, 2), "benchmark_return_pct": round(nav[-1]["benchmark_ret"] * 100, 2),
        "excess_pp": round(nav[-1]["excess_pp"], 2), "pnl_eur": round(nav_now - CAP, 2),
        "day_change_eur": round(nav_now - prev_nav, 2), "day_change_pct": round((nav_now / prev_nav - 1) * 100, 2),
        "cash_eur": round(nav_now - sum(r["value_eur"] for r in prows), 2), "fees_eur": entry["fees_eur"],
        "dividends_eur": round(sum(r["dividends_eur"] for r in prows), 2), "max_drawdown_pct": round(mdd * 100, 2),
        "value_pct": round(tot(lambda r: r["bucket"] == "Value"), 1), "growth_pct": round(tot(lambda r: r["bucket"] == "Growth"), 1),
        "best_name": best["name"], "best_pp": best["contribution_pp"], "worst_name": worst["name"], "worst_pp": worst["contribution_pp"],
        "limits_breached": sum(1 for r in lrows if r["status"] == "Breach"), "entry_locked": entry["locked"],
        "holdings": len(prows), "adjustments": "; ".join(entry["adjustments"]) or "none",
    }]

    os.makedirs(OUT, exist_ok=True)
    for name, rows in (("summary", summary), ("positions", prows), ("nav", nav), ("limits", lrows)):
        json.dump(rows, open(os.path.join(OUT, f"{name}.json"), "w"), ensure_ascii=False, indent=1)
    with open(os.path.join(DIR, "nav.csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(nav[0].keys())); w.writeheader(); w.writerows(nav)
    tpl = open(os.path.join(DIR, "tracker-template.html"), encoding="utf-8").read()
    blob = json.dumps({"summary": summary, "positions": prows, "nav": nav, "limits": lrows}, ensure_ascii=False).replace("</", "<\\/")
    with open(os.path.join(DIR, "tracker.html"), "w", encoding="utf-8") as fh:
        fh.write(tpl.replace("{{DATA}}", blob))
    s = summary[0]
    print(f"{s['valuation_date']} (prices to {s['as_of']} UTC) · fund €{s['fund_eur']:,.2f} ({s['return_pct']:+.2f}%) vs MSCI World "
          f"{s['benchmark_return_pct']:+.2f}% · excess {s['excess_pp']:+.2f}pp · cash €{s['cash_eur']:,.2f} · fees €{s['fees_eur']:,.2f} · "
          f"entry {'LOCKED' if entry['locked'] else 'provisional'} · adjustments: {s['adjustments']}")


if __name__ == "__main__":
    main()
