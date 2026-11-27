"""GitHub Actions용 실행기 (네이버 시세, 키·IP 불필요).

  python -m danta.gh morning     # 08:40 시작 → 공지·장전 관심종목·실시간 스캔·추적 (최대 14:35까지)
  python -m danta.gh afternoon   # 14:25 시작 → 남은 추적·15:20 청산·15:35 마감 리포트
  python -m danta.gh scan-once   # 지금 후보·점수 표 (점검용)

상태는 data/ 폴더(state.json, danta.db, minutes/)에 저장하고 워크플로가 커밋한다.
"""
from __future__ import annotations

import gzip
import json
import logging
import sys
import time as _time
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import messages as M
from .config import Config
from .features import from_bars
from .naver import Naver, is_common
from .premarket import Watch, finalize
from .scanner import evaluate, pick_best, plan
from .strategy import choose, snap_from_minutes, to_candidate
from .stats import Store, summarize, watch_summary
from .telegram import Telegram
from .tracker import Position, force_exit, update

KST = ZoneInfo("Asia/Seoul")
DATA = Path(__file__).resolve().parent.parent / "data"
STATE = DATA / "state.json"
log = logging.getLogger("danta")


def now() -> datetime:
    return datetime.now(KST).replace(tzinfo=None)


def wait_until(t: datetime):
    while (d := (t - now()).total_seconds()) > 0:
        _time.sleep(min(d, 20))


def at(t: time, plus_min: float = 0) -> datetime:
    return datetime.combine(now().date(), t) + timedelta(minutes=plus_min)


def load_state(day: str) -> dict:
    if STATE.exists():
        s = json.loads(STATE.read_text())
        if s.get("day") == day:
            return s
    return {"day": day}


def save_state(s: dict):
    DATA.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(s, ensure_ascii=False, indent=1))


