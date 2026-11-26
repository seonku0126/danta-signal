"""매매 기록 저장 + 성적 계산. 모든 숫자를 DB 기록에서 매번 다시 계산하므로 서로 어긋날 수 없다."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

SCHEMA = """
CREATE TABLE IF NOT EXISTS days (
    day TEXT PRIMARY KEY,          -- YYYYMMDD
    status TEXT NOT NULL,          -- traded / no_signal / holiday / error
    note TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL,
    code TEXT, name TEXT,
    signal_tm TEXT, entry INTEGER, stop INTEGER, target INTEGER,
    exit_tm TEXT, exit_price REAL, exit_reason TEXT,
    gross_pct REAL, net_pct REAL, max_pct REAL, min_pct REAL,
    score REAL, reasons TEXT
);
CREATE TABLE IF NOT EXISTS watch (
    day TEXT, code TEXT, name TEXT,
    exp_price REAL, exp_flu REAL, open_price REAL, close_price REAL, high_price REAL,
    picked INTEGER DEFAULT 0,
    PRIMARY KEY(day, code)
);
"""


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)

    def set_day(self, day: str, status: str, note: str = ""):
        self.db.execute("INSERT OR REPLACE INTO days(day,status,note) VALUES(?,?,?)", (day, status, note))
        self.db.commit()

    def add_trade(self, day, pos, cost_pct: float, score: float, reasons: str):
        gross = pos.pct(pos.exit_price)
        self.db.execute(
            """INSERT INTO trades(day,code,name,signal_tm,entry,stop,target,exit_tm,exit_price,exit_reason,
               gross_pct,net_pct,max_pct,min_pct,score,reasons) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (day, pos.code, pos.name, pos.entry_tm, pos.entry, pos.stop, pos.target, pos.exit_tm,
             pos.exit_price, pos.exit_reason, round(gross, 3), round(gross - cost_pct, 3),
             round(pos.pct(pos.max_price), 3), round(pos.pct(pos.min_price), 3), score, reasons),
        )
        self.db.commit()

    def add_watch(self, day: str, w):
        self.db.execute("INSERT OR REPLACE INTO watch(day,code,name,exp_price,exp_flu) VALUES(?,?,?,?,?)",
                        (day, w.code, w.name, w.exp_price, w.flu_rt))
        self.db.commit()

    def mark_picked(self, day: str, code: str):
        self.db.execute("UPDATE watch SET picked=1 WHERE day=? AND code=?", (day, code))
        self.db.commit()

    def set_watch_result(self, day: str, code: str, open_p: float, close_p: float, high_p: float):
        self.db.execute("UPDATE watch SET open_price=?, close_price=?, high_price=? WHERE day=? AND code=?",
                        (open_p, close_p, high_p, day, code))
        self.db.commit()

    def watches(self, day: str | None = None) -> list[dict]:
        q, a = "SELECT * FROM watch", ()
        if day:
            q, a = q + " WHERE day=?", (day,)
        cur = self.db.execute(q + " ORDER BY day, code", a)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def recent_days(self, n: int = 5) -> list[dict]:
        """최근 n 거래일: 상태 + 그날 매매(있으면)."""
        days = self.db.execute(
            "SELECT day, status FROM days WHERE status IN ('traded','no_signal') ORDER BY day DESC LIMIT ?", (n,)
        ).fetchall()
        tmap = {t["day"]: t for t in self.trades()}
        return [{"day": d, "status": st, "trade": tmap.get(d)} for d, st in days]

    def trades(self) -> list[dict]:
        cur = self.db.execute("SELECT * FROM trades ORDER BY day, id")
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def day_counts(self) -> dict:
        return dict(self.db.execute("SELECT status, COUNT(*) FROM days GROUP BY status").fetchall())

    def first_day(self) -> str | None:
        r = self.db.execute("SELECT MIN(day) FROM days WHERE status IN ('traded','no_signal')").fetchone()
        return r[0] if r else None


@dataclass
class Summary:
    n: int
    wins: int
    losses: int
    win_rate: float
    avg_win: float
    avg_loss: float
    profit_factor: float
    sum_net_pct: float      # 단순 합 (매번 같은 금액)
    pnl_won: float          # notional 기준 누적 손익(원)
    mdd_won: float          # 누적손익 곡선의 최대 낙폭(원)
    no_signal_days: int
    since: str | None


def summarize(store: Store, notional: int, month: str | None = None) -> Summary:
    """month='YYYYMM' 이면 그 달만."""
    ts = [t for t in store.trades() if not month or t["day"].startswith(month)]
    nets = [t["net_pct"] for t in ts]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]
    gw, gl = sum(wins), -sum(losses)
    eq = peak = mdd = 0.0
    for x in nets:
        eq += notional * x / 100
        peak = max(peak, eq)
        mdd = max(mdd, peak - eq)
    return Summary(
        n=len(nets), wins=len(wins), losses=len(losses),
        win_rate=len(wins) / len(nets) * 100 if nets else 0.0,
        avg_win=gw / len(wins) if wins else 0.0,
        avg_loss=-gl / len(losses) if losses else 0.0,
        profit_factor=gw / gl if gl else (float("inf") if gw else 0.0),
        sum_net_pct=sum(nets), pnl_won=eq, mdd_won=mdd,
        no_signal_days=store.day_counts().get("no_signal", 0),
        since=store.first_day(),
    )


def watch_summary(store: Store) -> dict:
    """장전 관심종목 성적: 시가(≈예상체결가) 매수 → 종가 매도 가정, 장중 고가 기준 +3% 도달률."""
    ws = [w for w in store.watches() if w["open_price"] and w["close_price"]]
    if not ws:
        return {"n": 0}
    rets = [(w["close_price"] / w["open_price"] - 1) * 100 for w in ws]
    hit3 = sum(1 for w in ws if w["high_price"] and w["high_price"] / w["open_price"] >= 1.03)
    return {"n": len(ws), "avg": sum(rets) / len(rets), "up": sum(1 for r in rets if r > 0),
            "hit3": hit3}
