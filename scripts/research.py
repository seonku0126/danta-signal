"""전략 비교 백테스트 (매일 반드시 1종목).

데이터: .cache/hist (fetch_hist.py). 야후 분봉은 장 시작 동시호가 거래량이 빠져 있다.
진입: 판단 시점 직후 봉의 시가 (미래 정보 사용 안 함).
같은 봉에서 손절·목표 둘 다 닿으면 손절로 처리. 비용 0.35% 차감.
"""
from __future__ import annotations
import gzip, json, sys, itertools, statistics as st
from pathlib import Path

C = Path(__file__).resolve().parent.parent / ".cache" / "hist"
COST = 0.35
EXCL = ("스팩", "리츠")

stocks = {s["code"]: s for s in json.loads((C / "stocks.json").read_text())}
with gzip.open(C / "daily.json.gz", "rt") as f:
    daily = json.load(f)
uni = json.loads((C / "universe_prev.json").read_text())   # 전일 거래대금 상위 400 (사후정보 없음), 종목코드순


def load(iv):
    with gzip.open(C / f"y{iv}.json.gz", "rt") as f:
        return json.load(f)


def prev_info(code, day):
    bars = daily.get(code) or []
    for i, b in enumerate(bars):
        if b["d"] == day:
            if i < 21:
                return None
            prev = bars[i - 1]["c"]
            avg = sum(x["v"] * (x["h"] + x["l"] + x["c"]) / 3 for x in bars[i - 20:i]) / 20
            hi60 = max(x["h"] for x in bars[max(0, i - 60):i])
            return prev, avg, hi60
    return None


def feats(code, day, bars, k):
    """개장 후 k개 봉이 끝난 시점의 지표. bars: [t,o,h,l,c,v]"""
    info = prev_info(code, day)
    if not info or len(bars) <= k:
        return None
    prev, avg, hi60 = info
    done = bars[:k]
    o = done[0][1]
    price = done[-1][4]
    hi = max(b[2] for b in done); lo = min(b[3] for b in done)
    pv = sum((b[2] + b[3] + b[4]) / 3 * b[5] for b in done)
    vol = sum(b[5] for b in done)
    up = sum(b[5] for b in done if b[4] > b[1]); dn = sum(b[5] for b in done if b[4] < b[1])
    s = stocks.get(code, {})
    if not o or not prev or price < 1000 or s.get("mcap", 0) < 5e10 or any(w in s.get("name", "") for w in EXCL):
        return None
    vwap = pv / vol if vol else price
    return dict(code=code, name=s.get("name", ""), prev=prev, o=o, price=price, hi=hi, lo=lo,
                flu=(price / prev - 1) * 100, gap=(o / prev - 1) * 100, value=pv,
                rvol=pv / avg if avg else 0, vwap=vwap, strength=(up / dn * 100) if dn else (300 if up else 100),
                pull=(1 - price / hi) * 100 if hi else 0, near_hi=price / hi60 if hi60 else 0,
                pos=(price - lo) / (hi - lo) if hi > lo else 1)


# ── 선정 규칙: 후보 리스트에서 1종목 ─────────────────────────────
def base_ok(f, cap=15):
    return 0 < f["flu"] <= cap and f["price"] >= f["vwap"] and f["price"] >= f["o"]


def sel_current(fs):
    """현재 봇 방식(점수식) + 매일 1종목 보장용 완화."""
    ok = [f for f in fs if 3 <= f["flu"] <= 15 and f["gap"] <= 10 and f["pull"] <= 3 and f["price"] >= f["vwap"]
          and f["price"] >= f["o"] and f["strength"] >= 110]
    pool = ok or [f for f in fs if base_ok(f)] or fs
    if not pool:
        return None
    vr = sorted(pool, key=lambda f: -f["value"])
    rank = {f["code"]: i for i, f in enumerate(vr)}
    def score(f):
        return (0.30 * min(f["rvol"] / 0.3, 1) + 0.20 * min(max((f["strength"] - 100) / 100, 0), 1)
                + 0.15 * f["pos"] + 0.15 * max(1 - rank[f["code"]] / 50, 0)
                + 0.10 * min(max((f["near_hi"] - .85) / .15, 0), 1) + 0.10 * max(1 - abs(f["flu"] - 7.5) / 7.5, 0))
    return max(pool, key=score)


