"""Control-complete confirmation workflow.

All state changes pass through this module so UI routes and direct API calls
cannot apply different control rules.
"""
from __future__ import annotations

import hashlib
import os
import re
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

import db


class WorkflowError(ValueError):
    def __init__(self, message: str, status_code: int = 409):
        super().__init__(message)
        self.status_code = status_code


def _row(sql: str, params: list | tuple) -> dict:
    rows = db.query(sql, params)
    if not rows:
        raise WorkflowError("函证不存在", 404)
    return rows[0]


def get_confirmation(confirmation_id: int) -> dict:
    return _row(
        """SELECT c.*, p.name AS preparer_name, r.name AS reviewer_name,
                  (SELECT COUNT(*) FROM evidence_files e WHERE e.confirmation_id=c.id) AS evidence_count,
                  (SELECT COUNT(*) FROM review_decisions d WHERE d.confirmation_id=c.id) AS review_count
           FROM confirmations c
           JOIN users p ON p.id=c.preparer_id
           JOIN users r ON r.id=c.reviewer_id
           WHERE c.id=?""",
        [confirmation_id],
    )


def _actor(actor_id: int) -> dict:
    rows = db.query("SELECT * FROM users WHERE id=?", [actor_id])
    if not rows:
        raise WorkflowError("操作人不存在", 403)
    return rows[0]


def _require_role(actor: dict, allowed: set[str]) -> None:
    if actor["role"] not in allowed:
        raise WorkflowError("当前角色无权执行该操作", 403)


def _record_transition(
    c,
    confirmation_id: int,
    actor_id: int,
    action: str,
    before: str,
    after: str,
    detail: str,
    metadata: dict | None = None,
) -> None:
    db._insert_event(c, confirmation_id, actor_id, action, detail, before, after, metadata)


def apply_action(
    confirmation_id: int,
    action: str,
    actor_id: int,
    *,
    reply_amount: float | None = None,
    conclusion: str | None = None,
    decision: str | None = None,
    notes: str | None = None,
) -> dict:
    actor = _actor(actor_id)
    conf = get_confirmation(confirmation_id)
    before = conf["status"]

    with db.transaction() as c:
        if action == "send":
            _require_role(actor, {"preparer", "admin"})
            if before != "待发函":
                raise WorkflowError("仅待发函状态可以执行发函")
            after = "已发出"
            c.execute(
                "UPDATE confirmations SET status=?, sent_date=COALESCE(sent_date,?), updated_at=? WHERE id=?",
                (after, date.today().isoformat(), _now(), confirmation_id),
            )
            _record_transition(c, confirmation_id, actor_id, "发函", before, after, conf["confirm_no"])

        elif action == "reply":
            _require_role(actor, {"preparer", "admin"})
            if before != "已发出":
                raise WorkflowError("必须先完成发函，才能录入回函")
            if reply_amount is None:
                raise WorkflowError("请填写回函金额", 400)
            difference = round(float(conf["book_amount"]) - float(reply_amount), 2)
            after = "已回函" if difference == 0 else "差异待跟进"
            c.execute(
                """UPDATE confirmations
                   SET reply_amount=?, difference=?, status=?, reply_date=?,
                       diff_reason=CASE WHEN ?=0 THEN NULL ELSE diff_reason END,
                       preparer_conclusion=NULL, updated_at=?
                   WHERE id=?""",
                (reply_amount, difference, after, date.today().isoformat(), difference, _now(), confirmation_id),
            )
            _record_transition(
                c,
                confirmation_id,
                actor_id,
                "录入回函",
                before,
                after,
                f"回函 {reply_amount:.2f}，差异 {difference:.2f}",
                {"reply_amount": reply_amount, "difference": difference},
            )

        elif action == "urge":
            _require_role(actor, {"preparer", "admin"})
            if before != "已发出":
                raise WorkflowError("仅已发出且未回函的函证可以催办")
            after = before
            detail = conf["confirm_no"] + (
                "（银行存款不可替代程序）" if conf["account"] in db.BANK_ACCOUNTS else "（逾期需评估替代程序）"
            )
            _record_transition(c, confirmation_id, actor_id, "催函", before, after, detail)

        elif action == "submit_review":
            _require_role(actor, {"preparer", "admin"})
            if before not in {"已回函", "差异待跟进"}:
                raise WorkflowError("仅已回函或差异待跟进的函证可以提交复核")
            if actor["role"] != "admin" and actor_id != conf["preparer_id"]:
                raise WorkflowError("仅函证经办人可以提交复核", 403)
            evidence_count = c.execute(
                "SELECT COUNT(*) FROM evidence_files WHERE confirmation_id=?", (confirmation_id,)
            ).fetchone()[0]
            if evidence_count < 1:
                raise WorkflowError("提交复核前至少上传一份回函或替代程序证据")
            if float(conf["difference"] or 0) != 0 and not conf["diff_reason"]:
                raise WorkflowError("非零差异必须先完成差异归因")
            clean_conclusion = (conclusion or "").strip()
            if len(clean_conclusion) < 8:
                raise WorkflowError("处理结论至少填写8个字符")
            after = "待复核"
            c.execute(
                "UPDATE confirmations SET status=?, preparer_conclusion=?, updated_at=? WHERE id=?",
                (after, clean_conclusion, _now(), confirmation_id),
            )
            _record_transition(
                c,
                confirmation_id,
                actor_id,
                "提交复核",
                before,
                after,
                clean_conclusion,
                {"evidence_count": evidence_count},
            )

        elif action == "review":
            _require_role(actor, {"reviewer", "admin"})
            if before != "待复核":
                raise WorkflowError("仅待复核状态可以形成复核决定")
            if actor_id == conf["preparer_id"]:
                raise WorkflowError("经办人与复核人必须分离", 403)
            if actor["role"] != "admin" and actor_id != conf["reviewer_id"]:
                raise WorkflowError("仅指定复核人可以完成复核", 403)
            if decision not in {"APPROVED", "REJECTED"}:
                raise WorkflowError("复核决定必须为 APPROVED 或 REJECTED", 400)
            clean_conclusion = (conclusion or "").strip()
            if len(clean_conclusion) < 8:
                raise WorkflowError("复核结论至少填写8个字符")
            after = "已结项" if decision == "APPROVED" else (
                "差异待跟进" if float(conf["difference"] or 0) != 0 else "已回函"
            )
            c.execute(
                """INSERT INTO review_decisions
                   (confirmation_id,reviewer_id,decision,conclusion,decided_at,previous_status,new_status)
                   VALUES (?,?,?,?,?,?,?)""",
                (confirmation_id, actor_id, decision, clean_conclusion, _now(), before, after),
            )
            c.execute(
                "UPDATE confirmations SET status=?, updated_at=? WHERE id=?",
                (after, _now(), confirmation_id),
            )
            _record_transition(
                c,
                confirmation_id,
                actor_id,
                "复核决定",
                before,
                after,
                clean_conclusion,
                {"decision": decision, "notes": notes or ""},
            )

        elif action == "reset":
            if before == "已结项":
                _require_role(actor, {"admin"})
            else:
                _require_role(actor, {"preparer", "admin"})
            after = "待发函"
            c.execute(
                """UPDATE confirmations
                   SET status=?, sent_date=NULL, reply_date=NULL, reply_amount=NULL,
                       difference=NULL, diff_reason=NULL, preparer_conclusion=NULL, updated_at=?
                   WHERE id=?""",
                (after, _now(), confirmation_id),
            )
            _record_transition(c, confirmation_id, actor_id, "重置", before, after, conf["confirm_no"])

        else:
            raise WorkflowError("未知操作", 400)

    return get_confirmation(confirmation_id)


