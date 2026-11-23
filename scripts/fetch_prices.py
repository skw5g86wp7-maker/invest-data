#!/usr/bin/env python3
"""Fetch TWSE / TPEx closes and a US watchlist, write docs/data/prices/.

TWSE: official openapi STOCK_DAY_ALL + BWIBBU_ALL (rwd as a freshness fallback).
TPEx: openapi tpex_mainboard_daily_close_quotes + tpex_mainboard_peratio_analysis.
US: symbols in us_symbols.txt via Stooq, falling back to the Yahoo chart API (no key).
"""
from __future__ import annotations

import json, ssl, sys, time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "data" / "prices"
SYM_PATH = ROOT / "us_symbols.txt"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
CTX = ssl.create_default_context()
TPE = timezone(timedelta(hours=8))

TWSE_DAY = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TWSE_PE = "https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL"
TWSE_DAY_RWD = "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY_ALL?response=json"
TWSE_PE_RWD = "https://www.twse.com.tw/rwd/zh/afterTrading/BWIBBU_ALL?response=json"
TPEX_DAY = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"
TPEX_PE = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_peratio_analysis"

US_NAMES = {
    "AAPL": "Apple",
    "MSFT": "Microsoft",
    "NVDA": "NVIDIA",
    "GOOGL": "Alphabet",
    "AMZN": "Amazon",
    "META": "Meta",
    "TSLA": "Tesla",
    "AVGO": "Broadcom",
    "TSM": "台積電 ADR",
    "SPY": "SPDR S&P 500",
    "QQQ": "Invesco QQQ",
    "VOO": "Vanguard S&P 500",
    "VT": "Vanguard Total World",
    "VTI": "Vanguard Total Stock",
    "BND": "Vanguard Total Bond",
    "TLT": "iShares 20+ Year Treasury",
}


def now_iso():
    return datetime.now(TPE).strftime("%Y-%m-%dT%H:%M:%S+08:00")


def write_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


def get(url, retries=3, timeout=60):
    last = None
    for i in range(retries):
        req = Request(url, headers={"User-Agent": UA, "Accept": "application/json,text/csv,*/*"})
        try:
            with urlopen(req, timeout=timeout, context=CTX) as r:
                return r.read(), r.headers.get("Content-Type", ""), r.status
        except Exception as e:
            last = e
            time.sleep(0.8 * (i + 1))
    raise last if last else RuntimeError(f"GET failed: {url}")


def parse_body(raw):
    """TWSE rwd may return CSV even when response=json is requested."""
    import csv, io
    text = raw.decode("utf-8-sig", errors="replace").strip()
    if not text:
        raise ValueError("empty body")
    if text[0] in "[{":
        return json.loads(text)
    first = text.split("\n", 1)[0]
    if any(k in first for k in ("證券代號", "證券名稱", "日期", "Code", "ClosingPrice")):
        return list(csv.DictReader(io.StringIO(text)))
    return json.loads(text)


def get_json(url, retries=3, timeout=60):
    raw, ctype, _ = get(url, retries=retries, timeout=timeout)
    return parse_body(raw)


