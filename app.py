"""
银行审计「函证 + 多线任务」协同平台 —— FastAPI 后端。
运行：  uvicorn app:app --reload  然后打开 http://127.0.0.1:8000
"""
from __future__ import annotations

import os
from datetime import date
from decimal import Decimal
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.responses import HTMLResponse, Response, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import db
import letter

app = FastAPI(title="银行审计函证与多线任务协同平台")
HERE = os.path.dirname(__file__)


@app.on_event("startup")
def _startup():
    db.ensure_seeded()


# --------------------------------------------------------------------------- #
# 总览 Dashboard
# --------------------------------------------------------------------------- #
@app.get("/api/overview")
def overview():
    counts = {r["status"]: r["n"] for r in db.query(
        "SELECT status, COUNT(*) n FROM confirmations GROUP BY status")}
    for s in db.STATUSES:
        counts.setdefault(s, 0)
    total = sum(counts.values())
    replied = counts["已回函"] + counts["差异待跟进"] + counts["已核销"]

    agg = db.query("""
        SELECT
          COUNT(*) FILTER (WHERE difference IS NOT NULL AND difference <> 0) AS diff_cnt,
          COALESCE(SUM(ABS(difference)) FILTER (WHERE difference IS NOT NULL AND difference <> 0),0) AS diff_amt,
          COALESCE(SUM(book_amount),0) AS book_total
        FROM confirmations""")[0]

    overdue = db.query("""
        SELECT COUNT(*) n FROM engagements
        WHERE due_date < CURRENT_DATE AND stage <> '报告'""")[0]["n"]

    # 账龄分布：仅往来款项（应收/应付/其他应收）——银行存款/借款无账龄概念
    recv = "(" + ",".join("'%s'" % a for a in db.RECEIVABLE_ACCOUNTS) + ")"
    aging = db.query(f"""
        WITH b AS (
          SELECT CASE
            WHEN date_diff('day', entry_date, as_of) <= 30 THEN '0-30天'
            WHEN date_diff('day', entry_date, as_of) <= 90 THEN '31-90天'
            WHEN date_diff('day', entry_date, as_of) <= 180 THEN '91-180天'
            WHEN date_diff('day', entry_date, as_of) <= 365 THEN '181-365天'
            ELSE '1年以上' END AS bucket,
          book_amount
          FROM ledger WHERE account IN {recv})
        SELECT bucket, COUNT(*) cnt, SUM(book_amount) amt FROM b GROUP BY bucket""")
    order = ['0-30天', '31-90天', '91-180天', '181-365天', '1年以上']
    aging.sort(key=lambda x: order.index(x["bucket"]) if x["bucket"] in order else 99)

    # 函证覆盖率（金额口径，按科目）——审计质量控制核心指标
    coverage = db.query("""
        SELECT l.account,
          SUM(l.book_amount) AS total_amt,
          COALESCE(SUM(l.book_amount) FILTER (
            WHERE EXISTS(SELECT 1 FROM confirmations c WHERE c.ledger_id = l.id)), 0) AS confirmed_amt
        FROM ledger l GROUP BY l.account""")
    for cov in coverage:
        cov["rate"] = round(cov["confirmed_amt"] / cov["total_amt"] * 100, 2) if cov["total_amt"] else 0
        # 银行存款监管红线：应 100% 函证（含零余额户、销户）
        cov["alert"] = (cov["account"] == "银行存款" and cov["rate"] < 100)
    coverage.sort(key=lambda x: -x["total_amt"])

    entity_progress = db.query("""
        SELECT e.entity,
               COUNT(c.id) AS total,
               COUNT(c.id) FILTER (WHERE c.status IN ('已回函','差异待跟进','已核销')) AS replied
        FROM engagements e LEFT JOIN confirmations c ON c.engagement_id = e.id
        GROUP BY e.entity ORDER BY e.entity""")

    return {
        "status_counts": counts,
        "total": total,
        "replied": replied,
        "reply_rate": round(replied / total * 100, 1) if total else 0,
        "diff_cnt": agg["diff_cnt"],
        "diff_amt": agg["diff_amt"],
        "book_total": agg["book_total"],
        "overdue": overdue,
        "aging": aging,
        "coverage": coverage,
        "entity_progress": entity_progress,
        "fy": db.FY,
        "as_of": db.REPORT_DATE.isoformat(),
        "today": date.today().isoformat(),
    }


