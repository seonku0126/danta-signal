"""하루 실행 흐름. 평일 08:40쯤 cron/systemd 로 띄우면 장 마감 리포트 후 스스로 종료한다.

사용법:
  python -m danta.bot                # 실제 운영
  python -m danta.bot --dry-run      # 텔레그램 대신 콘솔 출력
  python -m danta.bot --scan-once    # 지금 시점 후보·점수 표만 출력 (장중 점검용)
  python -m danta.bot --test-telegram
"""
from __future__ import annotations

import argparse
import logging
import time as _time
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

from . import messages as M
from .config import Config
from .kiwoom import Kiwoom, num
from .premarket import finalize, first_pass
from .scanner import Candidate, enrich, evaluate, merge_rankings, pick_best, plan
from .stats import Store, summarize, watch_summary
from .telegram import Telegram
from .tracker import Position, force_exit, update

KST = ZoneInfo("Asia/Seoul")
log = logging.getLogger("danta")


class RealClock:
    def now(self) -> datetime:
        return datetime.now(KST).replace(tzinfo=None)

    def sleep_until(self, t: datetime):
        while (d := (t - self.now()).total_seconds()) > 0:
            _time.sleep(min(d, 30))

    def sleep(self, sec: float):
        _time.sleep(sec)


def _at(day: datetime, t: time, plus_min: int = 0) -> datetime:
    return datetime.combine(day.date(), t) + timedelta(minutes=plus_min)


class Scanner:
    """스캔마다 순위 → 후보 상세 조회 → 평가. 하루 동안 안 바뀌는 데이터(20일 평균, 감리)는 캐시."""

    def __init__(self, api: Kiwoom, cfg: Config, today: str, watch_codes: set | None = None):
        self.api, self.cfg, self.today = api, cfg, today
        self.watch_codes = watch_codes or set()
        self._static: dict[str, tuple[dict, list]] = {}

    def run(self) -> tuple[list[Candidate], int]:
        gainers, leaders = self.api.gainers(), self.api.value_leaders()
        raw = len(gainers) + len(leaders)
        out = []
        for c in merge_rankings(gainers, leaders, self.cfg):
            try:
                if c.code not in self._static:
                    self._static[c.code] = (self.api.stock_info(c.code), self.api.daily_bars(c.code, self.today))
                info, daily = self._static[c.code]
                enrich(c, self.api.basic(c.code), info, self.api.minute_bars(c.code, self.today), daily, self.today)
                evaluate(c, self.cfg)
                if not c.reject and c.code in self.watch_codes:  # 장전에 미리 알린 종목이 조건까지 통과
                    c.score = round(c.score + self.cfg.watch_bonus, 3)
                    c.parts["장전관심"] = 1.0
                out.append(c)
            except Exception as e:  # 한 종목 실패가 전체를 멈추지 않게
                log.warning("%s 상세 조회 실패: %s", c.code, e)
        out.sort(key=lambda c: c.score, reverse=True)
        return out, raw


def run_day(cfg: Config, api: Kiwoom, tg: Telegram, store: Store, clock=None):
    clock = clock or RealClock()
    now = clock.now()
    today = now.strftime("%Y%m%d")
    if now.weekday() >= 5:
        log.info("주말 — 종료")
        return
    open_dt = _at(now, cfg.open_time)

    clock.sleep_until(open_dt - timedelta(minutes=10))
    tg.send(M.morning(clock.now(), summarize(store, cfg.notional), cfg))

    # ── 0) 장전 동시호가 관심종목 ──
    clock.sleep_until(_at(now, cfg.premarket_time))
    watches = premarket_watch(api, cfg, today)
    for w in watches:
        store.add_watch(today, w)
    tg.send(M.premarket(watches, cfg))

    # ── 1) 장 초반 스캔 ──
    scanner = Scanner(api, cfg, today, {w.code for w in watches})
    deadline = open_dt + timedelta(minutes=cfg.scan_minutes)
    clock.sleep_until(open_dt + timedelta(minutes=1))
    prev_code, last_best, last_all, chosen, empty_scans = None, None, [], None, 0
    while clock.now() <= deadline:
        try:
            cands, raw = scanner.run()
        except Exception as e:
            log.error("스캔 실패: %s", e)
            cands, raw = [], -1
        if raw == 0:
            empty_scans += 1
            if empty_scans >= 3:  # 순위 데이터 자체가 없으면 휴장일로 판단
                store.set_day(today, "holiday", "순위 데이터 없음")
                log.info("휴장일로 판단 — 종료")
                return
        last_all = cands or last_all
        best = pick_best(cands, cfg)
        log.info("스캔 %s: 후보 %d, 최고 %s", clock.now().strftime("%H:%M:%S"), len(cands),
                 f"{best.name}({best.score})" if best else "-")
        if best and best.code == prev_code:  # 두 번 연속 1등이면 확정
            chosen = best
            break
        prev_code, last_best = (best.code if best else None), best
        clock.sleep(cfg.scan_interval)
    chosen = chosen or last_best

    if not chosen:
        store.set_day(today, "no_signal")
        tg.send(M.no_signal([c for c in last_all if c.reject or c.score < cfg.min_score], cfg))
        _report(cfg, tg, store, clock, "시그널 없음 (쉼)", api)
        return

    # ── 2) 시그널 발송 ──
    entry, stop, target, stop_pct = plan(chosen, cfg)
    sig_tm = clock.now().strftime("%Y%m%d%H%M%S")
    pos = Position(chosen.code, chosen.name, entry, stop, target, sig_tm)
    store.mark_picked(today, chosen.code)
    tg.send(M.signal(chosen, entry, stop, target, stop_pct, sig_tm, cfg))
    marks = tuple(m for m in cfg.milestones if m < stop_pct * cfg.reward_risk)  # 목표 이전 구간만 알림

    # ── 3) 추적 ──
    exit_dt = _at(now, cfg.force_exit_time)
    next_vi = clock.now()
    while clock.now() < exit_dt and not pos.closed:
        try:
            for kind, info in update(pos, api.minute_bars(pos.code, today), marks):
                if kind == "milestone":
                    tg.send(M.milestone(pos, info["pct"], info["tm"]))
            if not pos.closed and clock.now() >= next_vi:
                next_vi = clock.now() + timedelta(seconds=cfg.vi_interval)
                rows = api.vi(pos.code)
                cnt = max((int(num(r.get("vimotn_cnt"))) for r in rows), default=0)
                if cnt > pos.vi_count and rows:
                    pos.vi_count = cnt
                    latest = max(rows, key=lambda r: str(r.get("trde_cntr_proc_time", "")))
                    tg.send(M.vi(pos, latest))
        except Exception as e:
            log.warning("추적 조회 실패: %s", e)
        if not pos.closed:
            clock.sleep(cfg.track_interval)

    if not pos.closed:
        try:
            update(pos, api.minute_bars(pos.code, today), ())
        except Exception as e:
            log.warning("청산 전 조회 실패: %s", e)
        if not pos.closed:
            force_exit(pos, pos.last_price, clock.now().strftime("%Y%m%d%H%M%S"))
    tg.send(M.exit_msg(pos, cfg.cost_pct))

    reasons = ", ".join(f"{k}={v:.2f}" for k, v in chosen.parts.items())
    store.add_trade(today, pos, cfg.cost_pct, chosen.score, reasons)
    store.set_day(today, "traded")
    net = pos.pct(pos.exit_price) - cfg.cost_pct
    _report(cfg, tg, store, clock, f"{chosen.name} {pos.exit_reason} {net:+.2f}%", api)


