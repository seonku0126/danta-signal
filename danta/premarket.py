"""장 시작 전(08:55) 동시호가 예상체결 기준 관심종목. 장이 열리기 전에 '오를 준비가 된' 후보를 미리 알린다."""
from __future__ import annotations

from dataclasses import dataclass

from .config import Config
from .kiwoom import num
from .scanner import EXCLUDE_NAME_WORDS


@dataclass
class Watch:
    code: str
    name: str
    exp_price: float
    flu_rt: float
    exp_value_mil: float      # 예상체결대금 (백만원)
    bid_ask: float            # 매수잔량 / 매도잔량
    avg20_value_mil: float = 0.0
    warning: str = ""
    score: float = 0.0
    source: str = "kiwoom"        # kiwoom(동시호가) / nxt(넥스트레이드 프리마켓)
    yday_value: float = 0.0       # 어제 거래대금 (억)

    @property
    def ratio(self) -> float:
        return self.exp_value_mil / self.avg20_value_mil if self.avg20_value_mil else 0.0


def first_pass(rows: list[dict], cfg: Config) -> list[Watch]:
    out = []
    for r in rows:
        code = str(r.get("stk_cd", "")).split("_")[0]
        name = str(r.get("stk_nm", "")).strip()
        if not code.isdigit() or code[-1] != "0" or any(w in name for w in EXCLUDE_NAME_WORDS):
            continue
        price, qty = num(r.get("exp_cntr_pric"), True), num(r.get("exp_cntr_qty"), True)
        flu = num(r.get("flu_rt"))
        sel, buy = num(r.get("sel_req"), True), num(r.get("buy_req"), True)
        w = Watch(code, name, price, flu, price * qty / 1e6, buy / sel if sel else 5.0)
        if cfg.pre_min_flu <= flu <= cfg.pre_max_flu and w.exp_value_mil >= cfg.pre_min_value_mil \
                and price >= cfg.min_price:
            out.append(w)
    out.sort(key=lambda w: w.exp_value_mil, reverse=True)
    return out[:10]


def finalize(ws: list[Watch], cfg: Config) -> list[Watch]:
    """감리·20일 평균이 채워진 후보를 점수화해서 상위 N개."""
    ok = []
    for w in ws:
        if w.warning:
            continue
        r = min(w.ratio / 0.1, 1.0) if w.avg20_value_mil else 0.3   # 장전에 평소 하루치의 10%면 만점
        b = min(max((w.bid_ask - 1) / 2, 0.0), 1.0)                  # 매수잔량이 매도의 3배면 만점
        f = 1 - abs(w.flu_rt - 6) / 6                                # 4~8% 갭 선호
        w.score = round(0.5 * r + 0.25 * b + 0.25 * max(f, 0.0), 3)
        ok.append(w)
    ok.sort(key=lambda w: w.score, reverse=True)
    return ok[: cfg.pre_count]
