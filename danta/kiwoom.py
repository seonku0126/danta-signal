"""키움 REST API 최소 클라이언트 (시세 조회 전용 — 주문 기능 없음)."""
from __future__ import annotations

import logging
import time as _time
from datetime import datetime, timedelta

import requests

log = logging.getLogger(__name__)


def num(v, absolute: bool = False) -> float:
    """키움 숫자 문자열("+74800", "-1.25", "000123") → float. 가격은 부호가 '전일대비 방향'이라 abs 필요."""
    if v is None:
        return 0.0
    s = str(v).replace(",", "").strip()
    if s in ("", "-", "+"):
        return 0.0
    try:
        x = float(s)
    except ValueError:
        return 0.0
    return abs(x) if absolute else x


class KiwoomError(RuntimeError):
    pass


class Kiwoom:
    MIN_GAP = 0.25  # 초당 4건 이하로 호출

    def __init__(self, base: str, appkey: str, secret: str, session: requests.Session | None = None):
        self.base = base
        self.appkey = appkey
        self.secret = secret
        self.s = session or requests.Session()
        self._token: str | None = None
        self._token_exp: datetime | None = None
        self._last = 0.0

    # ── 인증 ──
    def _ensure_token(self):
        if self._token and self._token_exp and datetime.now() < self._token_exp - timedelta(minutes=10):
            return
        r = self.s.post(
            f"{self.base}/oauth2/token",
            json={"grant_type": "client_credentials", "appkey": self.appkey, "secretkey": self.secret},
            headers={"Content-Type": "application/json;charset=UTF-8"},
            timeout=10,
        )
        r.raise_for_status()
        d = r.json()
        if not d.get("token"):
            raise KiwoomError(f"토큰 발급 실패: {d.get('return_msg')}")
        self._token = d["token"]
        try:
            self._token_exp = datetime.strptime(d.get("expires_dt", ""), "%Y%m%d%H%M%S")
        except ValueError:
            self._token_exp = datetime.now() + timedelta(hours=12)

    # ── 공통 호출 ──
    def call(self, api_id: str, path: str, body: dict, pages: int = 1) -> list[dict]:
        """연속조회 포함 호출. 페이지별 응답 dict 목록을 돌려준다."""
        out, cont, key = [], None, None
        for _ in range(pages):
            d, cont, key = self._post(api_id, path, body, cont, key)
            out.append(d)
            if cont != "Y":
                break
        return out

    def _post(self, api_id, path, body, cont=None, key=None, _retry=3):
        self._ensure_token()
        gap = _time.monotonic() - self._last
        if gap < self.MIN_GAP:
            _time.sleep(self.MIN_GAP - gap)
        h = {
            "Content-Type": "application/json;charset=UTF-8",
            "authorization": f"Bearer {self._token}",
            "api-id": api_id,
        }
        if cont == "Y":
            h["cont-yn"], h["next-key"] = "Y", key or ""
        r = self.s.post(f"{self.base}{path}", json=body, headers=h, timeout=10)
        self._last = _time.monotonic()
        if r.status_code == 429 and _retry:
            _time.sleep(1.5)
            return self._post(api_id, path, body, cont, key, _retry - 1)
        if r.status_code == 401 and _retry:
            self._token = None
            return self._post(api_id, path, body, cont, key, _retry - 1)
        r.raise_for_status()
        d = r.json()
        rc = d.get("return_code", 0)
        if rc not in (0, "0"):
            raise KiwoomError(f"{api_id} 오류 {rc}: {d.get('return_msg')}")
        return d, r.headers.get("cont-yn"), r.headers.get("next-key")

    # ── 사용하는 TR들 ──
    def gainers(self) -> list[dict]:
        """ka10027 전일대비 등락률 상위 (ETF·ETN 제외, 상하한 제외, 1천원 이상, 거래대금 10억 이상)."""
        body = {
            "mrkt_tp": "000", "sort_tp": "1", "trde_qty_cnd": "0000", "stk_cnd": "16",
            "crd_cnd": "0", "updown_incls": "0", "pric_cnd": "8", "trde_prica_cnd": "100",
            "stex_tp": "1",
        }
        rows = []
        for d in self.call("ka10027", "/api/dostk/rkinfo", body, pages=2):
            rows += d.get("pred_pre_flu_rt_upper", []) or []
        return rows

    def expected_gainers(self) -> list[dict]:
        """ka10029 장전 동시호가 예상체결 등락률 상위 (ETF·ETN 제외, 1천원 이상, 예상체결량 1만주 이상)."""
        body = {"mrkt_tp": "000", "sort_tp": "1", "trde_qty_cnd": "10", "stk_cnd": "16",
                "crd_cnd": "0", "pric_cnd": "8", "stex_tp": "1"}
        rows = []
        for d in self.call("ka10029", "/api/dostk/rkinfo", body, pages=2):
            rows += d.get("exp_cntr_flu_rt_upper", []) or []
        return rows

    def value_leaders(self) -> list[dict]:
        """ka10032 거래대금 상위 (관리종목 제외)."""
        body = {"mrkt_tp": "000", "mang_stk_incls": "0", "stex_tp": "1"}
        rows = []
        for d in self.call("ka10032", "/api/dostk/rkinfo", body, pages=2):
            rows += d.get("trde_prica_upper", []) or []
        return rows

    def basic(self, code: str) -> dict:
        """ka10001 주식기본정보 (시총, 시/고/저, 250일 고가 등)."""
        return self.call("ka10001", "/api/dostk/stkinfo", {"stk_cd": code})[0]

    def stock_info(self, code: str) -> dict:
        """ka10100 종목정보 (감리구분·투자유의 여부)."""
        return self.call("ka10100", "/api/dostk/stkinfo", {"stk_cd": code})[0]

    def minute_bars(self, code: str, day: str) -> list[dict]:
        """ka10080 1분봉 (최신순). day=YYYYMMDD 의 봉만 걸러서 돌려준다."""
        d = self.call("ka10080", "/api/dostk/chart",
                      {"stk_cd": code, "tic_scope": "1", "upd_stkpc_tp": "1", "base_dt": day})[0]
        bars = d.get("stk_min_pole_chart_qry", []) or []
        return [b for b in bars if str(b.get("cntr_tm", "")).startswith(day)]

    def daily_bars(self, code: str, day: str) -> list[dict]:
        """ka10081 일봉 (최신순)."""
        d = self.call("ka10081", "/api/dostk/chart",
                      {"stk_cd": code, "base_dt": day, "upd_stkpc_tp": "1"})[0]
        return d.get("stk_dt_pole_chart_qry", []) or []

    def vi(self, code: str) -> list[dict]:
        """ka10054 해당 종목 VI 발동 내역."""
        body = {
            "mrkt_tp": "000", "bf_mkrt_tp": "1", "stk_cd": code, "motn_tp": "0",
            "skip_stk": "000000000", "trde_qty_tp": "0", "min_trde_qty": "", "max_trde_qty": "",
            "trde_prica_tp": "0", "min_trde_prica": "", "max_trde_prica": "", "motn_drc": "0",
            "stex_tp": "1",
        }
        return self.call("ka10054", "/api/dostk/stkinfo", body)[0].get("motn_stk", []) or []