# --------------------------------------------------------------------------- #
# 函证看板
# --------------------------------------------------------------------------- #
URGE_DAYS = 30   # 发函后超过 N 天未回 → 应催函 / 转替代程序


@app.get("/api/confirmations")
def list_confirmations():
    rows = db.query("""
        SELECT c.*, e.entity, e.audit_area,
          CASE WHEN c.sent_date IS NOT NULL THEN date_diff('day', c.sent_date, CURRENT_DATE) END AS days_since_sent
        FROM confirmations c JOIN engagements e ON e.id = c.engagement_id
        ORDER BY c.confirm_no""")
    for r in rows:
        d = r.get("days_since_sent")
        # 已发出且超期未回：积极式须催函/转替代程序；银行存款不得以替代程序替代
        r["needs_followup"] = bool(r["status"] == "已发出" and d is not None and d >= URGE_DAYS)
        r["no_alt_procedure"] = r["account"] in db.BANK_ACCOUNTS
    return rows


class StatusIn(BaseModel):
    status: str


class ActionIn(BaseModel):
    action: str
    reply_amount: float | None = None


class ReasonIn(BaseModel):
    reason: str


def _get_conf(cid: int) -> dict:
    r = db.query("SELECT * FROM confirmations WHERE id = ?", [cid])
    if not r:
        raise HTTPException(404, "函证不存在")
    return r[0]


@app.post("/api/confirmations/{cid}/status")
def set_status(cid: int, body: StatusIn):
    """拖拽换状态：只允许状态机中合法的、无需额外数据的迁移。"""
    if body.status not in db.STATUSES:
        raise HTTPException(400, "非法状态")
    conf = _get_conf(cid)
    cur, st = conf["status"], body.status
    if st == cur:
        return conf
    # 1) 合法迁移校验（防止跳过发函/回函直接核销）
    if st not in db.STATUS_TRANSITIONS.get(cur, set()):
        raise HTTPException(409, f"不允许从「{cur}」直接拖到「{st}」，请按流程逐步推进")
    # 2) 进入已回函/差异待跟进必须有回函金额 → 强制走「录入回函」按钮
    if st in ("已回函", "差异待跟进") and conf["reply_amount"] is None:
        raise HTTPException(409, "请用「录入回函」按钮登记回函金额，回函差异需据实计算")
    sets = ["status = ?", "updated_at = now()"]
    params: list = [st]
    if st == "已发出":
        sets.append("sent_date = COALESCE(sent_date, CURRENT_DATE)")
    if st == "待发函":
        sets += ["sent_date = NULL", "reply_date = NULL",
                 "reply_amount = NULL", "difference = NULL", "diff_reason = NULL"]
    db.execute(f"UPDATE confirmations SET {', '.join(sets)} WHERE id = ?", params + [cid])
    db.log_event(cid, "状态流转", f"{cur} → {st}")
    return _get_conf(cid)


@app.post("/api/confirmations/{cid}/action")
def act(cid: int, body: ActionIn):
    conf = _get_conf(cid)
    a = body.action
    if a == "send":
        db.execute("UPDATE confirmations SET status='已发出', "
                   "sent_date=COALESCE(sent_date,CURRENT_DATE), updated_at=now() WHERE id=?", [cid])
        db.log_event(cid, "发函", conf["confirm_no"])
    elif a == "reply":
        if body.reply_amount is None:
            raise HTTPException(400, "请填写回函金额")
        reply = Decimal(str(body.reply_amount))
        diff = Decimal(str(conf["book_amount"])) - reply
        status = "已回函" if diff == 0 else "差异待跟进"
        # 相符则清空差异归因
        db.execute("UPDATE confirmations SET reply_amount=?, difference=?, status=?, "
                   "reply_date=COALESCE(reply_date,CURRENT_DATE), "
                   "sent_date=COALESCE(sent_date,CURRENT_DATE), "
                   "diff_reason=CASE WHEN ?=0 THEN NULL ELSE diff_reason END, updated_at=now() WHERE id=?",
                   [reply, diff, status, diff, cid])
        db.log_event(cid, "录入回函", f"回函 {reply}，差异 {diff}")
    elif a == "urge":
        db.log_event(cid, "催函", f"{conf['confirm_no']} 二次催函"
                     + ("（银行存款不可替代程序）" if conf["account"] in db.BANK_ACCOUNTS else "，逾期未回将转替代程序"))
    elif a == "writeoff":
        if conf["status"] not in ("已回函", "差异待跟进"):
            raise HTTPException(409, "仅已回函/差异待跟进的函证可核销")
        db.execute("UPDATE confirmations SET status='已核销', updated_at=now() WHERE id=?", [cid])
        db.log_event(cid, "核销", conf["confirm_no"])
    elif a == "reset":
        db.execute("UPDATE confirmations SET status='待发函', sent_date=NULL, reply_date=NULL, "
                   "reply_amount=NULL, difference=NULL, diff_reason=NULL, updated_at=now() WHERE id=?", [cid])
        db.log_event(cid, "重置", conf["confirm_no"])
    else:
        raise HTTPException(400, "未知操作")
    return _get_conf(cid)


