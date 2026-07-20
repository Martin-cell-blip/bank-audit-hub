from __future__ import annotations

import hashlib

import db


def _post(client, confirmation_id, payload):
    return client.post(f"/api/confirmations/{confirmation_id}/action", json=payload)


def test_direct_reply_cannot_bypass_send(client, pending_confirmation):
    response = _post(
        client,
        pending_confirmation["id"],
        {"action": "reply", "actor_id": 1, "reply_amount": pending_confirmation["book_amount"]},
    )
    assert response.status_code == 409
    assert "先完成发函" in response.json()["detail"]


def test_status_endpoint_cannot_skip_controlled_steps(client, pending_confirmation):
    response = client.post(
        f"/api/confirmations/{pending_confirmation['id']}/status",
        json={"status": "已结项", "actor_id": 1},
    )
    assert response.status_code == 409


def test_full_evidence_review_and_segregation_flow(client, pending_confirmation):
    confirmation_id = pending_confirmation["id"]
    assert _post(client, confirmation_id, {"action": "send", "actor_id": 1}).status_code == 200
    assert _post(
        client,
        confirmation_id,
        {"action": "reply", "actor_id": 1, "reply_amount": pending_confirmation["book_amount"]},
    ).status_code == 200

    blocked = _post(
        client,
        confirmation_id,
        {"action": "submit_review", "actor_id": 1, "conclusion": "回函相符，申请复核结项。"},
    )
    assert blocked.status_code == 409
    assert "至少上传一份" in blocked.json()["detail"]

    content = b"independent confirmation evidence"
    upload = client.post(
        f"/api/confirmations/{confirmation_id}/evidence",
        data={"actor_id": "1", "evidence_type": "回函扫描件"},
        files={"file": ("reply.pdf", content, "application/pdf")},
    )
    assert upload.status_code == 200
    assert upload.json()["sha256"] == hashlib.sha256(content).hexdigest()

    submitted = _post(
        client,
        confirmation_id,
        {"action": "submit_review", "actor_id": 1, "conclusion": "已核对金额及回函证据，提交复核。"},
    )
    assert submitted.status_code == 200
    assert submitted.json()["status"] == "待复核"

    self_review = _post(
        client,
        confirmation_id,
        {"action": "review", "actor_id": 1, "decision": "APPROVED", "conclusion": "证据完整，同意结项处理。"},
    )
    assert self_review.status_code == 403

    approved = _post(
        client,
        confirmation_id,
        {"action": "review", "actor_id": 2, "decision": "APPROVED", "conclusion": "证据完整、金额相符，同意结项。"},
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "已结项"
    assert approved.json()["review_count"] == 1

    events = client.get("/api/events?limit=20").json()
    review_event = next(event for event in events if event["action"] == "复核决定")
    assert review_event["actor_name"] == "李娜"
    assert review_event["before_status"] == "待复核"
    assert review_event["after_status"] == "已结项"


def test_nonzero_difference_requires_reason(client, pending_confirmation):
    confirmation_id = pending_confirmation["id"]
    _post(client, confirmation_id, {"action": "send", "actor_id": 1})
    _post(
        client,
        confirmation_id,
        {"action": "reply", "actor_id": 1, "reply_amount": pending_confirmation["book_amount"] - 100},
    )
    client.post(
        f"/api/confirmations/{confirmation_id}/evidence",
        data={"actor_id": "1", "evidence_type": "差异佐证"},
        files={"file": ("variance.txt", b"timing difference", "text/plain")},
    )

    blocked = _post(
        client,
        confirmation_id,
        {"action": "submit_review", "actor_id": 1, "conclusion": "已检查差异并准备提交复核。"},
    )
    assert blocked.status_code == 409
    assert "差异归因" in blocked.json()["detail"]

    reason = client.post(
        f"/api/confirmations/{confirmation_id}/reason",
        json={"reason": "未达账项", "actor_id": 1},
    )
    assert reason.status_code == 200
    submitted = _post(
        client,
        confirmation_id,
        {"action": "submit_review", "actor_id": 1, "conclusion": "差异属于未达账项，佐证材料完整。"},
    )
    assert submitted.status_code == 200
    assert submitted.json()["status"] == "待复核"


def test_evidence_versions_and_readonly_audit_events(client):
    confirmation = db.query(
        "SELECT * FROM confirmations WHERE status='已回函' ORDER BY id LIMIT 1"
    )[0]
    for name in ("reply-v1.pdf", "reply-v2.pdf"):
        response = client.post(
            f"/api/confirmations/{confirmation['id']}/evidence",
            data={"actor_id": "1", "evidence_type": "回函扫描件"},
            files={"file": (name, name.encode(), "application/pdf")},
        )
        assert response.status_code == 200
    evidence = client.get(f"/api/confirmations/{confirmation['id']}/evidence").json()
    assert [item["version"] for item in evidence[-2:]] == [1, 2]
    assert all(len(item["sha256"]) == 64 for item in evidence)


def test_overview_and_letter_remain_available(client):
    overview = client.get("/api/overview")
    assert overview.status_code == 200
    assert overview.json()["total"] > 0
    confirmation = db.query("SELECT id FROM confirmations ORDER BY id LIMIT 1")[0]
    letter = client.get(f"/api/confirmations/{confirmation['id']}/letter?fmt=html")
    assert letter.status_code == 200
    assert "询证函" in letter.text
