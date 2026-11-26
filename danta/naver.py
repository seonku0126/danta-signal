"""네이버 증권 공개 시세 (키·IP 등록 불필요). 공식 API가 아니므로 구조가 바뀌면 여기만 고치면 된다."""
from __future__ import annotations

import logging
import time as _time
from concurrent.futures import ThreadPoolExecutor

import requests

log = logging.getLogger(__name__)
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/131 Safari/537.36"}
M = "https://m.stock.naver.com/api"
C = "https://api.stock.naver.com/chart/domestic/item"
EXCLUDE_WORDS = ("스팩", "SPAC", "리츠", "제기호", "호스팩")


def _f(v) -> float:
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


class Naver:
    def __init__(self, session: requests.Session | None = None, workers: int = 12):
        self.s = session or requests.Session()
        self.s.headers.update(UA)
        ad = requests.adapters.HTTPAdapter(pool_connections=8, pool_maxsize=max(32, workers * 2))
        self.s.mount("https://", ad)
        self.workers = workers

    def _get(self, url: str, retry: int = 3):
        for i in range(retry):
            try:
                r = self.s.get(url, timeout=8)
                if r.status_code == 200:
                    return r.json()
                if r.status_code in (429, 503):
                    _time.sleep(1 + i)
                    continue
                return None
            except (requests.RequestException, ValueError):
                _time.sleep(0.5 + i)
        return None

    def pmap(self, fn, items):
        with ThreadPoolExecutor(self.workers) as ex:
            return list(ex.map(fn, items))

    # ── 순위 ──
    def ranking(self, sort: str, pages: int = 1, size: int = 100) -> list[dict]:
        """sort: up(상승률) / priceTop(거래대금) / quantTop(거래량) / marketValue(시총). 코스피+코스닥 합쳐 반환."""
        urls = [f"{M}/stocks/{sort}/{mk}?page={p}&pageSize={size}"
                for mk in ("KOSPI", "KOSDAQ") for p in range(1, pages + 1)]
        out = []
        for d in self.pmap(self._get, urls):
            for s in (d or {}).get("stocks", []):
                out.append(norm_rank(s))
        return out

    def all_stocks(self) -> list[dict]:
        """보통주 전체 목록 (시총순, ETF·ETN·스팩·우선주 제외)."""
        rows = self.ranking("marketValue", pages=30)
        return [r for r in rows if is_common(r)]

    # ── 차트 ──
    def minutes(self, code: str, day: str, start: str = "0900", end: str = "1530") -> list[dict]:
        d = self._get(f"{C}/{code}/minute?startDateTime={day}{start}&endDateTime={day}{end}") or []
        return [{"t": b["localDateTime"][8:12], "o": b["openPrice"], "h": b["highPrice"], "l": b["lowPrice"],
                 "c": b["currentPrice"], "v": b["accumulatedTradingVolume"]} for b in d]

    def days(self, code: str, start: str, end: str) -> list[dict]:
        d = self._get(f"{C}/{code}/day?startDateTime={start}&endDateTime={end}") or []
        return [{"d": b["localDate"], "o": b["openPrice"], "h": b["highPrice"], "l": b["lowPrice"],
                 "c": b["closePrice"], "v": b["accumulatedTradingVolume"]} for b in d]

    # ── 종목 상세 ──
    def basic(self, code: str) -> dict:
        return self._get(f"{M}/stock/{code}/basic") or {}

    def hi52(self, code: str) -> float:
        d = self._get(f"{M}/stock/{code}/integration") or {}
        for t in d.get("totalInfos", []):
            if t.get("code") == "highPriceOf52Weeks":
                return _f(t.get("value"))
        return 0.0


def norm_rank(s: dict) -> dict:
    return {
        "code": s.get("itemCode", ""), "name": s.get("stockName", ""), "type": s.get("stockEndType", ""),
        "price": _f(s.get("closePriceRaw") or s.get("closePrice")),
        "flu": _f(s.get("fluctuationsRatio")),
        "value": _f(s.get("accumulatedTradingValueRaw")),       # 원
        "mcap": _f(s.get("marketValueRaw")),                    # 원
        "trading": (s.get("tradeStopType") or {}).get("name") == "TRADING",
        "mkt": "KQ" if str(s.get("sosok")) == "1" else "KS",
    }


def is_common(r: dict) -> bool:
    return (r["type"] == "stock" and r["code"].isdigit() and r["code"].endswith("0")
            and not any(w in r["name"] for w in EXCLUDE_WORDS))
