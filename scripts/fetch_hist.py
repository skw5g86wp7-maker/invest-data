#!/usr/bin/env python3
"""Fetch ~20y monthly adjusted closes from Yahoo and write docs/data/hist/<code>.json.

Universe: hist_watchlist.txt + us_symbols.txt + top TWSE/TPEx names by daily turnover.
Yahoo symbols: <code>.TW (TWSE), <code>.TWO (TPEx), bare ticker (US).
"""
from __future__ import annotations

import json, ssl, sys, time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "data" / "hist"
TW_JSON = ROOT / "docs" / "data" / "prices" / "tw.json"
US_JSON = ROOT / "docs" / "data" / "prices" / "us.json"
WATCH = ROOT / "hist_watchlist.txt"
US_SYM = ROOT / "us_symbols.txt"

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
CTX = ssl.create_default_context()
TPE = timezone(timedelta(hours=8))
MAX_YEARS = 20
TWSE_TOP, TPEX_TOP = 200, 100
SLEEP = 0.15

TWSE_DAY = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TPEX_DAY = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"


def now_iso():
    return datetime.now(TPE).strftime("%Y-%m-%dT%H:%M:%S+08:00")


def write_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


def compact(v):
    if v is None:
        return None
    try:
        x = round(float(v), 4)
    except (TypeError, ValueError):
        return None
    return int(x) if x == int(x) else x


