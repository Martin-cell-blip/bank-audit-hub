"""SQLite transactional data layer for the audit-confirmation workflow."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("BANK_AUDIT_DB", HERE / "data" / "audit.sqlite3"))
EVIDENCE_DIR = Path(os.environ.get("BANK_AUDIT_EVIDENCE_DIR", HERE / "data" / "evidence"))
_LOCK = threading.RLock()
_CONN: sqlite3.Connection | None = None

REPORT_DATE = date(date.today().year - 1, 12, 31)
FY = REPORT_DATE.year
PERIOD = f"{FY} 年度财务报表审计"

STATUSES = ["待发函", "已发出", "已回函", "差异待跟进", "待复核", "已结项"]
STAGES = ["计划", "现场", "复核", "报告"]
RECEIVABLE_ACCOUNTS = ["应收账款", "应付账款", "其他应收款"]
BANK_ACCOUNTS = ["银行存款", "短期借款", "长期借款"]
DIFF_REASONS = ["未达账项", "记账错误", "舞弊迹象", "待核实"]

USERS = [
    (1, "张伟", "preparer"),
    (2, "李娜", "reviewer"),
    (3, "王强", "admin"),
]

_TODAY = date.today()


def _rel(days: int) -> date:
    return _TODAY + timedelta(days=days)


ENGAGEMENTS = [
    (1, "天津海河实业集团有限公司", "银行存款", "张伟", "现场", -40, 15),
    (2, "天津海河实业集团有限公司", "应收账款", "李娜", "复核", -45, -4),
    (3, "津滨物流发展股份有限公司", "应付账款", "王强", "现场", -35, -6),
    (4, "津门先进制造股份有限公司", "短期借款", "陈静", "计划", -10, 11),
    (5, "渤海湾国际贸易有限公司", "银行存款", "刘洋", "报告", -60, -8),
    (6, "天津智联科技股份有限公司", "其他应收款", "赵敏", "现场", -25, 39),
    (7, "天津海河实业集团有限公司", "短期借款", "张伟", "现场", -40, 15),
]

# eng_id, counterparty, bank_name, account, book_amount, entry_offset, status, reply
SEED = [
    (1, "中国工商银行天津分行营业部", "中国工商银行天津分行", "银行存款", 18620400.55, 5, "已结项", 18620400.55),
    (1, "中国建设银行天津海河支行", "中国建设银行天津分行", "银行存款", 9330500.00, 12, "已回函", 9330500.00),
    (1, "中国邮政储蓄银行天津分行", "中国邮政储蓄银行天津分行", "银行存款", 5407880.20, 8, "差异待跟进", 5470880.20),
    (1, "交通银行天津分行", "交通银行天津分行", "银行存款", 2140000.00, 20, "已发出", None),
    (1, "招商银行天津分行", "招商银行天津分行", "银行存款", 860233.10, 3, "待发函", None),
    (1, "天津银行股份有限公司", "天津银行", "银行存款", 120500.00, 15, None, None),
    (2, "国网天津市电力公司", "", "应收账款", 4680000.00, 65, "已回函", 4680000.00),
    (2, "天津港集团有限公司", "", "应收账款", 3125400.00, 120, "差异待跟进", 2980400.00),
    (2, "中石化销售天津分公司", "", "应收账款", 2210000.00, 210, "已发出", None),
    (2, "天津一汽丰田汽车有限公司", "", "应收账款", 1880600.00, 40, "已结项", 1880600.00),
    (2, "天津钢管制造有限公司", "", "应收账款", 990000.00, 400, None, None),
    (3, "中远海运物流有限公司", "", "应付账款", 3560000.00, 30, "已回函", 3560000.00),
    (3, "天津顺丰速运有限公司", "", "应付账款", 1420000.00, 55, "已发出", None),
    (3, "中储发展股份有限公司", "", "应付账款", 2075300.00, 95, "待发函", None),
    (3, "天津开发区德邦物流", "", "应付账款", 640000.00, 25, None, None),
    (4, "中国银行天津开发区支行", "中国银行天津分行", "短期借款", 30000000.00, 18, "已发出", None),
    (4, "中国农业银行天津分行", "中国农业银行天津分行", "短期借款", 15000000.00, 60, "待发函", None),
    (4, "浦发银行天津分行", "上海浦东发展银行天津分行", "短期借款", 8000000.00, 45, None, None),
    (5, "中国工商银行天津自贸区支行", "中国工商银行天津分行", "银行存款", 22450000.00, 7, "已结项", 22450000.00),
    (5, "中信银行天津分行", "中信银行天津分行", "银行存款", 6180000.00, 10, "已回函", 6180000.00),
    (5, "宁波银行天津分行", "宁波银行天津分行", "银行存款", 430000.00, 22, "差异待跟进", 403000.00),
    (6, "天津经济技术开发区财政局", "", "其他应收款", 1500000.00, 150, "已发出", None),
    (6, "天津市高新技术产业协会", "", "其他应收款", 320000.00, 300, "待发函", None),
    (6, "天津智联控股集团有限公司（控股股东）", "", "其他应收款", 4200000.00, 90, "已发出", None),
    (7, "中国工商银行天津分行营业部", "中国工商银行天津分行", "短期借款", 12000000.00, 15, "已发出", None),
]

_AREA_CODE = {"银行存款": "YH", "应收账款": "YS", "应付账款": "YF", "短期借款": "JK", "其他应收款": "QT"}
_DIFF_REASON = {
    "中国邮政储蓄银行天津分行": "未达账项",
    "天津港集团有限公司": "记账错误",
    "宁波银行天津分行": "待核实",
}


def configure(db_path: str | Path, evidence_dir: str | Path | None = None) -> None:
    """Point the module at an isolated database, primarily for tests."""
    global DB_PATH, EVIDENCE_DIR
    close()
    DB_PATH = Path(db_path)
    if evidence_dir is not None:
        EVIDENCE_DIR = Path(evidence_dir)


def conn() -> sqlite3.Connection:
    global _CONN
    if _CONN is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CONN = sqlite3.connect(DB_PATH, check_same_thread=False)
        _CONN.row_factory = sqlite3.Row
        _CONN.execute("PRAGMA foreign_keys = ON")
        _CONN.execute("PRAGMA journal_mode = WAL")
    return _CONN


def close() -> None:
    global _CONN
    if _CONN is not None:
        _CONN.close()
        _CONN = None


def query(sql: str, params: list | tuple | None = None) -> list[dict]:
    with _LOCK:
        return [dict(row) for row in conn().execute(sql, params or []).fetchall()]


def execute(sql: str, params: list | tuple | None = None) -> sqlite3.Cursor:
    with _LOCK:
        cur = conn().execute(sql, params or [])
        conn().commit()
        return cur


@contextmanager
def transaction():
    with _LOCK:
        c = conn()
        try:
            c.execute("BEGIN IMMEDIATE")
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise


def next_id(table: str) -> int:
    allowed = {"engagements", "ledger", "confirmations", "evidence_files", "review_decisions", "events"}
    if table not in allowed:
        raise ValueError("unsupported table")
    row = query(f"SELECT COALESCE(MAX(id), 0) + 1 AS n FROM {table}")[0]
    return int(row["n"])


def _create_schema(c: sqlite3.Connection) -> None:
    for table in ("events", "review_decisions", "evidence_files", "confirmations", "ledger", "engagements", "users"):
        c.execute(f"DROP TABLE IF EXISTS {table}")
    c.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('preparer','reviewer','admin'))
        );
        CREATE TABLE engagements (
            id INTEGER PRIMARY KEY,
            entity TEXT NOT NULL, audit_area TEXT NOT NULL, period TEXT NOT NULL,
            lead TEXT NOT NULL, stage TEXT NOT NULL,
            start_date TEXT NOT NULL, due_date TEXT NOT NULL
        );
        CREATE TABLE ledger (
            id INTEGER PRIMARY KEY,
            engagement_id INTEGER NOT NULL REFERENCES engagements(id),
            counterparty TEXT NOT NULL, bank_name TEXT, account TEXT NOT NULL,
            book_amount REAL NOT NULL, currency TEXT NOT NULL,
            entry_date TEXT NOT NULL, as_of TEXT NOT NULL
        );
        CREATE TABLE confirmations (
            id INTEGER PRIMARY KEY,
            ledger_id INTEGER NOT NULL UNIQUE REFERENCES ledger(id),
            engagement_id INTEGER NOT NULL REFERENCES engagements(id),
            confirm_no TEXT NOT NULL UNIQUE, counterparty TEXT NOT NULL, bank_name TEXT,
            account TEXT NOT NULL, method TEXT NOT NULL,
            book_amount REAL NOT NULL, reply_amount REAL,
            difference REAL, status TEXT NOT NULL,
            sent_date TEXT, reply_date TEXT, updated_at TEXT NOT NULL,
            diff_reason TEXT, preparer_id INTEGER NOT NULL REFERENCES users(id),
            reviewer_id INTEGER NOT NULL REFERENCES users(id),
            preparer_conclusion TEXT
        );
        CREATE TABLE evidence_files (
            id INTEGER PRIMARY KEY,
            confirmation_id INTEGER NOT NULL REFERENCES confirmations(id),
            evidence_type TEXT NOT NULL, original_name TEXT NOT NULL,
            stored_path TEXT NOT NULL, sha256 TEXT NOT NULL,
            uploaded_by INTEGER NOT NULL REFERENCES users(id),
            uploaded_at TEXT NOT NULL, version INTEGER NOT NULL
        );
        CREATE TABLE review_decisions (
            id INTEGER PRIMARY KEY,
            confirmation_id INTEGER NOT NULL REFERENCES confirmations(id),
            reviewer_id INTEGER NOT NULL REFERENCES users(id),
            decision TEXT NOT NULL CHECK(decision IN ('APPROVED','REJECTED')),
            conclusion TEXT NOT NULL, decided_at TEXT NOT NULL,
            previous_status TEXT NOT NULL, new_status TEXT NOT NULL
        );
        CREATE TABLE events (
            id INTEGER PRIMARY KEY,
            conf_id INTEGER REFERENCES confirmations(id),
            ts TEXT NOT NULL, actor_id INTEGER REFERENCES users(id),
            action TEXT NOT NULL, before_status TEXT, after_status TEXT,
            detail TEXT, metadata_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX idx_events_conf ON events(conf_id, id);
        CREATE INDEX idx_evidence_conf ON evidence_files(confirmation_id, id);
        """
    )