def premarket_watch(api, cfg, today: str):
    try:
        ws = first_pass(api.expected_gainers(), cfg)
        for w in ws:
            info = api.stock_info(w.code)
            audit = str(info.get("auditInfo", "정상")).strip() or "정상"
            warn = str(info.get("orderWarning", "0")).strip() or "0"
            w.warning = "" if (audit == "정상" and warn in ("0", "")) else audit
            past = [d for d in api.daily_bars(w.code, today) if str(d.get("dt")) != today][:20]
            vals = [num(d.get("trde_prica"), True) for d in past]
            w.avg20_value_mil = sum(vals) / len(vals) if vals else 0.0
        return finalize(ws, cfg)
    except Exception as e:
        log.warning("장전 관심종목 조회 실패: %s", e)
        return []


def _report(cfg, tg, store, clock, today_line: str, api=None):
    clock.sleep_until(_at(clock.now(), cfg.report_time))
    now = clock.now()
    today = now.strftime("%Y%m%d")
    if api:
        for w in store.watches(today):
            try:
                b = api.basic(w["code"])
                store.set_watch_result(today, w["code"], num(b.get("open_pric"), True),
                                       num(b.get("cur_prc"), True), num(b.get("high_pric"), True))
            except Exception as e:
                log.warning("관심종목 종가 조회 실패 %s: %s", w["code"], e)
    tg.send(M.report(now, summarize(store, cfg.notional), summarize(store, cfg.notional, now.strftime("%Y%m")),
                     today_line, store.recent_days(5), store.watches(today), watch_summary(store), cfg))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--scan-once", action="store_true")
    ap.add_argument("--test-telegram", action="store_true")
    ap.add_argument("--find-chat-id", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = Config()
    tg = Telegram(cfg.tg_token, cfg.tg_chat_id, dry_run=a.dry_run)
    if a.find_chat_id:
        found = tg.find_chats()
        if not found:
            print("못 찾았어요. 봇을 채널 관리자로 추가한 뒤 채널에 아무 글이나 하나 올리고 다시 실행하세요.")
        for cid, title in found:
            print(f"{title}: TELEGRAM_CHAT_ID={cid}")
        return
    if a.test_telegram:
        tg.send(f"✅ <b>{cfg.channel_name}</b> 봇 연결 테스트")
        return
    if not (cfg.kiwoom_appkey and cfg.kiwoom_secret):
        raise SystemExit(".env 에 KIWOOM_APPKEY / KIWOOM_SECRETKEY 를 넣어주세요.")
    api = Kiwoom(cfg.kiwoom_base, cfg.kiwoom_appkey, cfg.kiwoom_secret)
    if a.scan_once:
        today = RealClock().now().strftime("%Y%m%d")
        cands, raw = Scanner(api, cfg, today).run()
        print(f"순위 원자료 {raw}건 → 상세 평가 {len(cands)}건")
        for c in cands:
            print(f"{c.score:5.2f} {c.name:<12} {c.code} {c.flu_rt:+6.2f}% 대금 {c.value_mil/100:6.0f}억 "
                  f"배수 {c.rvol:4.2f} 체강 {c.cntr_str:5.0f} {c.reject}")
        return
    run_day(cfg, api, tg, Store(cfg.db_path))


if __name__ == "__main__":
    main()