def to_float(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    t = str(v).replace(",", "").replace("%", "").strip()
    if not t or t in {"--", "---", "N/A", "-", "無"}:
        return None
    try:
        return float(t)
    except ValueError:
        return None


def get_json(url, retries=3, timeout=40):
    last = None
    for i in range(retries):
        req = Request(url, headers={"User-Agent": UA, "Accept": "application/json,*/*"})
        try:
            with urlopen(req, timeout=timeout, context=CTX) as r:
                return json.loads(r.read().decode("utf-8-sig", errors="replace"))
        except Exception as e:
            last = e
            time.sleep(0.8 * (i + 1))
    raise last if last else RuntimeError(url)


def load_lines(path: Path):
    if not path.exists():
        return []
    out, seen = [], set()
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.split("#", 1)[0].strip().upper()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def load_markets():
    tw, us = {}, {}
    if TW_JSON.exists():
        try:
            tw = json.loads(TW_JSON.read_text(encoding="utf-8"))
        except Exception:
            tw = {}
    if US_JSON.exists():
        try:
            us = json.loads(US_JSON.read_text(encoding="utf-8"))
        except Exception:
            us = {}
    return tw if isinstance(tw, dict) else {}, us if isinstance(us, dict) else {}


def market_of(code, tw, us):
    if code in us or (code.isalpha() or (code[:1].isalpha() and "." in code)):
        if code in tw and tw[code].get("market") in {"TWSE", "TPEx"}:
            return tw[code]["market"]
        return "US"
    rec = tw.get(code) or {}
    return rec.get("market") or ("TWSE" if code.startswith("00") or (code[:1].isdigit() and len(code) == 4) else "TWSE")


def yahoo_candidates(code, market):
    if market == "US":
        return [code]
    if market == "TPEx":
        return [f"{code}.TWO", f"{code}.TW"]
    return [f"{code}.TW", f"{code}.TWO"]


def latest_date(rows, key="Date"):
    dates = [str(r.get(key) or "") for r in rows if r.get(key)]
    return max(dates) if dates else None


def rank_tw_turnover():
    """Return [(code, market, value), ...] for the latest session."""
    ranked = []
    try:
        rows = get_json(TWSE_DAY, retries=2, timeout=25)
        if isinstance(rows, list) and rows:
            latest = latest_date(rows)
            n = 0
            for r in rows:
                if latest and str(r.get("Date") or "") != latest:
                    continue
                code = str(r.get("Code") or "").strip().upper()
                val = to_float(r.get("TradeValue"))
                if code and val:
                    ranked.append((code, "TWSE", val))
                    n += 1
            print(f"TWSE turnover rows {n} date={latest}", flush=True)
    except Exception as e:
        print(f"TWSE turnover: {e}", flush=True)
    # Skip TPEX_DAY here: the official dump is huge and often hangs. TPEx names
    # come from hist_watchlist.txt and whatever is already in docs/data/hist/.
    return ranked


def build_universe(tw, us):
    items = {}  # code -> market

    def add(code, market=None):
        code = (code or "").strip().upper()
        if not code:
            return
        m = market or market_of(code, tw, us)
        items[code] = m

    for c in load_lines(WATCH):
        add(c)
    for c in load_lines(US_SYM):
        add(c, "US")
    for c, rec in us.items():
        add(c, rec.get("market") or "US")

    ranked = rank_tw_turnover()
    twse = [x for x in ranked if x[1] == "TWSE"]
    tpex = [x for x in ranked if x[1] == "TPEx"]
    twse.sort(key=lambda x: -x[2])
    tpex.sort(key=lambda x: -x[2])
    for code, m, _ in twse[:TWSE_TOP] + tpex[:TPEX_TOP]:
        add(code, m)

    # Reuse last run's universe if today's turnover lists were slow/empty.
    if OUT.exists():
        for fp in OUT.glob("*.json"):
            if fp.name == "meta.json":
                continue
            try:
                rec = json.loads(fp.read_text(encoding="utf-8"))
                add(rec.get("code") or fp.stem, rec.get("market"))
            except Exception:
                add(fp.stem)

    # keep a stable order: watchlist, US, then the rest alpha
    order, seen = [], set()
    for c in load_lines(WATCH) + load_lines(US_SYM):
        if c in items and c not in seen:
            order.append(c)
            seen.add(c)
    for c in sorted(items):
        if c not in seen:
            order.append(c)
            seen.add(c)
    return [(c, items[c]) for c in order]


def to_monthly(pts):
    """Keep the last close of each calendar month so vol/CAGR stay monthly."""
    m = {}
    for d, c in pts:
        m[d[:7]] = [d, c]
    return [m[k] for k in sorted(m)]

def fetch_monthly(symbol):
    # range=20y keeps interval=1mo actually monthly; range=max can turn into quarterly/weekly.
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=20y&interval=1mo"
    data = get_json(url, retries=3, timeout=25)
    err = (data.get("chart") or {}).get("error")
    results = (data.get("chart") or {}).get("result") or []
    if err or not results:
        raise RuntimeError(err or "empty yahoo chart")
    res = results[0]
    meta = res.get("meta") or {}
    ts = res.get("timestamp") or []
    closes = ((res.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
    adj_list = (res.get("indicators") or {}).get("adjclose") or [{}]
    adj = (adj_list[0].get("adjclose") if adj_list else None) or []
    used_adj = bool(adj and any(x is not None for x in adj))
    off = int(meta.get("gmtoffset") or 0)
    pts = []
    for i, t in enumerate(ts):
        raw = adj[i] if used_adj and i < len(adj) and adj[i] is not None else (
            closes[i] if i < len(closes) else None
        )
        c = compact(raw)
        if c is None or c <= 0:
            continue
        d = datetime.fromtimestamp(int(t) + off, timezone.utc).strftime("%Y-%m-%d")
        pts.append([d, c])
    pts = to_monthly(pts)
    name = (meta.get("shortName") or meta.get("longName") or symbol).strip()
    currency = (meta.get("currency") or "").upper()
    return {
        "name": name,
        "currency": currency or None,
        "adj": used_adj,
        "d": pts,
    }


def fetch_one(code, market):
    last = None
    for sym in yahoo_candidates(code, market):
        try:
            rec = fetch_monthly(sym)
            if rec["d"] and len(rec["d"]) >= 2:
                rec["symbol"] = sym
                return rec
            last = RuntimeError(f"{sym}: too few points")
        except Exception as e:
            last = e
            time.sleep(0.4)
    raise last if last else RuntimeError("no yahoo data")


def main():
    tw, us = load_markets()
    universe = build_universe(tw, us)
    print(f"universe {len(universe)} (watch + US + top turnover)", flush=True)
    OUT.mkdir(parents=True, exist_ok=True)

    ok, fail = [], []
    t0 = time.time()
    for i, (code, market) in enumerate(universe, 1):
        try:
            rec = fetch_one(code, market)
            name = rec.get("name") or (tw.get(code) or us.get(code) or {}).get("name") or ""
            payload = {
                "code": code,
                "market": market,
                "symbol": rec["symbol"],
                "name": name,
                "asOf": rec["d"][-1][0] if rec["d"] else None,
                "adj": rec.get("adj", False),
                "currency": rec.get("currency"),
                "n": len(rec["d"]),
                "source": "yahoo",
                "d": rec["d"],
            }
            write_json(OUT / f"{code}.json", payload)
            ok.append({"code": code, "market": market, "n": payload["n"], "asOf": payload["asOf"]})
            if i % 25 == 0 or code in {"8299", "AAPL", "2330"}:
                print(f"  [{i}/{len(universe)}] {code} {market} {payload['n']} pts {payload['asOf']}", flush=True)
        except Exception as e:
            fail.append({"code": code, "market": market, "err": str(e)[:200]})
            print(f"  FAIL {code} {market}: {e}", flush=True)
        time.sleep(SLEEP)

    meta = {
        "generatedAt": now_iso(),
        "universe": len(universe),
        "ok": len(ok),
        "fail": len(fail),
        "fails": fail,
        "elapsedSec": round(time.time() - t0, 1),
        "source": "https://query1.finance.yahoo.com/v8/finance/chart",
        "interval": "1mo",
        "years": MAX_YEARS,
        "note": "Monthly adjclose when available (dividends/splits). TWSE=.TW, TPEx=.TWO, US=ticker.",
    }
    write_json(OUT / "meta.json", meta)
    print(f"done ok={len(ok)} fail={len(fail)} elapsed={meta['elapsedSec']}s", flush=True)
    if fail:
        print("failures:", ", ".join(f['code'] for f in fail[:20]), flush=True)
    # non-zero only if nothing worked
    if not ok:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