def pick_method(account: str, amount: float) -> str:
    if account in ("应收账款", "其他应收款") and float(amount) < 500000:
        return "消极式"
    return "积极式"


def _insert_event(
    c: sqlite3.Connection,
    conf_id: int | None,
    actor_id: int | None,
    action: str,
    detail: str,
    before_status: str | None = None,
    after_status: str | None = None,
    metadata: dict | None = None,
) -> None:
    c.execute(
        """INSERT INTO events
           (conf_id, ts, actor_id, action, before_status, after_status, detail, metadata_json)
           VALUES (?,?,?,?,?,?,?,?)""",
        (
            conf_id,
            datetime.now(timezone.utc).isoformat(),
            actor_id,
            action,
            before_status,
            after_status,
            detail,
            json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
        ),
    )


def log_event(
    conf_id: int | None,
    actor_id: int | None,
    action: str,
    detail: str,
    before_status: str | None = None,
    after_status: str | None = None,
    metadata: dict | None = None,
) -> None:
    with transaction() as c:
        _insert_event(c, conf_id, actor_id, action, detail, before_status, after_status, metadata)


def seed() -> dict:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        c = conn()
        c.execute("PRAGMA foreign_keys = OFF")
        _create_schema(c)
        c.executemany("INSERT INTO users VALUES (?,?,?)", USERS)
        for eid, entity, area, lead, stage, start, due in ENGAGEMENTS:
            c.execute(
                "INSERT INTO engagements VALUES (?,?,?,?,?,?,?,?)",
                (eid, entity, area, PERIOD, lead, stage, _rel(start).isoformat(), _rel(due).isoformat()),
            )

        ledger_id = 0
        confirmation_id = 0
        sequence: dict[str, int] = {}
        now = datetime.now(timezone.utc).isoformat()
        for engagement_id, counterparty, bank_name, account, amount, offset, status, reply in SEED:
            ledger_id += 1
            c.execute(
                "INSERT INTO ledger VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    ledger_id,
                    engagement_id,
                    counterparty,
                    bank_name,
                    account,
                    amount,
                    "CNY",
                    (REPORT_DATE - timedelta(days=offset)).isoformat(),
                    REPORT_DATE.isoformat(),
                ),
            )
            if status is None:
                continue
            confirmation_id += 1
            code = _AREA_CODE.get(account, "XX")
            sequence[code] = sequence.get(code, 0) + 1
            confirm_no = f"YQ{FY}-{code}-{sequence[code]:03d}"
            sent_date = (REPORT_DATE + timedelta(days=35)).isoformat() if status != "待发函" else None
            reply_date = (REPORT_DATE + timedelta(days=50)).isoformat() if reply is not None else None
            difference = round(amount - reply, 2) if reply is not None else None
            reason = _DIFF_REASON.get(counterparty) if difference not in (None, 0) else None
            conclusion = "回函金额与账面余额一致，证据完整，同意结项。" if status == "已结项" else None
            c.execute(
                """INSERT INTO confirmations
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    confirmation_id,
                    ledger_id,
                    engagement_id,
                    confirm_no,
                    counterparty,
                    bank_name,
                    account,
                    pick_method(account, amount),
                    amount,
                    reply,
                    difference,
                    status,
                    sent_date,
                    reply_date,
                    now,
                    reason,
                    1,
                    2,
                    conclusion,
                ),
            )
            if status == "已结项":
                seed_path = EVIDENCE_DIR / f"{confirm_no}_seed.txt"
                seed_text = f"{confirm_no} 演示回函证据"
                seed_path.write_text(seed_text, encoding="utf-8")
                import hashlib

                c.execute(
                    """INSERT INTO evidence_files
                       (confirmation_id,evidence_type,original_name,stored_path,sha256,uploaded_by,uploaded_at,version)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        confirmation_id,
                        "回函扫描件",
                        seed_path.name,
                        str(seed_path),
                        hashlib.sha256(seed_text.encode("utf-8")).hexdigest(),
                        1,
                        now,
                        1,
                    ),
                )
                c.execute(
                    """INSERT INTO review_decisions
                       (confirmation_id,reviewer_id,decision,conclusion,decided_at,previous_status,new_status)
                       VALUES (?,?,?,?,?,?,?)""",
                    (confirmation_id, 2, "APPROVED", conclusion, now, "待复核", "已结项"),
                )
        _insert_event(c, None, 3, "系统初始化", f"{len(ENGAGEMENTS)} 个审计项目 / {ledger_id} 条台账 / {confirmation_id} 封函证")
        c.commit()
        c.execute("PRAGMA foreign_keys = ON")
    return {"engagements": len(ENGAGEMENTS), "ledger": ledger_id, "confirmations": confirmation_id}


def ensure_seeded() -> None:
    try:
        if query("SELECT COUNT(*) AS n FROM confirmations")[0]["n"] > 0:
            return
    except sqlite3.Error:
        pass
    seed()
