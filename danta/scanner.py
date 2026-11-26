"""장 초반 후보 선정 로직. API 호출부와 판단부를 분리해 테스트 가능하게 만듦."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .config import Config
from .kiwoom import num

log = logging.getLogger(__name__)

EXCLUDE_NAME_WORDS = ("스팩", "SPAC", "리츠", "ETN", "KODEX", "TIGER", "ACE ", "RISE ", "SOL ", "PLUS ", "KIWOOM ")


@dataclass
class Candidate:
    code: str
    name: str
    price: float
    flu_rt: float
    cntr_str: float
    value_mil: float           # 오늘 누적 거래대금 (백만원)
    value_rank: int = 999
    # 상세 조회 후 채움
    mcap_eok: float = 0.0
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    prev_close: float = 0.0
    hi250: float = 0.0
    vwap: float = 0.0
    avg20_value_mil: float = 0.0
    warning: str = ""
    reject: str = ""
    score: float = 0.0
    parts: dict = field(default_factory=dict)

    @property
    def gap_pct(self) -> float:
        return (self.open / self.prev_close - 1) * 100 if self.prev_close else 0.0

    @property
    def rvol(self) -> float:
        return self.value_mil / self.avg20_value_mil if self.avg20_value_mil else 0.0

    @property
    def pullback_pct(self) -> float:
        return (1 - self.price / self.high) * 100 if self.high else 0.0

    @property
    def range_pos(self) -> float:
        rng = self.high - self.low
        return (self.price - self.low) / rng if rng > 0 else 1.0


# ── 1단계: 순위 TR 두 개를 교집합 ──────────────────────────
def merge_rankings(gainers: list[dict], leaders: list[dict], cfg: Config) -> list[Candidate]:
    value_map = {}
    for i, r in enumerate(leaders):
        code = str(r.get("stk_cd", "")).split("_")[0]
        value_map[code] = (num(r.get("trde_prica"), True), int(num(r.get("now_rank"))) or i + 1)

    out = []
    for r in gainers:
        code = str(r.get("stk_cd", "")).split("_")[0]
        name = str(r.get("stk_nm", "")).strip()
        if code not in value_map:
            continue  # 거래대금 상위에 없으면 유동성 부족
        if any(w in name for w in EXCLUDE_NAME_WORDS) or not code.isdigit() or code[-1] != "0":
            continue  # 스팩·ETF류·우선주(코드 끝자리 ≠ 0) 제외
        value, rank = value_map[code]
        c = Candidate(code=code, name=name, price=num(r.get("cur_prc"), True), flu_rt=num(r.get("flu_rt")),
                      cntr_str=num(r.get("cntr_str")), value_mil=value, value_rank=rank)
        if not (cfg.min_flu_rt <= c.flu_rt <= cfg.max_flu_rt):
            continue
        if c.price < cfg.min_price or c.value_mil < cfg.min_value_mil or c.cntr_str < cfg.min_cntr_str:
            continue
        out.append(c)
    out.sort(key=lambda c: c.value_mil, reverse=True)
    return out[: cfg.max_candidates_detail]


# ── 2단계: 상세 데이터 채우기 ─────────────────────────────
def enrich(c: Candidate, basic: dict, info: dict, bars: list[dict], daily: list[dict], today: str) -> Candidate:
    c.mcap_eok = num(basic.get("mac"), True)
    c.open = num(basic.get("open_pric"), True)
    c.high = num(basic.get("high_pric"), True)
    c.low = num(basic.get("low_pric"), True)
    c.prev_close = num(basic.get("base_pric"), True)
    c.hi250 = num(basic.get("250hgst"), True)
    cur = num(basic.get("cur_prc"), True)
    if cur:
        c.price = cur
    audit = str(info.get("auditInfo", "정상")).strip() or "정상"
    warn = str(info.get("orderWarning", "0")).strip() or "0"
    c.warning = "" if (audit == "정상" and warn in ("0", "")) else f"{audit}/{warn}"

    pv = vol = 0.0
    for b in bars:
        v = num(b.get("trde_qty"), True)
        p = (num(b.get("high_pric"), True) + num(b.get("low_pric"), True) + num(b.get("cur_prc"), True)) / 3
        pv += p * v
        vol += v
    c.vwap = pv / vol if vol else 0.0

    past = [d for d in daily if str(d.get("dt")) != today][:20]
    vals = [num(d.get("trde_prica"), True) for d in past]
    c.avg20_value_mil = sum(vals) / len(vals) if vals else 0.0
    return c


# ── 3단계: 거르기 + 점수 ─────────────────────────────────
def _clip(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def evaluate(c: Candidate, cfg: Config) -> Candidate:
    if c.warning:
        c.reject = f"투자유의({c.warning})"
    elif c.mcap_eok and c.mcap_eok < cfg.min_mcap_eok:
        c.reject = f"시총 {c.mcap_eok:.0f}억 < {cfg.min_mcap_eok:.0f}억"
    elif c.gap_pct > cfg.max_gap_pct:
        c.reject = f"갭 {c.gap_pct:.1f}% 과다"
    elif c.pullback_pct > cfg.max_pullback_pct:
        c.reject = f"고점대비 -{c.pullback_pct:.1f}% 이미 밀림"
    elif c.vwap and c.price < c.vwap:
        c.reject = "VWAP 아래"
    elif c.open and c.price < c.open:
        c.reject = "시가 아래(음봉)"
    elif c.avg20_value_mil and c.rvol < cfg.min_rvol:
        c.reject = f"거래대금 배수 {c.rvol:.2f} 부족"
    if c.reject:
        c.score = 0.0
        return c

    parts = {
        "거래대금배수": _clip(c.rvol / 1.5) if c.avg20_value_mil else 0.3,   # 20일 평균의 1.5배면 만점
        "체결강도": _clip((c.cntr_str - 100) / 100),                       # 200% 만점
        "당일위치": _clip(c.range_pos),                                     # 고가에 붙어 있을수록
        "거래대금순위": _clip(1 - (c.value_rank - 1) / 50),
        "신고가근접": _clip((c.price / c.hi250 - 0.85) / 0.15) if c.hi250 else 0.0,
        "상승률": _clip(1 - abs(c.flu_rt - 7.5) / 7.5),                     # 5~10% 구간 선호
    }
    w = {"거래대금배수": .30, "체결강도": .20, "당일위치": .15, "거래대금순위": .15, "신고가근접": .10, "상승률": .10}
    c.parts = parts
    c.score = round(sum(parts[k] * w[k] for k in w), 3)
    return c


def pick_best(cands: list[Candidate], cfg: Config) -> Candidate | None:
    ok = [c for c in cands if not c.reject and c.score >= cfg.min_score]
    return max(ok, key=lambda c: c.score) if ok else None


# ── 진입가·손절·목표 ──────────────────────────────────────
def tick(price: float) -> int:
    for lim, t in ((2000, 1), (5000, 5), (20000, 10), (50000, 50), (200000, 100), (500000, 500)):
        if price < lim:
            return t
    return 1000


def round_down(p: float) -> int:
    t = tick(p)
    return int(p // t * t)


def round_up(p: float) -> int:
    t = tick(p)
    return int(-(-p // t) * t)


def plan(c: Candidate, cfg: Config) -> tuple[int, int, int, float]:
    """(진입가, 손절가, 목표가, 손절폭%) — 손절은 VWAP 살짝 아래, 2~4% 범위로 제한."""
    entry = int(c.price)
    dist = (entry - c.vwap) / entry * 100 + 0.5 if c.vwap else cfg.stop_min_pct
    stop_pct = _clip(dist, cfg.stop_min_pct, cfg.stop_max_pct)
    stop = round_down(entry * (1 - stop_pct / 100))
    target = round_up(entry * (1 + stop_pct * cfg.reward_risk / 100))
    return entry, stop, target, round(stop_pct, 2)
