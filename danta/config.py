"""설정값. 비밀값(키, 토큰)은 .env 에서만 읽는다."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:  # python-dotenv 없으면 환경변수만 사용
    pass


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _t(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


@dataclass
class Config:
    # ── 키움 ──────────────────────────────────────────
    kiwoom_appkey: str = field(default_factory=lambda: _env("KIWOOM_APPKEY"))
    kiwoom_secret: str = field(default_factory=lambda: _env("KIWOOM_SECRETKEY"))
    # 시세 조회는 실전 서버가 정확하다. 모의 서버는 순위 TR 데이터가 비어 있을 수 있음.
    kiwoom_mock: bool = field(default_factory=lambda: _env("KIWOOM_MOCK", "0") == "1")

    # ── 텔레그램 ──────────────────────────────────────
    tg_token: str = field(default_factory=lambda: _env("TELEGRAM_BOT_TOKEN"))
    tg_chat_id: str = field(default_factory=lambda: _env("TELEGRAM_CHAT_ID"))
    channel_name: str = field(default_factory=lambda: _env("CHANNEL_NAME", "단타 시그널"))

    # ── 시간표 (거래소 시간 변경일엔 .env 로 덮어쓰기: 수능날·연초 첫날 10시 개장 등) ──
    premarket_time: time = field(default_factory=lambda: _t(_env("PREMARKET_TIME", "08:55")))
    open_time: time = field(default_factory=lambda: _t(_env("OPEN_TIME", "09:00")))
    scan_minutes: int = field(default_factory=lambda: int(_env("SCAN_MINUTES", "10")))
    force_exit_time: time = field(default_factory=lambda: _t(_env("FORCE_EXIT_TIME", "15:20")))
    report_time: time = field(default_factory=lambda: _t(_env("REPORT_TIME", "15:35")))

    # ── 후보 필터 ─────────────────────────────────────
    min_flu_rt: float = 3.0          # 너무 안 오른 건 모멘텀 부족
    max_flu_rt: float = 15.0         # 상한가 근처 추격 금지
    max_gap_pct: float = 10.0        # 시가 갭이 너무 크면 제외
    min_value_mil: float = 3000      # 누적 거래대금 하한 (백만원) = 30억
    min_mcap_eok: float = 500        # 시총 500억 미만 제외 (작전·동전주 회피)
    min_price: int = 1000
    min_cntr_str: float = 110.0      # 체결강도
    max_pullback_pct: float = 3.0    # 당일 고가 대비 이미 3% 넘게 밀렸으면 제외
    min_rvol: float = 0.25           # 오늘 거래대금 / 20일 평균 일거래대금 (장 초반 기준)
    min_score: float = 0.55          # 이 점수 미만이면 그날은 쉰다
    max_candidates_detail: int = 15  # 상세 조회할 후보 수 (API 호출 절약)

    # ── 장전 관심종목 (동시호가) ──────────────────────
    pre_min_flu: float = 2.0
    pre_max_flu: float = 12.0
    pre_min_value_mil: float = 300   # 예상체결대금 3억 이상
    pre_count: int = 3
    watch_bonus: float = 0.05        # 장전 관심종목이 9시 조건도 통과하면 가산점

    # ── 진입/청산 규칙 ────────────────────────────────
    stop_min_pct: float = 2.0
    stop_max_pct: float = 4.0
    reward_risk: float = 2.0         # 목표 = 손절폭 × 2
    cost_pct: float = 0.35           # 왕복 비용 가정: 거래세 0.20%(2026) + 수수료 약 0.03% + 슬리피지 약 0.12%
    notional: int = 10_000_000       # 성적 계산용 1회 매매 금액 (복리 X, 고정)
    milestones: tuple = (3.0, 5.0, 7.0, 10.0, 15.0)

    # ── GitHub Actions(네이버) 실시간 설정 ─────────────
    scan_poll_sec: float = 2.0       # 아침 스캔 간격(초) — 한 번 도는 데 2~4초 더 걸림
    confirm_polls: int = 3           # 같은 1등이 3번 연속이면 확정 (약 10~15초)
    track_poll_sec: float = 3.0      # 진입 후 현재가 확인 간격(초)
    morning_cutoff: time = field(default_factory=lambda: _t(_env("MORNING_CUTOFF", "14:20")))

    # ── 폴링 주기 (초) ────────────────────────────────
    scan_interval: int = 30
    track_interval: int = 30
    vi_interval: int = 60

    db_path: str = field(
        default_factory=lambda: _env("DB_PATH", str(Path(__file__).resolve().parent.parent / "danta.db"))
    )

    @property
    def kiwoom_base(self) -> str:
        return "https://mockapi.kiwoom.com" if self.kiwoom_mock else "https://api.kiwoom.com"