def to_float(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    t = str(v).replace(",", "").replace("%", "").strip()
    if not t or t in {"--", "---", "----", "N/A", "n/a", "-", "無", "----"}:
        return None
    try:
        return float(t)
    except ValueError:
        return None


def compact(v):
    x = to_float(v)
    if x is None:
        return None
    x = round(x, 4)
    if x == int(x):
        return int(x)
    return x


def roc_to_iso(s):
    if s is None:
        return None
    t = str(s).strip().replace("/", "").replace("-", "")
    if not t:
        return None
    try:
        if len(t) == 7 and t.isdigit():  # 1151123
            y = int(t[:3]) + 1911
            return f"{y:04d}-{t[3:5]}-{t[5:7]}"
        if len(t) == 8 and t.isdigit():  # 20261123
            return f"{t[:4]}-{t[4:6]}-{t[6:8]}"
        if len(t) == 6 and t.isdigit():  # 115123 leftover? skip
            return None
    except ValueError:
        return None
    if len(str(s).strip()) >= 8 and "-" in str(s):
        return str(s).strip()[:10]
    return None


def iso_key(s):
    """Sortable date string; empty sorts last among missing."""
    return s or ""


def load_us_symbols():
    if not SYM_PATH.exists():
        return list(US_NAMES)
    out = []
    seen = set()
    for line in SYM_PATH.read_text(encoding="utf-8").splitlines():
        s = line.split("#", 1)[0].strip().upper()
        if not s or not s.replace(".", "").replace("-", "").isalnum():
            continue
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


# ---------- TWSE / TPEx ----------

def rows_from_twse_openapi(data):
    if not isinstance(data, list):
        return []
    return [r for r in data if isinstance(r, dict)]


def rows_from_twse_rwd(data):
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if not isinstance(data, dict):
        return []
    tables = data.get("tables") or data.get("data") or []
    # Some rwd payloads are {stat, date, tables:[{data, fields}]} or {stat, data:[[...]], fields}
    if tables and isinstance(tables, list) and tables and isinstance(tables[0], dict) and "data" in tables[0]:
        fields = tables[0].get("fields") or []
        rows = []
        for row in tables[0].get("data") or []:
            if isinstance(row, dict):
                rows.append(row)
            elif isinstance(row, list) and fields:
                rows.append({fields[i]: row[i] for i in range(min(len(fields), len(row)))})
        date = data.get("date") or tables[0].get("date")
        if date:
            for r in rows:
                r.setdefault("Date", date)
        return rows
    fields = data.get("fields") or []
    rows = []
    for row in data.get("data") or []:
        if isinstance(row, dict):
            rows.append(row)
        elif isinstance(row, list) and fields:
            rows.append({fields[i]: row[i] for i in range(min(len(fields), len(row)))})
    date = data.get("date")
    if date:
        for r in rows:
            r.setdefault("Date", date)
    return rows


def pick_newer(a_rows, b_rows, date_key="Date"):
    def max_date(rows):
        ds = []
        for r in rows:
            raw = r.get(date_key) or r.get("date") or r.get("Date") or r.get("日期")
            ds.append(roc_to_iso(raw))
        ds = [d for d in ds if d]
        return max(ds) if ds else ""
    if not a_rows:
        return b_rows
    if not b_rows:
        return a_rows
    return b_rows if max_date(b_rows) > max_date(a_rows) else a_rows


def fetch_twse_day():
    notes = []
    rows = []
    try:
        rows = rows_from_twse_openapi(get_json(TWSE_DAY, timeout=90))
        notes.append(f"openapi day n={len(rows)}")
    except Exception as e:
        notes.append(f"openapi day: {e}")
    try:
        rwd = rows_from_twse_rwd(get_json(TWSE_DAY_RWD, timeout=90))
        notes.append(f"rwd day n={len(rwd)}")
        rows = pick_newer(rows, rwd)
    except Exception as e:
        notes.append(f"rwd day: {e}")
    return rows, notes


def fetch_twse_pe():
    notes = []
    rows = []
    try:
        o = rows_from_twse_openapi(get_json(TWSE_PE, timeout=90))
        notes.append(f"openapi pe n={len(o)}")
        rows.extend(o)
    except Exception as e:
        notes.append(f"openapi pe: {e}")
    try:
        rwd = rows_from_twse_rwd(get_json(TWSE_PE_RWD, timeout=90))
        notes.append(f"rwd pe n={len(rwd)}")
        rows.extend(rwd)
    except Exception as e:
        notes.append(f"rwd pe: {e}")
    return rows, notes


def fetch_tpex_day():
    data = get_json(TPEX_DAY, timeout=90)
    if not isinstance(data, list):
        raise RuntimeError("tpex close quotes not a list")
    return data


def fetch_tpex_pe():
    data = get_json(TPEX_PE, timeout=90)
    if not isinstance(data, list):
        raise RuntimeError("tpex pe not a list")
    return data


def build_tw():
    notes = []
    out = {}
    dates = {"TWSE": None, "TPEx": None}

    day_rows, dnotes = fetch_twse_day()
    notes.extend(dnotes)
    pe_rows, pnotes = fetch_twse_pe()
    notes.extend(pnotes)
    pe_map = {}
    for r in pe_rows:
        code = str(r.get("Code") or r.get("證券代號") or r.get("股票代號") or r.get("代號") or r.get("code") or "").strip()
        pe = compact(r.get("PEratio") or r.get("本益比") or r.get("pe") or r.get("本益比(倍)"))
        if code and pe is not None:
            pe_map[code] = pe
    for r in day_rows:
        code = str(r.get("Code") or r.get("證券代號") or r.get("股票代號") or r.get("代號") or r.get("code") or "").strip()
        if not code:
            continue
        close = compact(r.get("ClosingPrice") or r.get("收盤價") or r.get("close"))
        if close is None:
            continue
        name = str(r.get("Name") or r.get("證券名稱") or r.get("股票名稱") or r.get("name") or "").strip()
        dt = roc_to_iso(r.get("Date") or r.get("date") or r.get("日期"))
        chg = compact(r.get("Change") or r.get("漲跌價差") or r.get("change"))
        rec = {"name": name, "close": close, "date": dt, "pe": pe_map.get(code), "market": "TWSE"}
        if chg is not None:
            rec["chg"] = chg
        out[code] = rec
        if dt and (dates["TWSE"] is None or dt > dates["TWSE"]):
            dates["TWSE"] = dt

    try:
        tday = fetch_tpex_day()
        notes.append(f"tpex day n={len(tday)}")
    except Exception as e:
        tday = []
        notes.append(f"tpex day: {e}")
    try:
        tpe = fetch_tpex_pe()
        notes.append(f"tpex pe n={len(tpe)}")
    except Exception as e:
        tpe = []
        notes.append(f"tpex pe: {e}")
    tpe_map = {}
    for r in tpe:
        code = str(r.get("SecuritiesCompanyCode") or r.get("Code") or "").strip()
        pe = compact(r.get("PriceEarningRatio") or r.get("PEratio") or r.get("本益比"))
        if code:
            tpe_map[code] = pe
    for r in tday:
        code = str(r.get("SecuritiesCompanyCode") or r.get("Code") or "").strip()
        if not code:
            continue
        close = compact(r.get("Close") or r.get("ClosingPrice") or r.get("收盤價"))
        if close is None:
            continue
        name = str(r.get("CompanyName") or r.get("Name") or "").strip()
        dt = roc_to_iso(r.get("Date") or r.get("date"))
        chg = compact(r.get("Change") or r.get("change"))
        rec = {"name": name, "close": close, "date": dt, "pe": tpe_map.get(code), "market": "TPEx"}
        if chg is not None:
            rec["chg"] = chg
        # Prefer not to overwrite a TWSE name/code if collision — unique in practice.
        if code in out and out[code].get("market") == "TWSE":
            continue
        out[code] = rec
        if dt and (dates["TPEx"] is None or dt > dates["TPEx"]):
            dates["TPEx"] = dt

    twse_n = sum(1 for v in out.values() if v.get("market") == "TWSE")
    tpex_n = sum(1 for v in out.values() if v.get("market") == "TPEx")
    return out, {"twse": twse_n, "tpex": tpex_n, "n": len(out), "dates": dates, "notes": notes}


# ---------- US ----------

def parse_stooq_csv(text, symbol):
    lines = [ln.strip() for ln in text.replace("\r\n", "\n").split("\n") if ln.strip()]
    if len(lines) < 2:
        return None
    # header Symbol,Date,Time,Open,High,Low,Close,Volume
    row = lines[1].split(",")
    if len(row) < 7:
        return None
    # N/D means missing
    if "N/D" in row or row[1] in {"N/D", ""}:
        return None
    close = compact(row[6])
    if close is None:
        return None
    date = row[1].strip()
    if len(date) == 8 and date.isdigit():
        date = f"{date[:4]}-{date[4:6]}-{date[6:8]}"
    return {"name": US_NAMES.get(symbol, symbol), "close": close, "date": date, "pe": None, "market": "US", "source": "stooq"}


def fetch_stooq(symbol):
    s = symbol.lower()
    url = f"https://stooq.com/q/l/?s={s}.us&f=sd2t2ohlcv&h&e=csv"
    raw, _, _ = get(url, retries=1, timeout=8)
    return parse_stooq_csv(raw.decode("utf-8", errors="replace"), symbol)


def stooq_available():
    """One short probe so a TLS-blocked network does not stall every symbol."""
    try:
        rec = fetch_stooq("AAPL")
        return bool(rec)
    except Exception as e:
        print(f"Stooq unavailable, using Yahoo: {e}", flush=True)
        return False


def fetch_yahoo(symbol):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=10d&interval=1d"
    data = get_json(url, retries=3, timeout=20)
    err = (data.get("chart") or {}).get("error")
    results = (data.get("chart") or {}).get("result") or []
    if err or not results:
        raise RuntimeError(err or "empty yahoo chart")
    res = results[0]
    meta = res.get("meta") or {}
    ts = res.get("timestamp") or []
    closes = ((res.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
    pairs = [(t, c) for t, c in zip(ts, closes) if c is not None]
    close = None
    date = None
    if pairs:
        t, c = pairs[-1]
        close = compact(c)
        # Exchange date in US Eastern-ish via gmtoffset
        off = int(meta.get("gmtoffset") or 0)
        date = datetime.fromtimestamp(t + off, timezone.utc).strftime("%Y-%m-%d")
    if close is None:
        close = compact(meta.get("regularMarketPrice"))
        t = meta.get("regularMarketTime")
        if t:
            off = int(meta.get("gmtoffset") or 0)
            date = datetime.fromtimestamp(int(t) + off, timezone.utc).strftime("%Y-%m-%d")
    if close is None:
        raise RuntimeError("no yahoo close")
    name = (meta.get("shortName") or meta.get("longName") or US_NAMES.get(symbol) or symbol).strip()
    rec = {"name": name, "close": close, "date": date, "pe": None, "market": "US", "source": "yahoo"}
    prev = compact(meta.get("chartPreviousClose") or meta.get("previousClose"))
    if prev is not None:
        rec["chg"] = compact(close - prev)
    return rec


def build_us():
    symbols = load_us_symbols()
    out = {}
    fail = []
    sources = {"stooq": 0, "yahoo": 0}
    use_stooq = stooq_available()
    for i, sym in enumerate(symbols):
        rec = None
        err_s = err_y = None
        if use_stooq:
            try:
                rec = fetch_stooq(sym)
            except Exception as e:
                err_s = str(e)
        else:
            err_s = "skipped probe failed"
        if rec:
            sources["stooq"] += 1
        else:
            try:
                rec = fetch_yahoo(sym)
                sources["yahoo"] += 1
            except Exception as e:
                err_y = str(e)
        if rec:
            out[sym] = rec
        else:
            fail.append({"symbol": sym, "stooq": err_s or "empty", "yahoo": err_y})
        # polite pause, especially for Yahoo
        time.sleep(0.35)
    src = "stooq" if sources["stooq"] and not sources["yahoo"] else (
        "yahoo" if sources["yahoo"] and not sources["stooq"] else
        ("mixed" if sources["stooq"] or sources["yahoo"] else None)
    )
    return out, {
        "n": len(out),
        "ok": len(out),
        "attempted": len(symbols),
        "fail": fail,
        "source": src,
        "sources": sources,
    }


def main():
    t0 = time.time()
    tw, tw_meta = {}, {"n": 0, "twse": 0, "tpex": 0, "dates": {}, "notes": [], "error": None}
    us, us_meta = {}, {"n": 0, "ok": 0, "fail": [], "source": None}
    tw_ok = us_ok = False

    try:
        tw, tw_meta = build_tw()
        tw_ok = tw_meta.get("n", 0) > 0
    except Exception as e:
        tw_meta["error"] = str(e)
        print(f"TW fetch failed: {e}", file=sys.stderr)

    try:
        us, us_meta = build_us()
        us_ok = us_meta.get("n", 0) > 0
    except Exception as e:
        us_meta["error"] = str(e)
        print(f"US fetch failed: {e}", file=sys.stderr)

    # Overwrite a file only on success so a failed run keeps yesterday's JSON.
    if tw_ok:
        write_json(OUT / "tw.json", tw)
    else:
        print("keeping existing tw.json (fetch unsuccessful)", file=sys.stderr)
    if us_ok:
        write_json(OUT / "us.json", us)
    else:
        print("keeping existing us.json (fetch unsuccessful)", file=sys.stderr)

    elapsed = round(time.time() - t0, 1)
    meta = {
        "generatedAt": now_iso(),
        "tw": {k: tw_meta[k] for k in tw_meta if k != "error" or tw_meta[k]},
        "us": {k: us_meta[k] for k in us_meta if k != "error" or us_meta[k]},
        "elapsedSec": elapsed,
        "wrote": {"tw": tw_ok, "us": us_ok},
    }
    # Always write prices/meta.json so the status of this run is visible.
    write_json(OUT / "meta.json", meta)
    print(json.dumps({
        "generatedAt": meta["generatedAt"],
        "tw": {k: tw_meta.get(k) for k in ("n", "twse", "tpex", "dates", "notes") if k in tw_meta},
        "us": {k: us_meta.get(k) for k in ("n", "ok", "attempted", "source", "sources") if k in us_meta},
        "usFail": us_meta.get("fail") or [],
        "elapsedSec": elapsed,
        "wrote": meta["wrote"],
    }, ensure_ascii=False, indent=2))
    if not tw_ok and not us_ok:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
