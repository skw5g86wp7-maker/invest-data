#!/usr/bin/env python3
"""Fetch NAV history and holdings from invest.fubonlife.com.tw and write
static JSON under docs/data/. Overwrites a file only after a successful
fetch for that code; never deletes existing files. No deploy step.

Usage:
  python3 scripts/fetch_fubon.py
  python3 scripts/fetch_fubon.py --codes F0892,NGB12
  python3 scripts/fetch_fubon.py --rebuild-latest
"""
from __future__ import annotations

import argparse, csv, json, ssl, sys, time, threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
CSV_PATH = ROOT / "targets.csv"
OUT = ROOT / "docs" / "data"
API = "https://invest.fubonlife.com.tw/api"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
KIND = {"ETF": "etf", "基金": "fund", "委託帳戶": "discretionary"}
NAV_EP = {"etf": "etf/getEtfNavs", "fund": "fund/getFundNavs", "discretionary": "discretionary/getDiscretionaryNavs"}
HOLD_EP = {
    "etf": [
        ("holdings", "etf/getEtfHoldings"),
        ("areas", "etf/getEtfHoldingAreas"),
        ("industry", "etf/getEtfHoldingIndustry"),
    ],
    "fund": [
        ("holdings", "fund/getFundHoldings"),
        ("areas", "fund/getFundHoldingAreas"),
        ("industry", "fund/getFundHoldingIndustry"),
        ("sector", "fund/getFundHoldingSector"),
    ],
    "discretionary": [],
}
CTX = ssl.create_default_context()
TPE = timezone(timedelta(hours=8))


class Rate:
    def __init__(self, rps=2.2):
        self.gap = 1.0 / rps
        self.lock = threading.Lock()
        self.t = 0.0

    def wait(self):
        with self.lock:
            now = time.monotonic()
            sl = self.t + self.gap - now
            if sl > 0:
                time.sleep(sl)
            self.t = time.monotonic()


RATE = Rate(2.2)


def today():
    return datetime.now(TPE).date()


def fmt(d: date) -> str:
    return d.strftime("%Y/%m/%d")


def post(path, body, retries=3):
    data = json.dumps(body, ensure_ascii=False).encode()
    req = Request(API + "/" + path, data=data, method="POST", headers={
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": UA,
        "Origin": "https://invest.fubonlife.com.tw",
        "Referer": "https://invest.fubonlife.com.tw/funds/overview",
    })
    last = None
    for i in range(retries):
        RATE.wait()
        try:
            with urlopen(req, timeout=30, context=CTX) as r:
                raw = r.read()
            j = json.loads(raw.decode())
            if j.get("returnCode") != "0000":
                return None, j.get("message") or j.get("returnCode") or "bad returnCode"
            return j, None
        except Exception as e:
            last = e
            time.sleep(0.8 * (i + 1))
    return None, str(last)


def num_ratio(s):
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    t = str(s).replace("%", "").replace(",", "").strip()
    try:
        return float(t)
    except ValueError:
        return None


def compact_nav(v):
    try:
        x = round(float(v), 4)
    except (TypeError, ValueError):
        return None
    if x == int(x):
        return int(x)
    return x


def latest_group(rows):
    if not rows:
        return [], None
    dates = [r.get("date") for r in rows if r.get("date")]
    latest = max(dates) if dates else None
    items = [r for r in rows if not latest or r.get("date") == latest]
    return items, latest


def pack_alloc(rows):
    items, dt = latest_group(rows or [])
    out = []
    for r in items:
        w = num_ratio(r.get("ratio"))
        name = str(r.get("name") or "").strip()
        if not name or w is None:
            continue
        cn = compact_nav(w)
        out.append({"name": name, "w": cn if cn is not None else w})
    out.sort(key=lambda x: -x["w"])
    return out, dt


def load_targets(only=None):
    only = set(only) if only else None
    seen = {}
    with CSV_PATH.open(encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            code = (r.get("標的代碼") or "").split("/")[0].strip()
            typ = (r.get("類型") or "").strip()
            name = (r.get("標的名稱") or "").strip()
            kind = KIND.get(typ)
            if not code or not kind or not (2 <= len(code) <= 12) or not code.isalnum():
                continue
            if only and code not in only:
                continue
            if code not in seen:
                seen[code] = {"code": code, "kind": kind, "type": typ, "name": name}
    return list(seen.values())


def fetch_nav(item, start, end):
    j, err = post(NAV_EP[item["kind"]], {"assetFbCode": item["code"], "startDate": start, "endDate": end})
    if not j:
        return None, err
    rows = j.get("data") or []
    pts = []
    for p in rows:
        if not isinstance(p, dict):
            continue
        d, n = p.get("date"), compact_nav(p.get("nav"))
        if d and n is not None:
            pts.append([d, n])
    pts.sort(key=lambda x: x[0])
    uniq = {}
    for d, n in pts:
        uniq[d] = n
    pts = [[d, uniq[d]] for d in sorted(uniq)]
    if len(pts) < 2:
        return None, "too few NAV points"
    return {"asOf": pts[-1][0], "n": len(pts), "d": pts}, None


def fetch_hold(item, start, end):
    kind = item["kind"]
    out = {"code": item["code"], "kind": kind, "holdings": [], "areas": [], "industry": [], "sector": []}
    if kind == "discretionary":
        out["note"] = "全權委託帳戶在富邦人壽投資型專區未提供持股明細"
        return out, None
    body = {"assetFbCode": item["code"], "startDate": start, "endDate": end}
    dates = {}
    errors = []
    ok = 0
    for key, path in HOLD_EP[kind]:
        j, err = post(path, body)
        if not j:
            errors.append(f"{key}:{err}")
            continue
        ok += 1
        packed, dt = pack_alloc(j.get("data") or [])
        out[key] = packed
        if dt:
            dates[key] = dt
    if dates:
        out["asOf"] = max(dates.values())
        out["dates"] = dates
    if ok == 0:
        return None, "; ".join(errors) if errors else "no holdings response"
    if not out["holdings"] and not out["areas"] and not out["industry"] and not out["sector"]:
        out["note"] = "富邦人壽投資型專區目前沒有此標的的持股或配置資料"
        if errors:
            out["error"] = "; ".join(errors)
    return out, None


def write_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


def dir_size(p: Path) -> int:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) if p.exists() else 0


