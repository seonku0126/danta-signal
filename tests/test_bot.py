"""키움 서버 없이 가짜 데이터로 하루를 통째로 돌려보는 테스트.  실행: python -m pytest -q"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from danta.bot import run_day
from danta.config import Config
from danta.scanner import Candidate, evaluate, plan, round_down, round_up, tick
from danta.stats import Store, summarize
from danta.telegram import Telegram
from danta.tracker import Position, update

DAY = "20261127"  # 금요일


class SimClock:
    def __init__(self, start: datetime):
        self.t = start

    def now(self):
        return self.t

    def sleep_until(self, t):
        self.t = max(self.t, t)

    def sleep(self, sec):
        self.t += timedelta(seconds=sec)


def bar(hhmm: str, o, h, l, c, v=10000):
    return {"cntr_tm": f"{DAY}{hhmm}00", "open_pric": f"+{o}", "high_pric": f"+{h}", "low_pric": f"+{l}",
            "cur_prc": f"+{c}", "trde_qty": str(v)}


class FakeKiwoom:
    """A(좋은 후보), B(투자경고), C(VWAP 아래로 밀림), D(우선주) 네 종목."""

    def __init__(self, clock, path="up", empty=False):
        self.clock, self.path, self.empty = clock, path, empty

    def _hm(self):
        return self.clock.now().strftime("%H%M")

    def gainers(self):
        if self.empty:
            return []
        return [
            {"stk_cd": "111110", "stk_nm": "에이전자", "cur_prc": "+10500", "flu_rt": "+7.14", "cntr_str": "165.00"},
            {"stk_cd": "222220", "stk_nm": "비바이오", "cur_prc": "+5200", "flu_rt": "+9.00", "cntr_str": "180.00"},
            {"stk_cd": "333330", "stk_nm": "씨화학", "cur_prc": "+20100", "flu_rt": "+4.10", "cntr_str": "130.00"},
            {"stk_cd": "444445", "stk_nm": "디우", "cur_prc": "+8000", "flu_rt": "+6.00", "cntr_str": "150.00"},
            {"stk_cd": "555550", "stk_nm": "이상한가", "cur_prc": "+3000", "flu_rt": "+29.90", "cntr_str": "300.00"},
        ]

    def expected_gainers(self):
        if self.empty:
            return []
        return [
            {"stk_cd": "111110", "stk_nm": "에이전자", "exp_cntr_pric": "+10300", "flu_rt": "+5.10",
             "exp_cntr_qty": "60000", "sel_req": "20000", "buy_req": "70000"},
            {"stk_cd": "222220", "stk_nm": "비바이오", "exp_cntr_pric": "+5100", "flu_rt": "+6.90",
             "exp_cntr_qty": "200000", "sel_req": "1000", "buy_req": "9000"},
            {"stk_cd": "333330", "stk_nm": "씨화학", "exp_cntr_pric": "+20500", "flu_rt": "+6.20",
             "exp_cntr_qty": "30000", "sel_req": "9000", "buy_req": "12000"},
            {"stk_cd": "666660", "stk_nm": "너무작음", "exp_cntr_pric": "+2000", "flu_rt": "+4.00",
             "exp_cntr_qty": "1000", "sel_req": "10", "buy_req": "10"},
        ]

    def value_leaders(self):
        if self.empty:
            return []
        return [{"stk_cd": c, "trde_prica": v, "now_rank": str(i + 1)} for i, (c, v) in enumerate(
            [("111110", "18000"), ("222220", "12000"), ("333330", "9000"), ("444445", "8000"), ("555550", "7000")])]

    def stock_info(self, code):
        return {"auditInfo": "투자경고" if code == "222220" else "정상", "orderWarning": "0"}

    def daily_bars(self, code, day):
        return [{"dt": day, "trde_prica": "99999"}] + [{"dt": f"2026110{i}", "trde_prica": "20000"} for i in range(20)]

    def basic(self, code):
        m = {
            "111110": dict(open_pric="+10050", high_pric="+10550", low_pric="+10000", cur_prc="+10500"),
            "222220": dict(open_pric="+4900", high_pric="+5250", low_pric="+4850", cur_prc="+5200"),
            "333330": dict(open_pric="+20500", high_pric="+20900", low_pric="+19900", cur_prc="+20100"),
        }[code]
        return {**m, "mac": "+3500", "base_pric": "9800" if code == "111110" else "4770" if code == "222220" else "19300",
                "250hgst": "+11000"}

    def minute_bars(self, code, day):
        if code == "333330":
            return [bar("0900", 20500, 20900, 20400, 20800, 50000), bar("0901", 20800, 20800, 20100, 20100, 5000)]
        hm = self._hm()
        early = [bar("0900", 10050, 10300, 10000, 10250, 30000), bar("0901", 10250, 10450, 10200, 10400, 20000),
                 bar("0902", 10400, 10550, 10380, 10500, 20000)]
        if self.path == "up":
            later = [bar("0904", 10500, 10700, 10480, 10650), bar("0905", 10650, 10900, 10600, 10850),
                     bar("0910", 10850, 11300, 10800, 11250), bar("0915", 11250, 11500, 11200, 11400),
                     bar("0916", 11400, 11450, 11380, 11420)]
        else:
            later = [bar("0904", 10500, 10520, 10300, 10350), bar("0905", 10350, 10360, 10050, 10100),
                     bar("0906", 10100, 10150, 10000, 10050), bar("0907", 10050, 10080, 10020, 10060)]
        all_bars = early + later
        return [b for b in all_bars if b["cntr_tm"][8:12] <= hm]

    def vi(self, code):
        if self._hm() >= "0910" and self.path == "up":
            return [{"vimotn_cnt": "1", "viaplc_tp": "정적", "motn_pric": "+11000", "trde_cntr_proc_time": "091012"}]
        return []


def make(tmp, path="up", empty=False):
    cfg = Config()
    cfg.db_path = os.path.join(tmp, "t.db")
    clock = SimClock(datetime(2026, 11, 27, 8, 40))
    return cfg, FakeKiwoom(clock, path, empty), Telegram("", "", dry_run=True), Store(cfg.db_path), clock


def test_tick_rounding():
    assert tick(1999) == 1 and tick(4999) == 5 and tick(10500) == 10 and tick(60000) == 100
    assert round_down(10291) == 10290 and round_up(10291) == 10300


def test_full_day_target_hit():
    with tempfile.TemporaryDirectory() as tmp:
        cfg, api, tg, store, clock = make(tmp, "up")
        run_day(cfg, api, tg, store, clock)
        text = "\n".join(tg.sent)
        assert "에이전자" in text and "비바이오" not in text.split("오늘의 종목")[1].split("근거")[0]
        assert "목표가 도달" in text and "VI 발동" in text and "+3% 터치" in text and "+5% 터치" not in text
        t = store.trades()[0]
        assert t["exit_reason"] == "목표가 도달" and t["net_pct"] > 0
        s = summarize(store, cfg.notional)
        assert s.n == 1 and s.wins == 1
        # 리포트 숫자와 DB 숫자가 일치하는지
        assert f"{s.pnl_won / 10000:+.1f}만원" in tg.sent[-1]
        pre = [m for m in tg.sent if "장전 관심종목" in m][0]
        assert "에이전자" in pre and "씨화학" in pre and "비바이오" not in pre and "너무작음" not in pre
        assert "장전 관심종목에서 조건 통과" in "\n".join(tg.sent)
        rep = tg.sent[-1]
        assert "장전 관심종목 결과" in rep and "에이전자 🎯" in rep and "최근 1거래일" in rep and "이번 달" in rep


def test_two_days_stats_accumulate():
    with tempfile.TemporaryDirectory() as tmp:
        cfg, api, tg, store, clock = make(tmp, "up")
        run_day(cfg, api, tg, store, clock)
        # 다음 거래일(월) 손절
        clock.t = datetime(2026, 11, 30, 8, 40)
        global DAY
        old, DAY = DAY, "20261130"
        try:
            api.path = "down"
            run_day(cfg, api, tg, store, clock)
        finally:
            DAY = old
        s = summarize(store, cfg.notional)
        assert s.n == 2 and s.wins == 1 and s.losses == 1
        rep = tg.sent[-1]
        assert "최근 2거래일" in rep and "11/27" in rep and "11/30" in rep and "2회 · 1승 1패" in rep


def test_full_day_stop():
    with tempfile.TemporaryDirectory() as tmp:
        cfg, api, tg, store, clock = make(tmp, "down")
        run_day(cfg, api, tg, store, clock)
        t = store.trades()[0]
        assert t["exit_reason"] == "손절", t
        assert t["net_pct"] < 0
        assert t["exit_price"] == t["stop"]


def test_holiday():
    with tempfile.TemporaryDirectory() as tmp:
        cfg, api, tg, store, clock = make(tmp, empty=True)
        run_day(cfg, api, tg, store, clock)
        assert store.day_counts() == {"holiday": 1} and not store.trades()


def test_no_signal_when_strict():
    with tempfile.TemporaryDirectory() as tmp:
        cfg, api, tg, store, clock = make(tmp)
        cfg.min_score = 0.99
        run_day(cfg, api, tg, store, clock)
        assert "오늘은 쉽니다" in "\n".join(tg.sent)
        assert store.day_counts() == {"no_signal": 1}


def test_same_bar_stop_and_target_counts_as_stop():
    p = Position("1", "x", 10000, 9700, 10600, f"{DAY}090300")
    update(p, [bar("0904", 10000, 10700, 9600, 10000), bar("0905", 1, 1, 1, 1)])
    assert p.exit_reason == "손절"


def test_rejections():
    cfg = Config()
    c = Candidate("1", "x", 10000, 7, 150, 5000, open=10100, high=10200, low=9900, prev_close=9350, vwap=9900)
    assert evaluate(c, cfg).reject.startswith("시가 아래")
    c = Candidate("1", "x", 10000, 7, 150, 5000, open=9500, high=10500, low=9400, prev_close=9350, vwap=9800)
    assert "밀림" in evaluate(c, cfg).reject
    c = Candidate("1", "x", 10000, 7, 150, 5000, open=9500, high=10050, low=9400, prev_close=9350, vwap=9800)
    assert not evaluate(c, cfg).reject and c.score > 0
    e, s, t, pct = plan(c, cfg)
    assert s < e < t and 2.0 <= pct <= 4.0 and round((t / e - 1) * 100 / pct) == 2


class _Resp:
    def __init__(self, body, headers=None, status=200):
        self._b, self.headers, self.status_code = body, headers or {}, status
        self.ok = status == 200

    def json(self):
        return self._b

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class _Sess:
    def __init__(self):
        self.calls = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append((url, json, headers))
        if url.endswith("/oauth2/token"):
            return _Resp({"token": "T", "expires_dt": "20991231000000", "return_code": 0})
        page = sum(1 for c in self.calls if c[2] and c[2].get("api-id") == "ka10027")
        if page == 1:
            return _Resp({"return_code": 0, "pred_pre_flu_rt_upper": [{"stk_cd": "000010"}]},
                         {"cont-yn": "Y", "next-key": "K1"})
        return _Resp({"return_code": 0, "pred_pre_flu_rt_upper": [{"stk_cd": "000020"}]})


def test_kiwoom_client_paging_and_headers():
    from danta.kiwoom import Kiwoom, num
    s = _Sess()
    k = Kiwoom("https://api.kiwoom.com", "a", "b", session=s)
    k.MIN_GAP = 0
    rows = k.gainers()
    assert [r["stk_cd"] for r in rows] == ["000010", "000020"]
    h2 = s.calls[-1][2]
    assert h2["cont-yn"] == "Y" and h2["next-key"] == "K1" and h2["authorization"] == "Bearer T"
    assert s.calls[1][0].endswith("/api/dostk/rkinfo")
    assert num("-8540", True) == 8540 and num("+3.25") == 3.25 and num("") == 0
