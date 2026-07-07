"""
DuckDB 数据层：建表、种子数据、通用查询助手。

设计说明
--------
本平台复用「对账/账龄」思路（源自 logistics-settlement-recon 的 DuckDB 引擎），
把审计函证与多线任务落到 4 张表：
  engagements  审计项目（被审计单位 × 科目 × 阶段）
  ledger       往来/银行台账（账面余额、入账日 → 账龄）
  confirmations 函证（编号、方式、发/回函、差异、状态）
  events       审计留痕（谁在何时对哪封函做了什么）
"""
from __future__ import annotations

import os
import threading
from datetime import date, datetime, timedelta
from decimal import Decimal

import duckdb

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "audit.duckdb")
_LOCK = threading.RLock()          # DuckDB 连接非线程安全 → 统一加锁
_CONN: duckdb.DuckDBPyConnection | None = None

# 资产负债表日（账龄基准）= 上一个已结束年度的 12-31（动态，避免时间线穿帮）
REPORT_DATE = date(date.today().year - 1, 12, 31)
FY = REPORT_DATE.year
PERIOD = f"{FY} 年度财务报表审计"

STATUSES = ["待发函", "已发出", "已回函", "差异待跟进", "已核销"]
STAGES = ["计划", "现场", "复核", "报告"]

# 往来款项科目（只有这些才有"账龄"概念；银行存款/借款不计账龄）
RECEIVABLE_ACCOUNTS = ["应收账款", "应付账款", "其他应收款"]
BANK_ACCOUNTS = ["银行存款", "短期借款", "长期借款"]

# 函证状态机：仅允许相邻的、不需要额外数据的迁移（拖拽用）。
# 目标为 已回函/差异待跟进 时必须带回函金额 → 由 app 层强制走"录入回函"按钮。
STATUS_TRANSITIONS = {
    "待发函": {"已发出"},
    "已发出": {"待发函", "已回函", "差异待跟进"},      # 待发函=撤回
    "已回函": {"差异待跟进", "已核销", "已发出"},
    "差异待跟进": {"已回函", "已核销", "已发出"},
    "已核销": {"已回函", "差异待跟进"},                # 允许退回重开
}

# 差异归因（三分法）
DIFF_REASONS = ["未达账项", "记账错误", "舞弊迹象", "待核实"]


# --------------------------------------------------------------------------- #
# 连接与通用查询
# --------------------------------------------------------------------------- #
def conn() -> duckdb.DuckDBPyConnection:
    global _CONN
    if _CONN is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        _CONN = duckdb.connect(DB_PATH)
    return _CONN


def _rows(cur) -> list[dict]:
    cols = [d[0] for d in cur.description]
    out = []
    for r in cur.fetchall():
        d = {}
        for k, v in zip(cols, r):
            if isinstance(v, Decimal):
                v = float(v)
            elif isinstance(v, (date, datetime)):
                v = v.isoformat()
            d[k] = v
        out.append(d)
    return out


def query(sql: str, params: list | None = None) -> list[dict]:
    with _LOCK:
        return _rows(conn().execute(sql, params or []))


def execute(sql: str, params: list | None = None):
    with _LOCK:
        return conn().execute(sql, params or [])


def next_id(table: str) -> int:
    with _LOCK:
        r = conn().execute(f"SELECT COALESCE(MAX(id), 0) + 1 AS n FROM {table}").fetchone()
        return int(r[0])


# --------------------------------------------------------------------------- #
# 建表 + 种子
# --------------------------------------------------------------------------- #
def _create_schema(c):
    c.execute("DROP TABLE IF EXISTS events")
    c.execute("DROP TABLE IF EXISTS confirmations")
    c.execute("DROP TABLE IF EXISTS ledger")
    c.execute("DROP TABLE IF EXISTS engagements")
    c.execute("""
        CREATE TABLE engagements (
            id INTEGER PRIMARY KEY,
            entity VARCHAR, audit_area VARCHAR, period VARCHAR,
            lead VARCHAR, stage VARCHAR,
            start_date DATE, due_date DATE
        )""")
    c.execute("""
        CREATE TABLE ledger (
            id INTEGER PRIMARY KEY,
            engagement_id INTEGER,
            counterparty VARCHAR, bank_name VARCHAR, account VARCHAR,
            book_amount DECIMAL(18,2), currency VARCHAR,
            entry_date DATE, as_of DATE
        )""")
    c.execute("""
        CREATE TABLE confirmations (
            id INTEGER PRIMARY KEY,
            ledger_id INTEGER, engagement_id INTEGER,
            confirm_no VARCHAR, counterparty VARCHAR, bank_name VARCHAR,
            account VARCHAR, method VARCHAR,
            book_amount DECIMAL(18,2), reply_amount DECIMAL(18,2),
            difference DECIMAL(18,2), status VARCHAR,
            sent_date DATE, reply_date DATE, updated_at TIMESTAMP,
            diff_reason VARCHAR
        )""")
    c.execute("""
        CREATE TABLE events (
            id INTEGER PRIMARY KEY,
            conf_id INTEGER, ts TIMESTAMP, action VARCHAR, detail VARCHAR
        )""")