class Live:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.nv = Naver(workers=16)
        self.tg = Telegram(cfg.tg_token, cfg.tg_chat_id, dry_run="--dry-run" in sys.argv)
        DATA.mkdir(exist_ok=True)
        self.store = Store(str(DATA / "danta.db"))
        self.day = now().strftime("%Y%m%d")
        self.state = load_state(self.day)
        self._daily: dict[str, tuple[float, float]] = {}   # code → (전일종가, 20일 평균 거래대금 백만원)
        self._hi52: dict[str, float] = {}
        self._mins: dict[str, list] = {}
        self.yday_top: set[str] = set()

    # ── 공통 ──
    def market_open(self) -> bool:
        rows = self.nv._get("https://m.stock.naver.com/api/stocks/marketValue/KOSPI?page=1&pageSize=1") or {}
        st = ((rows.get("stocks") or [{}])[0]).get("marketStatus")
        return st == "OPEN"

    def daily_info(self, code: str) -> tuple[float, float]:
        if code not in self._daily:
            start = (now() - timedelta(days=45)).strftime("%Y%m%d")
            bars = [b for b in self.nv.days(code, start, self.day) if b["d"] != self.day]
            prev = bars[-1]["c"] if bars else 0.0
            past = bars[-20:]
            avg = sum(b["v"] * (b["h"] + b["l"] + b["c"]) / 3 for b in past) / len(past) / 1e6 if past else 0.0
            self._daily[code] = (prev, avg)
        return self._daily[code]

    def hi52(self, code: str) -> float:
        if code not in self._hi52:
            self._hi52[code] = self.nv.hi52(code)
        return self._hi52[code]

    def minutes(self, code: str, upto: str) -> list[dict]:
        """완성된 분봉만 (upto 미만). 같은 분 안에서는 캐시."""
        key = f"{code}:{upto}"
        if key not in self._mins:
            self._mins[key] = [b for b in self.nv.minutes(code, self.day, "0900", "1530") if b["t"] < upto]
        return self._mins[key]

    # ── 0) 장전 관심종목: NXT 프리마켓(08:00~08:50) 가격 기준 ──
    def premarket(self) -> list[Watch]:
        cfg = self.cfg
        top = [r for r in self.nv.ranking("priceTop", pages=2) if is_common(r)]
        self.yday_top = {r["code"] for r in top}   # 장 시작 전 순위 = 전일 거래대금 상위 (9시 후보군에 사용)
        uni = {r["code"]: r for r in top + self.nv.ranking("up", pages=1) if is_common(r)}
        def one(code):
            b = self.nv.basic(code)
            om = b.get("overMarketPriceInfo") or {}
            if om.get("tradingSessionType") != "PRE_MARKET":
                return None
            price = float(str(om.get("overPrice", "0")).replace(",", "") or 0)
            flu = float(str(om.get("fluctuationsRatio", "0")).replace(",", "") or 0)
            return code, price, flu
        out = []
        for res in self.nv.pmap(one, list(uni)):
            if not res:
                continue
            code, price, flu = res
            if not (cfg.pre_min_flu <= flu <= cfg.pre_max_flu) or price < cfg.min_price:
                continue
            r = uni[code]
            if r["mcap"] / 1e8 < cfg.min_mcap_eok:
                continue
            prev, avg = self.daily_info(code)
            w = Watch(code, r["name"], price, flu, exp_value_mil=avg * 0.05, bid_ask=1.0, avg20_value_mil=avg,
                      source="nxt", yday_value=r["value"] / 1e8)
            out.append(w)
        # 점수: 프리마켓 갭 적정성 + 어제 거래대금 규모
        for w in out:
            w.bid_ask = 1 + min(w.yday_value / 500, 2)   # 어제 대금 1,000억이면 만점 (finalize 가 2배까지 반영)
        return finalize(out, cfg)

    # ── 1) 실시간 스캔 ──
    def scan(self, watch_codes: set[str]):
        cfg = self.cfg
        t_now = now()
        upto = t_now.strftime("%H%M")
        gainers = {r["code"]: r for r in self.nv.ranking("up", pages=2) if is_common(r) and r["trading"]}
        leaders = [r for r in self.nv.ranking("priceTop", pages=2) if is_common(r)]
        leaders.sort(key=lambda r: r["value"], reverse=True)
        rank = {r["code"]: i + 1 for i, r in enumerate(leaders)}
        pre = []
        for code, g in gainers.items():
            if code not in rank:
                continue
            if not (cfg.min_flu_rt <= g["flu"] <= cfg.max_flu_rt) or g["price"] < cfg.min_price:
                continue
            if g["value"] / 1e6 < cfg.min_value_mil or g["mcap"] / 1e8 < cfg.min_mcap_eok:
                continue
            pre.append(g)
        pre.sort(key=lambda g: g["value"], reverse=True)
        pre = pre[: cfg.max_candidates_detail]

        def detail(g):
            code = g["code"]
            prev, avg = self.daily_info(code)
            bars = self.minutes(code, upto)
            c = from_bars(code, g["name"], bars + [{"t": upto, "o": g["price"], "h": g["price"],
                                                    "l": g["price"], "c": g["price"], "v": 0}],
                          prev, avg, g["mcap"] / 1e8, self.hi52(code), live_price=g["price"])
            if not c:
                return None
            c.value_mil = g["value"] / 1e6          # 실시간 누적 대금
            c.value_rank = rank[code]
            evaluate(c, cfg)
            if c.cntr_str < cfg.min_cntr_str and not c.reject:
                c.reject = f"매수세 {c.cntr_str:.0f}% 부족"
                c.score = 0
            if not c.reject and code in watch_codes:
                c.score = round(c.score + cfg.watch_bonus, 3)
                c.parts["장전관심"] = 1.0
            return c
        cands = [c for c in self.nv.pmap(detail, pre) if c]
        cands.sort(key=lambda c: c.score, reverse=True)
        return cands

    # ── 1') 선정 v2: 09:05 초반 거래대금 1위 (strategy.py) ──
    def pick_v2(self):
        upto = now().strftime("%H%M")
        val = [r for r in self.nv.ranking("priceTop", pages=2) if is_common(r)]
        gain = [r for r in self.nv.ranking("up", pages=2) if is_common(r)]
        rows = {r["code"]: r for r in gain + val if r["trading"]}
        allowed = self.yday_top | {r["code"] for r in val}
        pre = [r for r in rows.values() if r["code"] in allowed and 0 < r["flu"] <= 20
               and r["price"] >= self.cfg.min_price and r["mcap"] >= self.cfg.min_mcap_eok * 1e8]
        if not pre:
            pre = sorted(rows.values(), key=lambda r: -r["value"])[:30]
        pre = sorted(pre, key=lambda r: -r["value"])[:80]

        def one(r):
            bars = [b for b in self.nv.minutes(r["code"], self.day, "0900", upto) if b["t"] < upto]
            prev = r["price"] / (1 + r["flu"] / 100) if r["flu"] > -100 else 0
            return snap_from_minutes(r["code"], r["name"], bars, r["price"], prev, r["value"], r["mcap"])
        snaps = [x for x in self.nv.pmap(one, pre) if x]
        best, tier = choose(snaps, self.cfg.min_price, self.cfg.min_mcap_eok * 1e8)
        rank = 1 + sorted(snaps, key=lambda x: -x.value_early).index(best) if best else 0
        return best, tier, len(snaps), rank

    # ── 2) 실시간 추적 (현재가 3초 간격) ──
    def _bars(self, code: str) -> list[dict]:
        """네이버 1분봉을 tracker.update 형식으로 변환."""
        return [{"cntr_tm": f"{self.day}{b['t']}00", "high_pric": b["h"], "low_pric": b["l"], "cur_prc": b["c"]}
                for b in self.nv.minutes(code, self.day, "0900", "1530")]

    def track(self, pos: Position, until: datetime) -> Position:
        """진입 후 추적: 1분에 한 번 1분봉을 받아 고가·저가로 손절/목표/마일스톤 판정 (요청 최소화)."""
        cfg = self.cfg
        marks = [m for m in cfg.milestones if m < pos.pct(pos.target) and m not in pos.hit_milestones]
        while now() < until and not pos.closed:
            for kind, info in update(pos, self._bars(pos.code), marks):
                if kind == "milestone":
                    marks.remove(info["pct"])
                    self.tg.send(M.milestone(pos, info["pct"], info["tm"]))
            self.state["pos"] = pos.__dict__
            if pos.closed:
                break
            # 다음 분이 시작되고 몇 초 뒤(직전 봉이 확정된 뒤)에 다시 확인
            t = now()
            nxt = t.replace(second=0, microsecond=0) + timedelta(minutes=1, seconds=cfg.track_bar_delay_sec)
            _time.sleep(max(1.0, min((nxt - t).total_seconds(), (until - t).total_seconds())))
        return pos

    def finish(self, pos: Position, chosen_score: float, reasons: str):
        self.tg.send(M.exit_msg(pos, self.cfg.cost_pct))
        self.store.add_trade(self.day, pos, self.cfg.cost_pct, chosen_score, reasons)
        self.store.set_day(self.day, "traded")
        self.state["done"] = True
        self.state["today_line"] = f"{pos.name} {pos.exit_reason} {pos.pct(pos.exit_price) - self.cfg.cost_pct:+.2f}%"
        self.state["pos"] = pos.__dict__
        save_state(self.state)

    def archive_minutes(self):
        """오늘 아침 거래대금 상위 400종목의 09:00~09:15 분봉 저장 (네이버는 1주일치만 보관하므로 직접 쌓아둔다)."""
        try:
            top = [r for r in self.nv.ranking("priceTop", pages=2) if is_common(r)][:400]
            data = dict(self.nv.pmap(lambda r: (r["code"], self.nv.minutes(r["code"], self.day, "0900", "0915")), top))
            meta = {r["code"]: {"name": r["name"], "mcap": r["mcap"]} for r in top}
            (DATA / "minutes").mkdir(exist_ok=True)
            with gzip.open(DATA / "minutes" / f"{self.day}.json.gz", "wt", encoding="utf-8") as f:
                json.dump({"meta": meta, "bars": data}, f, ensure_ascii=False)
        except Exception as e:
            log.warning("분봉 저장 실패: %s", e)

    # ── 아침 작업 ──
    def morning(self):
        cfg, st = self.cfg, self.state
        if now().weekday() >= 5:
            return log.info("주말")
        if st.get("morning_done"):
            return log.info("오늘 아침 작업은 이미 실행됨 (중복 트리거)")
        st["morning_started"] = now().isoformat()
        save_state(st)

        wait_until(at(cfg.premarket_time))          # 08:45 (NXT 프리마켓 끝나기 전)
        watches = []
        try:
            watches = self.premarket()
        except Exception as e:
            log.warning("관심종목 실패: %s", e)
        self.tg.send(M.morning(now(), summarize(self.store, cfg.notional), cfg))
        for w in watches:
            self.store.add_watch(self.day, w)
        self.tg.send(M.premarket(watches, cfg))

        # 개장 확인 (수능날 등 늦은 개장 자동 대응: 최대 10:05까지 기다림)
        wait_until(at(cfg.open_time, 0.05))
        deadline_open = at(time(10, 5))
        while not self.market_open():
            if now() > deadline_open:
                self.store.set_day(self.day, "holiday", "개장 안 함")
                st["morning_done"] = st["done"] = True
                save_state(st)
                return log.info("휴장일")
            _time.sleep(5)
        open_dt = now().replace(second=0, microsecond=0)
        # 개장 5분째(09:05)에 판단: 09:01~09:04 완성봉 거래대금 기준. 늦어질수록 성과가 떨어지므로 바로 확정한다.
        wait_until(open_dt + timedelta(minutes=cfg.pick_after_min, seconds=1))
        best, tier, n = None, "", 0
        for _ in range(6):                      # 데이터 오류 시 최대 약 30초 재시도
            try:
                best, tier, n, rank = self.pick_v2()
                if best:
                    break
            except Exception as e:
                log.warning("선정 오류: %s", e)
            _time.sleep(5)
        if not best:
            self.store.set_day(self.day, "error", "선정 데이터 없음")
            self.tg.send("⚠️ 시세 데이터를 받지 못해 오늘 종목을 고르지 못했습니다.")
            st.update(morning_done=True, done=True, today_line="데이터 오류")
            save_state(st)
            return
        chosen = to_candidate(best, rank)
        entry, stop, target, stop_pct = plan(chosen, cfg)
        ts = now().strftime("%Y%m%d%H%M%S")
        pos = Position(chosen.code, chosen.name, entry, stop, target, ts)
        self.store.mark_picked(self.day, chosen.code)
        self.tg.send(M.signal_v2(best, chosen, entry, stop, target, stop_pct, tier, n, ts, cfg,
                                 best.code in {w.code for w in watches}))
        chosen.score = 1.0
        reasons = f"tier={tier}, early_value={best.value_early/1e8:.1f}억"
        st.update(pos=pos.__dict__, score=chosen.score, reasons=reasons, morning_done=True)
        save_state(st)

        archived = False
        pos = self.track(pos, at(time(9, 16)))
        if not pos.closed:
            self.archive_minutes()
            archived = True
            pos = self.track(pos, at(cfg.morning_cutoff))
        if pos.closed:
            self.finish(pos, chosen.score, reasons)
        else:
            save_state(st)        # 오후 작업이 이어서 추적
        if not archived:
            wait_until(at(time(9, 16)))
            self.archive_minutes()

    # ── 오후 작업 ──
    def afternoon(self):
        cfg, st = self.cfg, self.state
        if now().weekday() >= 5:
            return
        if st.get("report_sent"):
            return log.info("오늘 리포트는 이미 보냄")
        if st.get("pos") and not st.get("done"):
            saved = {k: v for k, v in st["pos"].items() if k in Position.__dataclass_fields__}
            pos = Position(**saved)
            pos.max_price, pos.min_price = saved["max_price"], saved["min_price"]
            # 오전 작업 종료~지금 사이 공백 구간은 1분봉으로 손절/목표 확인
            update(pos, self._bars(pos.code))
            pos = self.track(pos, at(cfg.force_exit_time))
            if not pos.closed:
                b = self.nv.basic(pos.code)       # 청산가만 현재가 1회로 확인
                p = float(str(b.get("closePrice", "0")).replace(",", "") or 0) or pos.last_price
                force_exit(pos, p, now().strftime("%Y%m%d%H%M%S"))
            self.finish(pos, st.get("score", 0), st.get("reasons", ""))
        if not st.get("morning_started"):
            return log.info("오늘 아침 작업이 없었음 — 리포트 생략")
        wait_until(at(cfg.report_time))
        for w in self.store.watches(self.day):
            try:
                b = self.nv.minutes(w["code"], self.day, "0900", "1530")
                if b:
                    self.store.set_watch_result(self.day, w["code"], b[0]["o"], b[-1]["c"], max(x["h"] for x in b))
            except Exception as e:
                log.warning("관심종목 결과 실패 %s", e)
        t = now()
        if self.store.day_counts().get("holiday") and not st.get("pos"):
            return
        self.tg.send(M.report(t, summarize(self.store, cfg.notional), summarize(self.store, cfg.notional, t.strftime("%Y%m")),
                              st.get("today_line", "-"), self.store.recent_days(5), self.store.watches(self.day),
                              watch_summary(self.store), cfg))
        st["report_sent"] = True
        save_state(st)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = Config()
    live = Live(cfg)
    cmd = (sys.argv[1:] or ["scan-once"])[0]
    if cmd == "morning":
        live.morning()
    elif cmd == "afternoon":
        live.afternoon()
    elif cmd == "test-telegram":
        live.tg.send(f"✅ <b>{cfg.channel_name}</b> 봇 연결 테스트 (GitHub Actions)")
    elif cmd == "find-chat-id":
        import requests
        tok = cfg.tg_token
        print(f"토큰 길이 {len(tok)}, 공백/줄바꿈 포함: {tok != tok.strip() or chr(10) in tok}")
        me = requests.get(f"https://api.telegram.org/bot{tok.strip()}/getMe", timeout=10).json()
        print("getMe:", me.get("ok"), (me.get("result") or {}).get("username"), me.get("description", ""))
        up = requests.get(f"https://api.telegram.org/bot{tok.strip()}/getUpdates", timeout=10).json()
        print("getUpdates:", up.get("ok"), "업데이트", len(up.get("result", [])), "건", up.get("description", ""))
        for u in up.get("result", []):
            for key in ("my_chat_member", "channel_post", "message"):
                chat = (u.get(key) or {}).get("chat")
                if chat:
                    print(f"  {key}: {chat.get('type')} {chat.get('title', '')}")
        for cid, title in live.tg.find_chats():
            print(f"채널 발견: {title}: {cid}")
    else:
        t0 = _time.time()
        cands = live.scan(set())
        print(f"스캔 {(_time.time() - t0):.1f}초, 상세 평가 {len(cands)}건")
        for c in cands:
            print(f"{c.score:5.2f} {c.name:<12} {c.code} {c.flu_rt:+6.2f}% 대금 {c.value_mil/100:6.0f}억 "
                  f"배수 {c.rvol:4.2f} 매수세 {c.cntr_str:5.0f} {c.reject}")


if __name__ == "__main__":
    main()