def sel_rvol(fs, cap=15):
    pool = [f for f in fs if base_ok(f, cap) and f["flu"] >= 2] or [f for f in fs if base_ok(f, 30)] or fs
    return max(pool, key=lambda f: f["rvol"]) if pool else None


def sel_value(fs, cap=15):
    pool = [f for f in fs if base_ok(f, cap) and f["flu"] >= 2] or [f for f in fs if base_ok(f, 30)] or fs
    return max(pool, key=lambda f: f["value"]) if pool else None


def sel_mom(fs, cap=15):
    pool = [f for f in fs if base_ok(f, cap) and f["rvol"] >= 0.03] or [f for f in fs if base_ok(f, 30)] or fs
    return max(pool, key=lambda f: f["flu"]) if pool else None


def sel_strength(fs, cap=15):
    pool = [f for f in fs if base_ok(f, cap) and f["flu"] >= 2 and f["rvol"] >= 0.03] or [f for f in fs if base_ok(f, 30)] or fs
    return max(pool, key=lambda f: (f["strength"], f["rvol"])) if pool else None


def sel_combo(fs, cap=15):
    """거래대금배수·매수세·당일위치 순위합 (단순·과최적화 덜한 형태)."""
    pool = [f for f in fs if base_ok(f, cap) and f["flu"] >= 2 and f["pull"] <= 3] or [f for f in fs if base_ok(f, 30)] or fs
    if not pool:
        return None
    def rk(key):
        o = sorted(pool, key=lambda f: -f[key]); return {f["code"]: i for i, f in enumerate(o)}
    a, b, c = rk("rvol"), rk("strength"), rk("pos")
    return min(pool, key=lambda f: a[f["code"]] + b[f["code"]] + 0.5 * c[f["code"]])


def sel_nearhi(fs, cap=15):
    """신고가 근접 + 거래대금 배수."""
    pool = [f for f in fs if base_ok(f, cap) and f["flu"] >= 2 and f["near_hi"] >= 0.95] or [f for f in fs if base_ok(f, cap)] or fs
    return max(pool, key=lambda f: f["rvol"]) if pool else None


SELECTORS = {
    "현재방식": sel_current,
    "거래대금배수": sel_rvol, "거래대금배수(상한10%)": lambda fs: sel_rvol(fs, 10), "거래대금배수(상한8%)": lambda fs: sel_rvol(fs, 8),
    "거래대금1위": sel_value, "거래대금1위(상한10%)": lambda fs: sel_value(fs, 10),
    "상승률1위": sel_mom, "상승률1위(상한10%)": lambda fs: sel_mom(fs, 10),
    "매수세1위": sel_strength, "순위합": sel_combo, "순위합(상한10%)": lambda fs: sel_combo(fs, 10),
    "신고가근접": sel_nearhi,
}


# ── 청산 규칙 ─────────────────────────────────────────────
def run_exit(rule, entry, f, rest, last_t="1520"):
    """rest: 진입 봉부터의 봉들. (청산가, 사유, 최대%, 최소%) 반환."""
    mx = mn = entry
    if rule[0] == "R":           # 현재: VWAP 기반 손절 2~4%, 목표 = 손절폭 × R
        sp = min(max((entry - f["vwap"]) / entry * 100 + 0.5, 2), 4)
        stop, tgt = entry * (1 - sp / 100), entry * (1 + sp * rule[1] / 100)
    elif rule[0] == "F":
        stop, tgt = entry * (1 - rule[2] / 100), entry * (1 + rule[1] / 100)
    elif rule[0] == "T":         # 트레일링: -s% 손절, +a% 이후 본전, 이후 최고가 대비 -s%
        stop, tgt = entry * (1 - rule[1] / 100), None
    else:                         # CLOSE / TIME
        stop, tgt = None, None
    n = 0
    for b in rest:
        t, o, h, l, c, v = b
        if t >= last_t:
            return o, "장마감", mx, mn
        if rule[0] == "M" and n >= rule[1]:
            return o, "시간청산", mx, mn
        if stop and l <= stop:
            return min(stop, o), "손절", mx, min(mn, l)
        if tgt and h >= tgt:
            return max(tgt, o) if o >= tgt else tgt, "목표", max(mx, h), mn
        mx, mn = max(mx, h), min(mn, l)
        if rule[0] == "T":
            if mx >= entry * (1 + rule[2] / 100):
                stop = max(stop, entry * 1.003, mx * (1 - rule[1] / 100))
        n += 1
    return (rest[-1][4] if rest else entry), "장마감", mx, mn