# 审计项目：被审计单位 × 科目 × 阶段（due_date 相对"今天"设置，2 个逾期用于演示）
_TODAY = date.today()


def _rel(days: int) -> date:
    return _TODAY + timedelta(days=days)


ENGAGEMENTS = [
    # id, entity, audit_area, lead, stage, start(rel), due(rel)
    (1, "天津海河实业集团有限公司", "银行存款", "张伟", "现场", -40, 15),
    (2, "天津海河实业集团有限公司", "应收账款", "李娜", "复核", -45, -4),   # 逾期
    (3, "津滨物流发展股份有限公司", "应付账款", "王强", "现场", -35, -6),   # 逾期
    (4, "津门先进制造股份有限公司", "短期借款", "陈静", "计划", -10, 11),
    (5, "渤海湾国际贸易有限公司", "银行存款", "刘洋", "报告", -60, -8),      # 报告阶段，不算逾期
    (6, "天津智联科技股份有限公司", "其他应收款", "赵敏", "现场", -25, 39),
    (7, "天津海河实业集团有限公司", "短期借款", "张伟", "现场", -40, 15),   # 与 eng1 同单位同行 → 演示"一行一函·多事项聚合"
]

# 台账 + 函证种子。
# (eng_id, counterparty, bank_name, account, book_amount, entry_offset_days_before_REPORT,
#  status|None, reply_amount|None)
#   status=None → 只有台账、尚未生成函证（用于"批量生成询证函"演示）
_C = None  # 占位便于阅读
SEED = [
    # ---- 天津海河实业 / 银行存款（银行询证函）----
    (1, "中国工商银行天津分行营业部", "中国工商银行天津分行", "银行存款", 18620400.55, 5, "已核销", 18620400.55),
    (1, "中国建设银行天津海河支行", "中国建设银行天津分行", "银行存款", 9330500.00, 12, "已回函", 9330500.00),
    (1, "中国邮政储蓄银行天津分行", "中国邮政储蓄银行天津分行", "银行存款", 5407880.20, 8, "差异待跟进", 5470880.20),
    (1, "交通银行天津分行", "交通银行天津分行", "银行存款", 2140000.00, 20, "已发出", None),
    (1, "招商银行天津分行", "招商银行天津分行", "银行存款", 860233.10, 3, "待发函", None),
    (1, "天津银行股份有限公司", "天津银行", "银行存款", 120500.00, 15, _C, None),  # 未生成
    # ---- 天津海河实业 / 应收账款（企业往来询证函）----
    (2, "国网天津市电力公司", "", "应收账款", 4680000.00, 65, "已回函", 4680000.00),
    (2, "天津港集团有限公司", "", "应收账款", 3125400.00, 120, "差异待跟进", 2980400.00),
    (2, "中石化销售天津分公司", "", "应收账款", 2210000.00, 210, "已发出", None),
    (2, "天津一汽丰田汽车有限公司", "", "应收账款", 1880600.00, 40, "已核销", 1880600.00),
    (2, "天津钢管制造有限公司", "", "应收账款", 990000.00, 400, _C, None),  # 未生成，长账龄
    # ---- 津滨物流 / 应付账款 ----
    (3, "中远海运物流有限公司", "", "应付账款", 3560000.00, 30, "已回函", 3560000.00),
    (3, "天津顺丰速运有限公司", "", "应付账款", 1420000.00, 55, "已发出", None),
    (3, "中储发展股份有限公司", "", "应付账款", 2075300.00, 95, "待发函", None),
    (3, "天津开发区德邦物流", "", "应付账款", 640000.00, 25, _C, None),
    # ---- 津门先进制造 / 短期借款（银行询证函·借款）----
    (4, "中国银行天津开发区支行", "中国银行天津分行", "短期借款", 30000000.00, 18, "已发出", None),
    (4, "中国农业银行天津分行", "中国农业银行天津分行", "短期借款", 15000000.00, 60, "待发函", None),
    (4, "浦发银行天津分行", "上海浦东发展银行天津分行", "短期借款", 8000000.00, 45, _C, None),
    # ---- 渤海湾贸易 / 银行存款 ----
    (5, "中国工商银行天津自贸区支行", "中国工商银行天津分行", "银行存款", 22450000.00, 7, "已核销", 22450000.00),
    (5, "中信银行天津分行", "中信银行天津分行", "银行存款", 6180000.00, 10, "已回函", 6180000.00),
    (5, "宁波银行天津分行", "宁波银行天津分行", "银行存款", 430000.00, 22, "差异待跟进", 403000.00),
    # ---- 天津智联科技 / 其他应收款（关联方占款是函证与监管重点）----
    (6, "天津经济技术开发区财政局", "", "其他应收款", 1500000.00, 150, "已发出", None),
    (6, "天津市高新技术产业协会", "", "其他应收款", 320000.00, 300, "待发函", None),  # 小额低风险→消极式
    (6, "天津智联控股集团有限公司（控股股东）", "", "其他应收款", 4200000.00, 90, "已发出", None),  # 关联方资金占用
    # ---- 天津海河实业 / 短期借款（与 eng1 同行 → 同一封工行询证函聚合存款+借款）----
    (7, "中国工商银行天津分行营业部", "中国工商银行天津分行", "短期借款", 12000000.00, 15, "已发出", None),
]