def set_reason(confirmation_id: int, actor_id: int, reason: str) -> dict:
    actor = _actor(actor_id)
    _require_role(actor, {"preparer", "admin"})
    conf = get_confirmation(confirmation_id)
    if conf["status"] != "差异待跟进" or float(conf["difference"] or 0) == 0:
        raise WorkflowError("仅差异待跟进的函证需要归因")
    if reason not in db.DIFF_REASONS:
        raise WorkflowError("非法归因", 400)
    with db.transaction() as c:
        c.execute(
            "UPDATE confirmations SET diff_reason=?, updated_at=? WHERE id=?",
            (reason, _now(), confirmation_id),
        )
        db._insert_event(c, confirmation_id, actor_id, "差异归因", f"{conf['confirm_no']} → {reason}", conf["status"], conf["status"])
    return get_confirmation(confirmation_id)


def add_evidence(
    confirmation_id: int,
    actor_id: int,
    evidence_type: str,
    original_name: str,
    content: bytes,
) -> dict:
    actor = _actor(actor_id)
    _require_role(actor, {"preparer", "admin"})
    conf = get_confirmation(confirmation_id)
    if conf["status"] in {"待发函", "已结项"}:
        raise WorkflowError("当前状态不允许新增证据")
    if not content:
        raise WorkflowError("证据文件不能为空", 400)
    if len(content) > 10 * 1024 * 1024:
        raise WorkflowError("单个证据文件不得超过10MB", 413)

    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", os.path.basename(original_name)) or "evidence.bin"
    digest = hashlib.sha256(content).hexdigest()
    target_dir = db.EVIDENCE_DIR / str(confirmation_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{uuid.uuid4().hex}_{safe_name}"
    target.write_bytes(content)

    with db.transaction() as c:
        version = c.execute(
            "SELECT COALESCE(MAX(version),0)+1 FROM evidence_files WHERE confirmation_id=? AND evidence_type=?",
            (confirmation_id, evidence_type),
        ).fetchone()[0]
        cur = c.execute(
            """INSERT INTO evidence_files
               (confirmation_id,evidence_type,original_name,stored_path,sha256,uploaded_by,uploaded_at,version)
               VALUES (?,?,?,?,?,?,?,?)""",
            (confirmation_id, evidence_type, original_name, str(target), digest, actor_id, _now(), version),
        )
        evidence_id = cur.lastrowid
        db._insert_event(
            c,
            confirmation_id,
            actor_id,
            "上传证据",
            f"{evidence_type} · {original_name}",
            conf["status"],
            conf["status"],
            {"sha256": digest, "version": version},
        )
    return db.query(
        """SELECT e.*, u.name AS uploader_name FROM evidence_files e
           JOIN users u ON u.id=e.uploaded_by WHERE e.id=?""",
        [evidence_id],
    )[0]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