@app.post("/api/confirmations/{cid}/reason")
def set_reason(cid: int, body: ReasonIn):
    """差异归因（三分法）：未达账项 / 记账错误 / 舞弊迹象 / 待核实。"""
    if body.reason not in db.DIFF_REASONS:
        raise HTTPException(400, "非法归因")
    conf = _get_conf(cid)
    if conf["difference"] in (None, 0):
        raise HTTPException(409, "仅有差异的函证需要归因")
    db.execute("UPDATE confirmations SET diff_reason=?, updated_at=now() WHERE id=?", [body.reason, cid])
    db.log_event(cid, "差异归因", f"{conf['confirm_no']} → {body.reason}")
    return _get_conf(cid)


@app.get("/api/confirmations/{cid}/letter")
def get_letter(cid: int, fmt: str = "html"):
    conf = _get_conf(cid)
    eng = db.query("SELECT * FROM engagements WHERE id=?", [conf["engagement_id"]])[0]
    # 银行询证函：一封覆盖该行（同一被审计单位）全部存款/借款事项 → 聚合同 bank_name+entity 的兄弟函证
    rows = None
    if conf["account"] in db.BANK_ACCOUNTS and conf["bank_name"]:
        rows = db.query("""
            SELECT c.* FROM confirmations c JOIN engagements e ON e.id = c.engagement_id
            WHERE c.bank_name = ? AND e.entity = ? ORDER BY c.account, c.confirm_no""",
            [conf["bank_name"], eng["entity"]])
    if fmt == "docx":
        data = letter.letter_docx(conf, eng, rows)
        no = conf["confirm_no"]
        fname = quote(f"询证函_{no}.docx")          # RFC 5987：HTTP 头仅支持 latin-1，中文名须 URL 编码
        disp = f"attachment; filename=\"letter_{no}.docx\"; filename*=UTF-8''{fname}"
        return Response(
            content=data,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={"Content-Disposition": disp},
        )
    return HTMLResponse(letter.letter_html(conf, eng, rows))


# --------------------------------------------------------------------------- #
# 台账 + 批量生成
# --------------------------------------------------------------------------- #
@app.get("/api/ledger")
def list_ledger():
    return db.query("""
        SELECT l.*, e.entity,
          date_diff('day', l.entry_date, l.as_of) AS age_days,
          EXISTS(SELECT 1 FROM confirmations c WHERE c.ledger_id = l.id) AS has_conf
        FROM ledger l JOIN engagements e ON e.id = l.engagement_id
        ORDER BY l.id""")


_AREA_CODE = {"银行存款": "YH", "应收账款": "YS", "应付账款": "YF",
              "短期借款": "JK", "长期借款": "JK", "其他应收款": "QT"}


def _new_confirm_no(area: str) -> str:
    code = _AREA_CODE.get(area, "XX")
    n = db.query("SELECT COUNT(*) n FROM confirmations WHERE confirm_no LIKE ?",
                 [f"YQ{db.FY}-{code}-%"])[0]["n"]
    return f"YQ{db.FY}-{code}-{n + 1:03d}"


@app.post("/api/ledger/generate")
def generate():
    rows = db.query("""
        SELECT l.* FROM ledger l
        WHERE NOT EXISTS(SELECT 1 FROM confirmations c WHERE c.ledger_id = l.id)""")
    created = []
    for r in rows:
        cid = db.next_id("confirmations")
        no = _new_confirm_no(r["account"])
        method = db.pick_method(r["account"], r["book_amount"])
        db.execute("INSERT INTO confirmations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   [cid, r["id"], r["engagement_id"], no, r["counterparty"], r["bank_name"],
                    r["account"], method, Decimal(str(r["book_amount"])), None, None,
                    "待发函", None, None, None, None])
        db.log_event(cid, "生成函证", f"{no} · {r['counterparty']}")
        created.append(no)
    return {"created": len(created), "nos": created}