_AREA_CODE = {"银行存款": "YH", "应收账款": "YS", "应付账款": "YF", "短期借款": "JK", "其他应收款": "QT"}

# 差异归因（三分法）——按往来单位挂账，演示"回函差异≠错报"的判断
_DIFF_REASON = {
    "中国邮政储蓄银行天津分行": "未达账项",   # 回函>账面：12-31 已计息未入账
    "天津港集团有限公司": "记账错误",         # 回函<账面：收入重复确认，待调整
    "宁波银行天津分行": "待核实",             # 差额待查，疑似未达账项
}


def pick_method(account, amt) -> str:
    """基于风险选择函证方式：小额、同质、低风险的往来款可用消极式；
    银行存款/借款、大额、关联方一律积极式（准则 1312 消极式适用条件）。"""
    if account in ("应收账款", "其他应收款") and float(amt) < 500000:
        return "消极式"
    return "积极式"


def seed():
    with _LOCK:
        c = conn()
        _create_schema(c)
        # 项目
        for (eid, entity, area, lead, stage, so, du) in ENGAGEMENTS:
            c.execute(
                "INSERT INTO engagements VALUES (?,?,?,?,?,?,?,?)",
                [eid, entity, area, PERIOD, lead, stage, _rel(so), _rel(du)],
            )
        # 台账 + 函证
        lid = 0
        cid = 0
        seq = {}
        now = datetime.now()
        for (eng, cp, bank, account, amt, off, status, reply) in SEED:
            lid += 1
            entry_dt = REPORT_DATE - timedelta(days=off)
            c.execute(
                "INSERT INTO ledger VALUES (?,?,?,?,?,?,?,?,?)",
                [lid, eng, cp, bank, account, Decimal(str(amt)), "CNY", entry_dt, REPORT_DATE],
            )
            if status is None:
                continue
            cid += 1
            code = _AREA_CODE.get(account, "XX")
            seq[code] = seq.get(code, 0) + 1
            confirm_no = f"YQ{FY}-{code}-{seq[code]:03d}"
            method = pick_method(account, amt)
            reply_amt = Decimal(str(reply)) if reply is not None else None
            diff = (Decimal(str(amt)) - reply_amt) if reply_amt is not None else None
            # 函证日期锚定资产负债表日：年报审计约在次年 2 月初寄发、2 月中收回
            sent_dt = None
            reply_dt = None
            if status in ("已发出", "已回函", "差异待跟进", "已核销"):
                sent_dt = REPORT_DATE + timedelta(days=35)
            if status in ("已回函", "差异待跟进", "已核销"):
                reply_dt = REPORT_DATE + timedelta(days=50)
            reason = _DIFF_REASON.get(cp) if (diff is not None and diff != 0) else None
            c.execute(
                "INSERT INTO confirmations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [cid, lid, eng, confirm_no, cp, bank, account, method,
                 Decimal(str(amt)), reply_amt, diff, status, sent_dt, reply_dt, now, reason],
            )
        _log(c, None, "系统", f"初始化演示数据：{len(ENGAGEMENTS)} 个审计项目 / {lid} 条台账 / {cid} 封函证")
    return {"engagements": len(ENGAGEMENTS), "ledger": lid, "confirmations": cid}


def _log(c, conf_id, action, detail):
    nid = c.execute("SELECT COALESCE(MAX(id),0)+1 FROM events").fetchone()[0]
    c.execute("INSERT INTO events VALUES (?,?,?,?,?)",
              [nid, conf_id, datetime.now(), action, detail])


def log_event(conf_id, action, detail):
    with _LOCK:
        _log(conn(), conf_id, action, detail)


def ensure_seeded():
    """首次启动或库为空时自动种子。"""
    try:
        n = query("SELECT COUNT(*) AS n FROM confirmations")[0]["n"]
        if n and n > 0:
            return
    except Exception:
        pass
    seed()