def write_latest(nav_dir: Path | None = None):
    nav_dir = nav_dir or (OUT / "nav")
    latest = {}
    if nav_dir.exists():
        for f in sorted(nav_dir.glob("*.json")):
            try:
                obj = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            pts = obj.get("d") or []
            if not pts:
                continue
            d, n = pts[-1][0], pts[-1][1]
            latest[f.stem] = {"nav": n, "date": d}
    payload = {
        "generatedAt": datetime.now(TPE).strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "n": len(latest),
        "nav": latest,
    }
    write_json(OUT / "latest.json", payload)
    return len(latest)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", default="", help="comma-separated codes for a partial refresh")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--rebuild-latest", action="store_true", help="rebuild latest.json from existing nav files only")
    args = ap.parse_args()
    if args.rebuild_latest:
        n = write_latest()
        print(f"rebuilt latest.json with {n} codes")
        return 0

    only = [c.strip() for c in args.codes.split(",") if c.strip()] or None
    if not CSV_PATH.exists():
        print(f"missing {CSV_PATH}", file=sys.stderr)
        return 2
    targets = load_targets(only)
    if not targets:
        print("no targets", file=sys.stderr)
        return 2
    end = today()
    nav_start = end - timedelta(days=365 * 3 + 7)
    hold_start = end - timedelta(days=365)
    nav_dir, hold_dir = OUT / "nav", OUT / "hold"
    # Never wipe existing files; only overwrite a code after a successful fetch.
    nav_dir.mkdir(parents=True, exist_ok=True)
    hold_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    stats = {"navOk": 0, "navFail": 0, "holdHas": 0, "holdEmpty": 0, "holdFail": 0, "fail": []}
    lock = threading.Lock()
    total = len(targets)
    done = [0]

    def work(item):
        nav, nerr = fetch_nav(item, fmt(nav_start), fmt(end))
        hold, herr = fetch_hold(item, fmt(hold_start), fmt(end))
        with lock:
            done[0] += 1
            i = done[0]
            if nav:
                write_json(nav_dir / f"{item['code']}.json", nav)
                stats["navOk"] += 1
            else:
                stats["navFail"] += 1
                stats["fail"].append({"code": item["code"], "nav": nerr})
            if hold is None:
                stats["holdFail"] += 1
                stats["fail"].append({"code": item["code"], "hold": herr})
            else:
                write_json(hold_dir / f"{item['code']}.json", hold)
                if hold.get("holdings"):
                    stats["holdHas"] += 1
                else:
                    stats["holdEmpty"] += 1
            if i % 10 == 0 or i == total:
                elapsed = time.time() - t0
                print(f"[{i}/{total}] {elapsed:.0f}s  navOk={stats['navOk']} holdHas={stats['holdHas']} fail={stats['navFail']+stats['holdFail']}", flush=True)
        return item["code"]

    print(f"Refreshing {total} targets, workers={args.workers}, polite delay ~{RATE.gap:.2f}s/req", flush=True)
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        futs = [ex.submit(work, it) for it in targets]
        for f in as_completed(futs):
            try:
                f.result()
            except Exception as e:
                with lock:
                    stats["fail"].append({"error": str(e)})

    elapsed = time.time() - t0
    nav_bytes = dir_size(nav_dir)
    hold_bytes = dir_size(hold_dir)
    now = datetime.now(TPE).strftime("%Y-%m-%dT%H:%M:%S+08:00")
    meta = {
        "generatedAt": now,
        "targets": total,
        "navOk": stats["navOk"],
        "navFail": stats["navFail"],
        "holdingsWithStocks": stats["holdHas"],
        "holdingsEmpty": stats["holdEmpty"],
        "navBytes": nav_bytes,
        "holdBytes": hold_bytes,
        "totalBytes": nav_bytes + hold_bytes,
        "elapsedSec": round(elapsed, 1),
        "source": "https://invest.fubonlife.com.tw/api",
        "fail": stats["fail"][:50],
    }
    write_json(OUT / "meta.json", meta)
    n_latest = write_latest(nav_dir)
    print(json.dumps({k: meta[k] for k in meta if k != "fail"}, ensure_ascii=False, indent=2))
    print(f"latest.json codes={n_latest}")
    if stats["fail"]:
        print("failures (first 20):", file=sys.stderr)
        for row in stats["fail"][:20]:
            print(" ", row, file=sys.stderr)
    return 0 if stats["navFail"] < total else 1


if __name__ == "__main__":
    sys.exit(main())
