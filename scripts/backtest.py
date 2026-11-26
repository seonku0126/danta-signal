"""최근 N거래일 백테스트: 09:01~09:10 매분 스캔을 1분봉으로 재현 → 선정 → 손절/목표/15:20 청산.

  python scripts/backtest.py --days 10          # 오늘 포함 최근 10거래일
주의: 과거 '그 시각의 순위표'는 구할 수 없어서, 그날 하루 거래대금 상위 400종목을 후보군으로 쓴다.
     (09:05에 30억 넘게 거래된 종목은 거의 다 여기에 들어가지만, 약간의 사후 정보가 섞인다)
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from danta.config import Config
from danta.features import from_bars
from danta.naver import Naver
from danta.scanner import evaluate, pick_best, plan
from danta.tracker import Position, force_exit, update

KST = ZoneInfo("Asia/Seoul")
CACHE = Path(__file__).resolve().parent.parent / ".cache"


def cached(name, fn):
    CACHE.mkdir(exist_ok=True)
    p = CACHE / f"{name}.json"
    if p.exists():
        return json.loads(p.read_text())
    v = fn()
    p.write_text(json.dumps(v, ensure_ascii=False))
    return v


def to_kiwoom(day, bars):
    return [{"cntr_tm": f"{day}{b['t']}00", "high_pric": b["h"], "low_pric": b["l"], "cur_prc": b["c"]} for b in bars]


def first_pass(c, cfg):
    return (cfg.min_flu_rt <= c.flu_rt <= cfg.max_flu_rt and c.price >= cfg.min_price
            and c.value_mil >= cfg.min_value_mil and c.cntr_str >= cfg.min_cntr_str)


def run(days_n: int, universe_n: int = 400):
    cfg = Config()
    nv = Naver(workers=16)
    now = datetime.now(KST)
    today = now.strftime("%Y%m%d")
    start = (now - timedelta(days=days_n * 2 + 45)).strftime("%Y%m%d")

    stocks = cached(f"stocks_{today}", nv.all_stocks)
    print(f"보통주 {len(stocks)}개", file=sys.stderr)
    meta = {s["code"]: s for s in stocks}

    def daily(code):
        return code, nv.days(code, start, today)
    dmap = dict(cached(f"daily_{start}_{today}", lambda: nv.pmap(daily, list(meta))))
    cal = [b["d"] for b in dmap.get("005930", [])]
    test_days = cal[-days_n:]
    print("테스트 날짜:", test_days, file=sys.stderr)

    results, logs = [], []
    for day in test_days:
        # 그날 후보군: 전일 데이터가 있고 그날 거래대금(근사) 상위
        rows = []
        for code, bars in dmap.items():
            idx = {b["d"]: i for i, b in enumerate(bars)}
            if day not in idx or idx[day] < 21:
                continue
            i = idx[day]
            b = bars[i]
            val = b["v"] * (b["h"] + b["l"] + b["c"]) / 3
            past = bars[i - 20:i]
            avg20 = sum(x["v"] * (x["h"] + x["l"] + x["c"]) / 3 for x in past) / 20 / 1e6
            rows.append((val, code, bars[i - 1]["c"], avg20))
        rows.sort(reverse=True)
        uni = rows[:universe_n]

        def mins(r):
            return r[1], nv.minutes(r[1], day, "0900", "0911")
        mb = dict(cached(f"min_{day}_0911_{universe_n}", lambda: nv.pmap(mins, uni)))
        info = {code: (prev, avg20) for _, code, prev, avg20 in uni}
        if not any(mb.values()):
            results.append({"day": day, "status": "분봉 데이터 없음(네이버 보관기간 초과 또는 9시 개장 아님)"})
            print(day, "데이터 없음", file=sys.stderr)
            continue

        prev_code, last_best, chosen, chosen_t = None, None, None, None
        for minute in range(1, cfg.scan_minutes + 1):
            t = f"09{minute:02d}"
            cands = []
            for code, bars in mb.items():
                done = [b for b in bars if b["t"] < t]
                prev, avg20 = info[code]
                c = from_bars(code, meta[code]["name"], done, prev, avg20, meta[code]["mcap"] / 1e8)
                if c:
                    cands.append(c)
            cands.sort(key=lambda c: c.value_mil, reverse=True)
            for i, c in enumerate(cands, 1):
                c.value_rank = i
            ok = [evaluate(c, cfg) for c in cands if first_pass(c, cfg)]
            best = pick_best(ok, cfg)
            if best and best.code == prev_code:
                chosen, chosen_t = best, t
                break
            prev_code, last_best, chosen_t = (best.code if best else None), best, t
        chosen = chosen or last_best

        if not chosen:
            results.append({"day": day, "status": "쉼"})
            print(day, "쉼", file=sys.stderr)
            continue
        entry, stop, target, stop_pct = plan(chosen, cfg)
        full = nv.minutes(chosen.code, day, "0900", "1530")
        pos = Position(chosen.code, chosen.name, entry, stop, target, f"{day}{chosen_t}00")
        until = [b for b in full if b["t"] < "1520"]
        update(pos, to_kiwoom(day, until + [{"t": "9999", "h": 0, "l": 0, "c": 0}]))
        status = "청산"
        if not pos.closed:
            if day == today and now.strftime("%H%M") < "1520":
                status = "진행 중"
                pos.exit_price, pos.exit_reason, pos.exit_tm = until[-1]["c"] if until else entry, "보유 중(현재가)", ""
            else:
                last = until[-1] if until else {"c": entry, "t": "1519"}
                force_exit(pos, last["c"], f"{day}{last['t']}00")
        gross = pos.pct(pos.exit_price)
        r = {"day": day, "status": status, "code": chosen.code, "name": chosen.name, "signal": chosen_t,
             "score": chosen.score, "entry": entry, "stop": stop, "target": target, "exit": pos.exit_price,
             "reason": pos.exit_reason, "exit_tm": pos.exit_tm[8:12], "gross": round(gross, 2),
             "net": round(gross - cfg.cost_pct, 2), "max": round(pos.pct(pos.max_price), 2),
             "min": round(pos.pct(pos.min_price), 2), "flu_at_signal": round(chosen.flu_rt, 2),
             "value_eok": round(chosen.value_mil / 100), "rvol": round(chosen.rvol, 2),
             "strength": round(chosen.cntr_str)}
        results.append(r)
        print(day, r["name"], r["reason"], r["net"], file=sys.stderr)
    return results


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=10)
    ap.add_argument("--out", default="backtest.csv")
    a = ap.parse_args()
    res = run(a.days)
    keys = sorted({k for r in res for k in r}, key=lambda k: ["day", "status", "name", "code"].index(k)
                  if k in ("day", "status", "name", "code") else 99)
    with open(a.out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(res)
    print(json.dumps(res, ensure_ascii=False))
