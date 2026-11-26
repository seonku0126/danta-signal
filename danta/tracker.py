"""진입 후 손절/목표/마일스톤 판정. 1분봉 고가·저가로 판단하고, 한 봉에서 둘 다 닿으면 손절로 친다(보수적)."""
from __future__ import annotations

from dataclasses import dataclass, field

from .kiwoom import num


@dataclass
class Position:
    code: str
    name: str
    entry: int
    stop: int
    target: int
    entry_tm: str               # YYYYMMDDHHmmss (시그널 시각)
    max_price: float = 0.0
    min_price: float = 0.0
    last_price: float = 0.0
    last_bar_tm: str = ""
    hit_milestones: list = field(default_factory=list)
    exit_price: float = 0.0
    exit_reason: str = ""
    exit_tm: str = ""
    vi_count: int = 0

    def __post_init__(self):
        self.max_price = self.min_price = self.last_price = float(self.entry)

    @property
    def closed(self) -> bool:
        return bool(self.exit_reason)

    def pct(self, p: float) -> float:
        return (p / self.entry - 1) * 100


def update(pos: Position, bars: list[dict], milestones=()) -> list[tuple[str, dict]]:
    """새로 완성된 1분봉들을 반영하고 발생 이벤트 목록을 돌려준다.

    시그널이 나온 그 분의 봉은 진입 전 가격이 섞여 있으므로 쓰지 않는다.
    마지막(진행 중) 봉도 아직 확정이 아니라 고가/저가 판정에서 제외한다.
    """
    events: list[tuple[str, dict]] = []
    if pos.closed:
        return events
    entry_min = pos.entry_tm[:12]
    ordered = sorted(bars, key=lambda b: str(b.get("cntr_tm")))
    if ordered:
        pos.last_price = num(ordered[-1].get("cur_prc"), True) or pos.last_price
    done = ordered[:-1]  # 진행 중 봉 제외
    for b in done:
        tm = str(b.get("cntr_tm"))
        if tm[:12] <= entry_min or tm <= pos.last_bar_tm:
            continue
        hi, lo = num(b.get("high_pric"), True), num(b.get("low_pric"), True)
        pos.last_bar_tm = tm
        if lo and lo <= pos.stop:
            pos.min_price = min(pos.min_price, lo)
            _close(pos, pos.stop, "손절", tm)
            events.append(("exit", {}))
            return events
        if hi and hi >= pos.target:
            pos.max_price = max(pos.max_price, hi)
            _close(pos, pos.target, "목표가 도달", tm)
            events.append(("exit", {}))
            return events
        pos.max_price = max(pos.max_price, hi or 0)
        pos.min_price = min(pos.min_price, lo or pos.min_price)
        for m in milestones:
            if m not in pos.hit_milestones and pos.pct(pos.max_price) >= m:
                pos.hit_milestones.append(m)
                events.append(("milestone", {"pct": m, "tm": tm}))
    return events


def force_exit(pos: Position, price: float, tm: str):
    if not pos.closed:
        _close(pos, price, "장마감 청산", tm)


def _close(pos: Position, price: float, reason: str, tm: str):
    pos.exit_price = float(price)
    pos.exit_reason = reason
    pos.exit_tm = tm
