"""백테스트용 과거 데이터 수집: 네이버 일봉(전 종목) + 야후 5분봉(60일)/2분봉(32일)."""
from __future__ import annotations
import json, sys, time, gzip
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import requests
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from danta.naver import Naver, is_common

C = Path(__file__).resolve().parent.parent / ".cache" / "hist"
C.mkdir(parents=True, exist_ok=True)
KST = timezone(timedelta(hours=9))
nv = Naver(workers=16)
today = datetime.now(KST).strftime("%Y%m%d")

# 1) 종목 목록
p = C / "stocks.json"
if not p.exists():
    p.write_text(json.dumps(nv.all_stocks(), ensure_ascii=False))
stocks = {s["code"]: s for s in json.loads(p.read_text())}
print("종목", len(stocks), flush=True)

# 2) 일봉 (약 140 캘린더일)
p = C / "daily.json.gz"
if not p.exists():
    start = (datetime.now(KST) - timedelta(days=140)).strftime("%Y%m%d")
    res = nv.pmap(lambda c: (c, nv.days(c, start, today)), list(stocks))
    with gzip.open(p, "wt") as f:
        json.dump(dict(res), f)
with gzip.open(p, "rt") as f:
    daily = json.load(f)
print("일봉", len(daily), flush=True)

# 3) 날짜별 거래대금 상위 300 → 합집합
cal = [b["d"] for b in daily["005930"]]
test_days = cal[-60:]
need = set()
uni = {}
for d in test_days:
    rows = []
    for code, bars in daily.items():
        for i, b in enumerate(bars):
            if b["d"] == d and i >= 21:
                rows.append((b["v"] * (b["h"] + b["l"] + b["c"]) / 3, code))
                break
    rows.sort(reverse=True)
    uni[d] = [c for _, c in rows[:300]]
    need.update(uni[d])
(C / "universe.json").write_text(json.dumps(uni))
print("테스트일", len(test_days), "필요 종목", len(need), flush=True)

# 4) 야후 분봉
S = requests.Session(); S.headers["User-Agent"] = "Mozilla/5.0"
S.mount("https://", requests.adapters.HTTPAdapter(pool_maxsize=16))
def yahoo(code, iv, rng):
    sym = f"{code}.{stocks[code].get('mkt','KS')}"
    for i in range(4):
        try:
            r = S.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval={iv}&range={rng}", timeout=15)
            if r.status_code == 429:
                time.sleep(3 + i * 3); continue
            if r.status_code != 200:
                return None
            res = r.json()["chart"]["result"][0]
            ts = res.get("timestamp") or []
            q = res["indicators"]["quote"][0]
            out = {}
            for k, t in enumerate(ts):
                if q["close"][k] is None:
                    continue
                dtk = datetime.fromtimestamp(t, KST)
                out.setdefault(dtk.strftime("%Y%m%d"), []).append(
                    [dtk.strftime("%H%M"), q["open"][k], q["high"][k], q["low"][k], q["close"][k], q["volume"][k] or 0])
            return out
        except Exception:
            time.sleep(1 + i)
    return None
for iv, rng in (("5m", "60d"), ("2m", "60d")):
    p = C / f"y{iv}.json.gz"
    if p.exists():
        continue
    codes = sorted(need)
    t0 = time.time()
    with ThreadPoolExecutor(8) as ex:
        res = list(ex.map(lambda c: (c, yahoo(c, iv, rng)), codes))
    ok = {c: v for c, v in res if v}
    with gzip.open(p, "wt") as f:
        json.dump(ok, f)
    print(iv, "수집", len(ok), "/", len(codes), f"{time.time()-t0:.0f}s", flush=True)
