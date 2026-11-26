"""1분봉으로 후보 지표 계산 (실시간 운영·백테스트 공용). 체결강도 대신 '상승봉 거래량 / 하락봉 거래량 × 100'을 쓴다."""
from __future__ import annotations

from .scanner import Candidate


def from_bars(code: str, name: str, bars: list[dict], prev_close: float, avg20_value_mil: float,
              mcap_eok: float, hi52: float = 0.0, live_price: float | None = None) -> Candidate | None:
    if not bars or not prev_close:
        return None
    o = bars[0]["o"] or bars[0]["c"]
    hi = max(b["h"] or b["c"] for b in bars)
    lo = min(b["l"] or b["c"] for b in bars)
    price = live_price or bars[-1]["c"]
    hi, lo = max(hi, price), min(lo, price)
    pv = vol = up = down = 0.0
    for b in bars:
        typ = ((b["h"] or b["c"]) + (b["l"] or b["c"]) + b["c"]) / 3
        pv += typ * b["v"]
        vol += b["v"]
        if b["c"] > (b["o"] or b["c"]):
            up += b["v"]
        elif b["c"] < (b["o"] or b["c"]):
            down += b["v"]
    strength = up / down * 100 if down else (200.0 if up else 100.0)
    c = Candidate(code=code, name=name, price=price, flu_rt=(price / prev_close - 1) * 100,
                  cntr_str=min(strength, 500.0), value_mil=pv / 1e6)
    c.open, c.high, c.low, c.prev_close = o, hi, lo, prev_close
    c.vwap = pv / vol if vol else price
    c.avg20_value_mil, c.mcap_eok, c.hi250 = avg20_value_mil, mcap_eok, hi52
    return c
