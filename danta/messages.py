"""텔레그램 메시지 문구 (HTML 모드)."""
from __future__ import annotations

from datetime import datetime
from html import escape

from .scanner import Candidate
from .stats import Summary
from .tracker import Position

WD = "월화수목금토일"
LINE = "━━━━━━━━━━━━━━"


def _won(x: float) -> str:
    return f"{x:,.0f}"


def _signed(x: float, d: int = 2) -> str:
    return f"{x:+.{d}f}"


def _hm(tm: str) -> str:
    return f"{tm[8:10]}:{tm[10:12]}" if len(tm) >= 12 else tm


def morning(now: datetime, s: Summary, cfg) -> str:
    rec = (f"누적 {s.n}전 {s.wins}승 {s.losses}패 · 승률 {s.win_rate:.0f}% · 손익비 {_pf(s)}"
           if s.n else "아직 기록 없음 (오늘이 첫 기록)")
    return (f"📅 <b>{now:%Y-%m-%d} ({WD[now.weekday()]})</b> {escape(cfg.channel_name)}\n{LINE}\n"
            f"{cfg.open_time:%H:%M}~{cfg.scan_minutes}분간 조건 검색 → 통과 종목이 없으면 오늘은 쉽니다.\n"
            f"{rec}\n\n<i>투자 참고용 자동 시그널이며 실제 체결 기록이 아닙니다.</i>")


def signal(c: Candidate, entry: int, stop: int, target: int, stop_pct: float, tm: str, cfg) -> str:
    rr = cfg.reward_risk
    why = [
        f"거래대금 {c.value_mil / 100:,.0f}억 (전체 {c.value_rank}위)",
        f"이미 20일 평균 하루 거래대금의 {c.rvol:.2f}배" if c.avg20_value_mil else "20일 평균 데이터 없음",
        f"매수세 강도 {c.cntr_str:.0f}%",
        f"VWAP {_won(c.vwap)}원 위 · 고가 대비 -{c.pullback_pct:.1f}%",
    ]
    if c.parts.get("장전관심"):
        why.insert(0, "🔔 장전 관심종목에서 조건 통과")
    if c.hi250:
        why.append(f"52주 고가 대비 {(c.price / c.hi250 - 1) * 100:+.1f}%")
    return (f"🎯 <b>오늘의 종목</b> {_hm(tm)}\n{LINE}\n"
            f"<b>{escape(c.name)}</b> ({c.code})\n"
            f"시그널 가격 : <b>{_won(entry)}원</b> ({_signed(c.flu_rt)}%)\n"
            f"손절가 : {_won(stop)}원 (-{stop_pct:.1f}%)\n"
            f"목표가 : {_won(target)}원 (+{stop_pct * rr:.1f}%)\n{LINE}\n"
            f"<b>근거</b> (점수 {c.score:.2f})\n" + "\n".join(f"· {w}" for w in why) +
            "\n\n손절가 이탈 시 즉시 종료, 15:20 남아 있으면 청산 기준으로 기록합니다.")


def no_signal(rejected: list[Candidate], cfg) -> str:
    lines = [f"· {escape(c.name)}: {escape(c.reject or f'점수 {c.score:.2f} 미달')}" for c in rejected[:5]]
    body = "\n".join(lines) if lines else "· 1차 조건(상승률·거래대금·체결강도) 통과 종목 없음"
    return (f"😴 <b>오늘은 쉽니다</b>\n{LINE}\n{cfg.open_time:%H:%M}~{cfg.scan_minutes}분 동안 "
            f"기준(점수 {cfg.min_score}) 넘는 종목이 없었습니다.\n\n<b>탈락 사유 상위</b>\n{body}\n\n"
            "억지로 고르지 않는 것도 전략입니다.")


def milestone(pos: Position, pct: float, tm: str) -> str:
    return (f"📈 <b>{escape(pos.name)}</b> +{pct:.0f}% 터치 ({_hm(tm)})\n"
            f"시그널 {_won(pos.entry)} → 고가 {_won(pos.max_price)}원\n"
            f"손절 {_won(pos.stop)} / 목표 {_won(pos.target)}")


def vi(pos: Position, row: dict) -> str:
    kind = row.get("viaplc_tp", "")
    price = str(row.get("motn_pric", "")).lstrip("+-")
    t = str(row.get("trde_cntr_proc_time", ""))
    return (f"⏸ <b>{escape(pos.name)}</b> {escape(kind)} VI 발동 {t[:2]}:{t[2:4]}\n"
            f"발동가 {price}원 · 2분간 단일가 매매")


def exit_msg(pos: Position, cost_pct: float) -> str:
    g = pos.pct(pos.exit_price)
    icon = {"목표가 도달": "✅", "손절": "🛑"}.get(pos.exit_reason, "🔔")
    return (f"{icon} <b>{escape(pos.name)} {pos.exit_reason}</b> {_hm(pos.exit_tm)}\n{LINE}\n"
            f"{_won(pos.entry)} → {_won(pos.exit_price)}원\n"
            f"수익률 {_signed(g)}% (비용 {cost_pct}% 차감 시 {_signed(g - cost_pct)}%)\n"
            f"보유 중 최고 {_signed(pos.pct(pos.max_price))}% / 최저 {_signed(pos.pct(pos.min_price))}%")


