"""종목 선정 규칙 v2 (2026-11-26 백테스트로 선택).

규칙: 장 시작 5분째(09:05)에, 상승률 +2~15%이면서 현재가가 VWAP와 시가 위에 있는 종목 중
      09:01~09:04 거래대금(동시호가 제외)이 가장 큰 1종목.
근거: 야후 2분봉 32거래일 백테스트에서 09:04~09:06 진입이 15개 청산 규칙 중 13개에서 플러스,
      09:02(너무 이름)·09:10 이후(늦음)는 대부분 마이너스. scripts/research.py 참고.
매일 반드시 1종목: 조건을 만족하는 종목이 없으면 단계적으로 완화한다.
"""
from __future__ import annotations

from dataclasses import dataclass

from .scanner import Candidate


@dataclass
class Snap:
    code: str
    name: str
    price: float          # 현재가
    prev: float           # 전일 종가
    open: float
    vwap: float
    value_early: float    # 09:01~ 완성봉 거래대금 (원, 동시호가 제외)
    value_total: float    # 오늘 누적 거래대금 (원)
    mcap: float           # 원
    high: float
    low: float

    @property
    def flu(self) -> float:
        return (self.price / self.prev - 1) * 100 if self.prev else 0.0


def snap_from_minutes(code: str, name: str, bars: list[dict], price: float, prev: float,
                      value_total: float, mcap: float) -> Snap | None:
    """bars: 네이버 1분봉(09:00부터, 완성봉만). 첫 봉(09:00)은 동시호가 체결이 섞여 거래대금 집계에서 뺀다."""
    if not bars or not prev:
        return None
    pv = vol = 0.0
    for b in bars:
        typ = (b["h"] + b["l"] + b["c"]) / 3
        pv += typ * b["v"]
        vol += b["v"]
    early = sum((b["h"] + b["l"] + b["c"]) / 3 * b["v"] for b in bars[1:])
    price = price or bars[-1]["c"]
    return Snap(code, name, price, prev, bars[0]["o"] or bars[0]["c"], pv / vol if vol else price, early,
                value_total, mcap, max([b["h"] for b in bars] + [price]), min([b["l"] for b in bars] + [price]))


def choose(snaps: list[Snap], min_price=1000, min_mcap=5e10) -> tuple[Snap | None, str]:
    """(선택 종목, 적용된 단계). 단계: 정규 → 완화1 → 완화2."""
    base = [s for s in snaps if s.price >= min_price and s.mcap >= min_mcap]
    tiers = [
        ("정규", lambda s: 2 <= s.flu <= 15 and s.price >= s.vwap and s.price >= s.open),
        ("완화1(상승률·VWAP)", lambda s: 0 < s.flu <= 20 and s.price >= s.vwap),
        ("완화2(상승 종목 중 대금 1위)", lambda s: s.flu > 0),
    ]
    for name, ok in tiers:
        pool = [s for s in base if ok(s)]
        if pool:
            return max(pool, key=lambda s: (s.value_early, s.value_total)), name
    pool = base or snaps
    if pool:
        return max(pool, key=lambda s: s.value_total), "완화3(대금 1위)"
    return None, "없음"


def to_candidate(s: Snap, rank: int) -> Candidate:
    c = Candidate(code=s.code, name=s.name, price=s.price, flu_rt=s.flu, cntr_str=0, value_mil=s.value_total / 1e6,
                  value_rank=rank)
    c.open, c.high, c.low, c.prev_close, c.vwap = s.open, s.high, s.low, s.prev, s.vwap
    c.mcap_eok = s.mcap / 1e8
    c.parts = {"초반거래대금(억)": s.value_early / 1e8}
    return c