EXITS = {
    "손절폭×2(현재)": ("R", 2), "손절폭×1.5": ("R", 1.5), "손절폭×1": ("R", 1),
    "+3/-2": ("F", 3, 2), "+4/-2": ("F", 4, 2), "+5/-3": ("F", 5, 3), "+3/-3": ("F", 3, 3), "+2/-2": ("F", 2, 2),
    "+6/-3": ("F", 6, 3), "+8/-4": ("F", 8, 4),
    "트레일링2%": ("T", 2, 2), "트레일링3%": ("T", 3, 3),
    "30분보유": ("M", 6), "60분보유": ("M", 12), "장마감까지": ("C",),
}


_FS, _PICK = {}, {}


def candidates(iv, k, day):
    key = (iv, k, day)
    if key not in _FS:
        data, fs = DATA[iv], []
        for code in uni[day]:
            bars = (data.get(code) or {}).get(day)
            if bars:
                f = feats(code, day, bars, k)
                if f:
                    fs.append(f)
        _FS[key] = fs
    return _FS[key]


def backtest(iv, k, sel_name, exit_name, days=None, detail=False):
    data = DATA[iv]
    out = []
    for day in (days or sorted(uni)):
        fs = candidates(iv, k, day)
        if not fs:
            continue
        pk = (iv, k, sel_name, day)
        if pk not in _PICK:
            _PICK[pk] = SELECTORS[sel_name](fs)
        pick = _PICK[pk]
        if not pick:
            continue
        bars = data[pick["code"]][day]
        rest = bars[k:]
        if not rest:
            continue
        entry = rest[0][1]
        step = 5 if iv == "5m" else 2
        rule = EXITS[exit_name]
        if rule[0] == "M":
            rule = ("M", rule[1] * 5 // step)
        px, why, mx, mn = run_exit(rule, entry, pick, rest)
        net = (px / entry - 1) * 100 - COST
        r = dict(day=day, code=pick["code"], name=pick["name"], t=rest[0][0], flu=round(pick["flu"], 1),
                 entry=entry, exit=round(px), why=why, net=round(net, 2), max=round((mx / entry - 1) * 100, 1))
        out.append(r)
    return out


def summarize(rs):
    if not rs:
        return {}
    nets = [r["net"] for r in rs]
    w = [x for x in nets if x > 0]; l = [x for x in nets if x <= 0]
    eq = pk = dd = 0
    for x in nets:
        eq += x; pk = max(pk, eq); dd = max(dd, pk - eq)
    h = len(nets) // 2
    return dict(n=len(nets), win=round(len(w) / len(nets) * 100), avg=round(st.mean(nets), 2), sum=round(sum(nets), 1),
                pf=round(sum(w) / -sum(l), 2) if l and sum(l) else 99, mdd=round(dd, 1),
                h1=round(sum(nets[:h]), 1), h2=round(sum(nets[h:]), 1))


DATA = {}
if __name__ == "__main__":
    DATA["5m"], DATA["2m"] = load("5m"), load("2m")
    rows = []
    grid = [("5m", 1), ("5m", 2), ("5m", 3), ("2m", 1), ("2m", 2), ("2m", 3), ("2m", 5)]
    days2 = sorted({d for v in DATA["2m"].values() for d in v} & set(uni))
    for (iv, k), s, e in itertools.product(grid, SELECTORS, EXITS):
        days = None if iv == "5m" else days2
        res = summarize(backtest(iv, k, s, e, days))
        if res:
            step = 5 if iv == "5m" else 2
            rows.append(dict(data=iv, signal=f"09:{k*step:02d}", sel=s, exit=e, **res))
    json.dump(rows, open(C / "grid.json", "w"), ensure_ascii=False)
    print(len(rows), "조합")