def _pf(s: Summary) -> str:
    return "∞" if s.profit_factor == float("inf") else f"{s.profit_factor:.2f}"


def premarket(ws, cfg) -> str:
    if not ws:
        return (f"🔔 <b>장전 관심종목</b> {cfg.premarket_time:%H:%M}\n{LINE}\n"
                "동시호가에서 조건에 맞는 종목이 없습니다. 9시 이후 장중 흐름으로 고릅니다.")
    if ws[0].source == "nxt":
        lines = [f"{i}. <b>{escape(w.name)}</b> ({w.code})\n"
                 f"   NXT 프리마켓 {_won(w.exp_price)}원 ({_signed(w.flu_rt)}%) · 어제 거래대금 {w.yday_value:,.0f}억"
                 for i, w in enumerate(ws, 1)]
        return (f"🔔 <b>장전 관심종목</b> {cfg.premarket_time:%H:%M} (넥스트레이드 프리마켓 기준)\n{LINE}\n" +
                "\n".join(lines) + f"\n{LINE}\n9시 이후 이 중 조건을 통과하면 🎯 종목으로 확정합니다. 시가 추격 매수는 주의하세요.")
    lines = []
    for i, w in enumerate(ws, 1):
        ratio = f" · 평소 하루치의 {w.ratio * 100:.0f}%" if w.avg20_value_mil else ""
        lines.append(f"{i}. <b>{escape(w.name)}</b> ({w.code})\n"
                     f"   예상 {_won(w.exp_price)}원 ({_signed(w.flu_rt)}%) · 예상대금 {w.exp_value_mil / 100:,.1f}억{ratio}\n"
                     f"   매수잔량/매도잔량 {w.bid_ask:.1f}배")
    return (f"🔔 <b>장전 관심종목</b> {cfg.premarket_time:%H:%M} (동시호가 기준)\n{LINE}\n" + "\n".join(lines) +
            f"\n{LINE}\n9시 이후 이 중 조건을 통과하면 🎯 종목으로 확정합니다. 시가 추격 매수는 주의하세요.")


def _pf(s: Summary) -> str:
    return "∞" if s.profit_factor == float("inf") else f"{s.profit_factor:.2f}"


def _block(title: str, s: Summary, cfg) -> str:
    if not s.n:
        return f"<b>{title}</b> 매매 기록 없음"
    return (f"<b>{title}</b> {s.n}회 · {s.wins}승 {s.losses}패 · 승률 {s.win_rate:.0f}%\n"
            f"평균 {_signed(s.avg_win)}% / {_signed(s.avg_loss)}% · PF {_pf(s)}\n"
            f"누적 {_signed(s.pnl_won / 10000, 1)}만원 · 최대낙폭 -{s.mdd_won / 10000:,.1f}만원")


def report(now: datetime, s_all: Summary, s_month: Summary, today_line: str, recent: list,
           watch_today: list, wsum: dict, cfg) -> str:
    parts = [f"📊 <b>마감 리포트</b> {now:%m/%d} ({WD[now.weekday()]})", LINE, f"<b>오늘</b> {escape(today_line)}"]

    if watch_today:
        parts.append("\n<b>장전 관심종목 결과</b> (시가→종가)")
        for w in watch_today:
            if w.get("open_price") and w.get("close_price"):
                r = (w["close_price"] / w["open_price"] - 1) * 100
                hi = (w["high_price"] / w["open_price"] - 1) * 100 if w.get("high_price") else 0
                mark = " 🎯" if w.get("picked") else ""
                parts.append(f"· {escape(w['name'])}{mark} {_signed(r)}% (장중 최고 {_signed(hi)}%)")

    if recent:
        parts.append(f"\n<b>최근 {len(recent)}거래일</b>")
        for d in recent:
            t = d["trade"]
            day = f"{d['day'][4:6]}/{d['day'][6:]}"
            if t:
                icon = "✅" if t["net_pct"] > 0 else "🛑"
                parts.append(f"{icon} {day} {escape(t['name'])} {_signed(t['net_pct'])}% ({t['exit_reason']})")
            else:
                parts.append(f"😴 {day} 쉼")

    parts += [LINE, _block(f"이번 달({now:%m월})", s_month, cfg), "", _block("전체 누적", s_all, cfg)]
    if s_all.n:
        parts.append(f"쉰 날 {s_all.no_signal_days}일 · 기록 시작 {s_all.since[:4]}-{s_all.since[4:6]}-{s_all.since[6:]}")
    if wsum.get("n"):
        parts.append(f"\n<b>장전 관심종목 누적</b> {wsum['n']}건 · 종가 상승 {wsum['up']}건 "
                     f"({wsum['up'] / wsum['n'] * 100:.0f}%) · 평균 {_signed(wsum['avg'])}% · "
                     f"장중 +3% 터치 {wsum['hit3'] / wsum['n'] * 100:.0f}%")
    parts.append(f"\n<i>1회 {cfg.notional // 10000:,}만원 고정 · 비용 {cfg.cost_pct}% 차감 · 손실 포함 전체 기록 · 실제 체결 아님</i>")
    return "\n".join(parts)