@app.post("/api/ledger/import")
async def import_ledger(file: UploadFile = File(...), engagement_id: int = Form(...)):
    """导入 Excel 台账（列：往来单位/开户行, 科目, 账面余额, 入账日[可选]）。"""
    import openpyxl
    import io as _io
    data = await file.read()
    wb = openpyxl.load_workbook(_io.BytesIO(data), data_only=True)
    ws = wb.active
    header = [str(c.value).strip() if c.value is not None else "" for c in ws[1]]

    def col(*names):
        for n in names:
            if n in header:
                return header.index(n)
        return None

    ci_cp = col("往来单位", "开户行", "往来单位/开户行", "单位名称")
    ci_ac = col("科目", "会计科目")
    ci_amt = col("账面余额", "余额", "金额", "账面金额")
    ci_dt = col("入账日", "入账日期", "日期")
    if ci_cp is None or ci_amt is None:
        raise HTTPException(400, "缺少必需列：往来单位/开户行、账面余额")

    n = 0
    for row in ws.iter_rows(min_row=2, values_only=True):
        if row is None or all(v is None for v in row):
            continue
        cp = str(row[ci_cp]).strip() if row[ci_cp] is not None else ""
        if not cp:
            continue
        account = str(row[ci_ac]).strip() if ci_ac is not None and row[ci_ac] else "应收账款"
        try:
            amt = Decimal(str(row[ci_amt]))
        except Exception:
            continue
        entry = row[ci_dt] if ci_dt is not None else None
        try:
            entry_dt = entry.date() if hasattr(entry, "date") else (
                date.fromisoformat(str(entry)[:10]) if entry else db.REPORT_DATE)
        except Exception:
            entry_dt = db.REPORT_DATE
        bank = cp if account in letter.BANK_ACCOUNTS else ""
        lid = db.next_id("ledger")
        db.execute("INSERT INTO ledger VALUES (?,?,?,?,?,?,?,?,?)",
                   [lid, engagement_id, cp, bank, account, amt, "CNY", entry_dt, db.REPORT_DATE])
        n += 1
    db.log_event(None, "导入台账", f"{file.filename}：{n} 行 → 项目#{engagement_id}")
    return {"imported": n}


# --------------------------------------------------------------------------- #
# 多线任务 / 审计项目
# --------------------------------------------------------------------------- #
@app.get("/api/engagements")
def list_engagements():
    return db.query("""
        SELECT e.*,
          date_diff('day', CURRENT_DATE, e.due_date) AS days_left,
          (e.due_date < CURRENT_DATE AND e.stage <> '报告') AS overdue,
          COUNT(c.id) AS conf_total,
          COUNT(c.id) FILTER (WHERE c.status IN ('已回函','差异待跟进','已核销')) AS conf_replied,
          COUNT(c.id) FILTER (WHERE c.status = '差异待跟进') AS conf_diff
        FROM engagements e LEFT JOIN confirmations c ON c.engagement_id = e.id
        GROUP BY ALL ORDER BY e.id""")


class AdvanceIn(BaseModel):
    direction: str = "next"


@app.post("/api/engagements/{eid}/advance")
def advance(eid: int, body: AdvanceIn):
    r = db.query("SELECT * FROM engagements WHERE id=?", [eid])
    if not r:
        raise HTTPException(404, "项目不存在")
    cur = r[0]["stage"]
    i = db.STAGES.index(cur) if cur in db.STAGES else 0
    i = min(i + 1, len(db.STAGES) - 1) if body.direction == "next" else max(i - 1, 0)
    db.execute("UPDATE engagements SET stage=? WHERE id=?", [db.STAGES[i], eid])
    return db.query("SELECT * FROM engagements WHERE id=?", [eid])[0]


# --------------------------------------------------------------------------- #
# 杂项
# --------------------------------------------------------------------------- #
@app.get("/api/events")
def events(limit: int = 12):
    return db.query("SELECT * FROM events ORDER BY id DESC LIMIT ?", [limit])


@app.post("/api/reset")
def reset():
    return db.seed()


@app.get("/", response_class=HTMLResponse)
def index():
    return FileResponse(os.path.join(HERE, "static", "index.html"))


app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")
