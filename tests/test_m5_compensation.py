"""验收单撤销补偿流程测试。

覆盖：
- 撤销后当前应付扣除撤销金额；已付款凭证原样保留，未付款关联才释放
- 红字冲抵到账前只列待追回、不增加可匹配余额；到账后结清
- 部分付款按比例拆分（已付保留/未付释放）
- 并发撤销的过期版本被拒绝
- 补偿写入中断可恢复，重试不重复冲抵，失败保留原匹配
- 幂等键防重复付款/撤销/红字
"""
import threading
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.modules.m5_matching import domain, ledger_service as svc
from app.modules.m5_matching.money import largest_remainder_split, to_decimal
from app.modules.m5_matching.repository import arm_failure, clear_failures


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "m5_test.sqlite3"
    monkeypatch.setattr("app.config.DATABASE_PATH", db_path)
    monkeypatch.setattr(
        "app.modules.m5_matching.repository.DATABASE_PATH", db_path
    )
    clear_failures()
    yield TestClient(app)
    clear_failures()


def _seed_and_match(client: TestClient):
    client.post("/api/matching/seed")
    # GR-1 600 + GR-2 530 匹配 IV-1 1130；GR-3 400 匹配 IV-2 400
    client.post("/api/matching/matches", json={"gr_id": "GR-1", "invoice_id": "IV-1", "amount": "600.00"})
    client.post("/api/matching/matches", json={"gr_id": "GR-2", "invoice_id": "IV-1", "amount": "530.00"})
    client.post("/api/matching/matches", json={"gr_id": "GR-3", "invoice_id": "IV-2", "amount": "400.00"})


def test_money_split_conserves_total_and_respects_caps():
    parts = largest_remainder_split(Decimal("100.00"), [Decimal("60"), Decimal("60")])
    assert sum(parts) == Decimal("100.00")
    assert all(part <= Decimal("60.00") for part in parts)
    assert parts == [Decimal("50.00"), Decimal("50.00")]
    odd = largest_remainder_split(Decimal("10.00"), [Decimal("3.33"), Decimal("3.33"), Decimal("3.34")])
    assert sum(odd) == Decimal("10.00")
    assert all(p > Decimal("0") for p in odd)


def test_ledger_tracks_original_occupied_available(client):
    _seed_and_match(client)
    data = client.get("/api/matching/ledger").json()
    assert data["kpis"]["current_payable"] == "1530.00"
    gr1 = next(row for row in data["goods_receipts"] if row["gr_id"] == "GR-1")
    assert (gr1["original_amount"], gr1["occupied_amount"], gr1["available_amount"]) == (
        "600.00", "600.00", "0.00",
    )
    iv1 = next(row for row in data["invoices"] if row["invoice_id"] == "IV-1")
    assert iv1["available_amount"] == "0.00"
    assert data["invariants"]["ok"] is True


def test_revoke_releases_unpaid_but_keeps_paid_voucher(client):
    _seed_and_match(client)
    ledger = client.get("/api/matching/ledger").json()
    gr3_link = next(link for link in ledger["match_links"] if link["gr_id"] == "GR-3")
    # GR-3 的凭证全额付款
    paid = client.post("/api/matching/payments", json={
        "link_id": gr3_link["link_id"], "amount": "400.00", "idempotency_key": "PAY-1"
    }).json()
    assert paid["status"] == domain.LINK_PAID

    # 撤销 GR-3（含已付款凭证）
    result = client.post("/api/matching/revocations", json={
        "gr_id": "GR-3", "idempotency_key": "REV-1"
    }).json()
    assert result["deducted_amount"] == "400.00"

    data = client.get("/api/matching/ledger").json()
    # 当前应付扣除撤销金额
    assert data["kpis"]["current_payable"] == "1130.00"
    assert data["kpis"]["revoked_total"] == "400.00"
    # 已付款关联原样保留，不删除、不释放
    link = next(row for row in data["match_links"] if row["link_id"] == gr3_link["link_id"])
    assert link["status"] == domain.LINK_RETAINED
    assert link["paid_amount"] == "400.00"
    assert link["released_amount"] == "0.00"
    # 红字到账前只列待追回，且不增加任何可匹配余额
    assert data["kpis"]["pending_recovery"] == "400.00"
    gr3 = next(row for row in data["goods_receipts"] if row["gr_id"] == "GR-3")
    assert gr3["available_amount"] == "0.00"
    assert data["invariants"]["ok"] is True


def test_revoke_unpaid_link_releases_invoice_balance_for_rematch(client):
    _seed_and_match(client)
    # 撤销未付款的 GR-1：关联释放，IV-1 重新获得 600 可匹配余额
    client.post("/api/matching/revocations", json={"gr_id": "GR-1", "idempotency_key": "REV-2"})
    data = client.get("/api/matching/ledger").json()
    link = next(row for row in data["match_links"] if row["gr_id"] == "GR-1")
    assert link["status"] == domain.LINK_RELEASED
    assert link["released_amount"] == "600.00"
    iv1 = next(row for row in data["invoices"] if row["invoice_id"] == "IV-1")
    assert iv1["available_amount"] == "600.00"
    assert data["kpis"]["current_payable"] == "930.00"
    assert data["kpis"]["pending_recovery"] == "0.00"
    # 释放出的发票余额可以同笔货款再次匹配到另一张 ACTIVE 验收单
    client.post("/api/matching/goods-receipts", json={
        "gr_id": "GR-4", "sku": "SKU-A", "tax_rate": "0.13", "amount": "600.00"
    })
    rematch = client.post("/api/matching/matches", json={
        "gr_id": "GR-4", "invoice_id": "IV-1", "amount": "600.00"
    })
    assert rematch.status_code == 200
    assert data["invariants"]["ok"] is True


def test_partial_payment_split_retains_paid_releases_unpaid(client):
    _seed_and_match(client)
    ledger = client.get("/api/matching/ledger").json()
    link = next(row for row in ledger["match_links"] if row["gr_id"] == "GR-1")
    # 部分付款 200/600
    client.post("/api/matching/payments", json={
        "link_id": link["link_id"], "amount": "200.00", "idempotency_key": "PAY-2"
    })
    client.post("/api/matching/revocations", json={"gr_id": "GR-1", "idempotency_key": "REV-3"})
    data = client.get("/api/matching/ledger").json()
    row = next(item for item in data["match_links"] if item["link_id"] == link["link_id"])
    assert row["status"] == domain.LINK_PARTIAL_RETAINED
    assert row["released_amount"] == "400.00"
    assert row["retained_amount"] == "200.00"
    assert row["paid_amount"] == "200.00"
    # 待追回仅含已付部分
    assert data["kpis"]["pending_recovery"] == "200.00"
    iv1 = next(row for row in data["invoices"] if row["invoice_id"] == "IV-1")
    assert iv1["available_amount"] == "400.00"
    assert data["invariants"]["ok"] is True


def test_credit_note_settles_recovery_without_refilling_balance(client):
    _seed_and_match(client)
    ledger = client.get("/api/matching/ledger").json()
    link = next(row for row in ledger["match_links"] if row["gr_id"] == "GR-3")
    client.post("/api/matching/payments", json={
        "link_id": link["link_id"], "amount": "400.00", "idempotency_key": "PAY-3"
    })
    client.post("/api/matching/revocations", json={"gr_id": "GR-3", "idempotency_key": "REV-4"})
    before = client.get("/api/matching/ledger").json()
    recovery_id = before["recoveries"][0]["recovery_id"]

    # 到账前不能超额登记
    too_much = client.post("/api/matching/credit-notes", json={
        "recovery_id": recovery_id, "amount": "500.00", "idempotency_key": "CN-X"
    })
    assert too_much.status_code == 422

    # 部分到账：仍列待追回，可匹配余额不变
    client.post("/api/matching/credit-notes", json={
        "recovery_id": recovery_id, "amount": "150.00", "idempotency_key": "CN-1"
    })
    mid = client.get("/api/matching/ledger").json()
    rec = next(row for row in mid["recoveries"] if row["recovery_id"] == recovery_id)
    assert rec["status"] == domain.RECOVERY_PENDING
    assert rec["outstanding_amount"] == "250.00"
    assert mid["kpis"]["pending_recovery"] == "250.00"
    gr3 = next(row for row in mid["goods_receipts"] if row["gr_id"] == "GR-3")
    assert gr3["available_amount"] == "0.00"

    # 全额到账后结清，依旧不回补可匹配余额
    client.post("/api/matching/credit-notes", json={
        "recovery_id": recovery_id, "amount": "250.00", "idempotency_key": "CN-2"
    })
    after = client.get("/api/matching/ledger").json()
    rec = next(row for row in after["recoveries"] if row["recovery_id"] == recovery_id)
    assert rec["status"] == domain.RECOVERY_SETTLED
    assert after["kpis"]["pending_recovery"] == "0.00"
    assert after["kpis"]["settled_recovery"] == "400.00"
    gr3 = next(row for row in after["goods_receipts"] if row["gr_id"] == "GR-3")
    assert gr3["available_amount"] == "0.00"
    assert after["invariants"]["ok"] is True


def test_credit_note_idempotent_retry_does_not_double_offset(client):
    _seed_and_match(client)
    ledger = client.get("/api/matching/ledger").json()
    link = next(row for row in ledger["match_links"] if row["gr_id"] == "GR-3")
    client.post("/api/matching/payments", json={
        "link_id": link["link_id"], "amount": "400.00", "idempotency_key": "PAY-4"
    })
    client.post("/api/matching/revocations", json={"gr_id": "GR-3", "idempotency_key": "REV-5"})
    recovery_id = client.get("/api/matching/ledger").json()["recoveries"][0]["recovery_id"]
    payload = {"recovery_id": recovery_id, "amount": "400.00", "idempotency_key": "CN-DUP"}
    first = client.post("/api/matching/credit-notes", json=payload).json()
    second = client.post("/api/matching/credit-notes", json=payload).json()
    assert first["credit_note_id"] == second["credit_note_id"]
    data = client.get("/api/matching/ledger").json()
    assert len(data["credit_notes"]) == 1
    assert data["kpis"]["settled_recovery"] == "400.00"


def test_concurrent_revoke_rejects_stale_version(client):
    _seed_and_match(client)
    gr = next(row for row in client.get("/api/matching/ledger").json()["goods_receipts"]
              if row["gr_id"] == "GR-2")
    assert gr["version"] == 1
    # 携带过期版本号的并发撤销被拒绝
    stale = client.post("/api/matching/revocations", json={
        "gr_id": "GR-2", "idempotency_key": "REV-STALE", "expected_version": 99
    })
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "GR_VERSION_CONFLICT"

    # 真实并发：两个线程同时撤销同一验收单，只允许一个成功
    barrier = threading.Barrier(2)
    outcomes = []

    def worker(key):
        barrier.wait()
        resp = client.post("/api/matching/revocations", json={"gr_id": "GR-2", "idempotency_key": key})
        outcomes.append(resp.status_code)

    t1 = threading.Thread(target=worker, args=("REV-C1",))
    t2 = threading.Thread(target=worker, args=("REV-C2",))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert sorted(outcomes) == [200, 409]
    data = client.get("/api/matching/ledger").json()
    gr2 = next(row for row in data["goods_receipts"] if row["gr_id"] == "GR-2")
    assert gr2["status"] == domain.GR_REVOKED
    assert gr2["version"] == 2
    # 只撤销一次：当前应付只扣一次
    assert data["kpis"]["current_payable"] == "1000.00"


def test_saga_interruption_is_recoverable_and_keeps_original_matching(client):
    _seed_and_match(client)
    ledger = client.get("/api/matching/ledger").json()
    gr3_link = next(link for link in ledger["match_links"] if link["gr_id"] == "GR-3")
    client.post("/api/matching/payments", json={
        "link_id": gr3_link["link_id"], "amount": "400.00", "idempotency_key": "PAY-5"
    })

    # 在第二步（保留已付款/登记待追回）注入故障，事务整体回滚
    arm_failure("revoke_RETAIN_PAID")
    with pytest.raises(RuntimeError):
        svc.revoke_gr("GR-3", idempotency_key="REV-6", failure_point=domain.STEP_RETAIN_PAID)

    # 中断后原匹配原样保留，验收单仍 ACTIVE、应付未扣
    mid = client.get("/api/matching/ledger").json()
    gr3 = next(row for row in mid["goods_receipts"] if row["gr_id"] == "GR-3")
    assert gr3["status"] == domain.GR_ACTIVE
    assert mid["kpis"]["current_payable"] == "1530.00"
    link = next(row for row in mid["match_links"] if row["link_id"] == gr3_link["link_id"])
    assert link["status"] == domain.LINK_PAID
    assert mid["recoveries"] == []

    # 恢复：从中断点继续，已完成的释放步骤不重复执行
    resume = client.post("/api/matching/revocations/resume").json()
    assert resume["count"] == 1
    assert domain.STEP_RETAIN_PAID in resume["resumed"][0]["resumed_steps"]
    assert domain.STEP_REVOKE_GR in resume["resumed"][0]["resumed_steps"]

    done = client.get("/api/matching/ledger").json()
    assert done["kpis"]["current_payable"] == "1130.00"
    assert done["kpis"]["pending_recovery"] == "400.00"
    link = next(row for row in done["match_links"] if row["link_id"] == gr3_link["link_id"])
    assert link["status"] == domain.LINK_RETAINED
    assert done["invariants"]["ok"] is True

    # 再次恢复为空操作，不产生重复待追回
    again = client.post("/api/matching/revocations/resume").json()
    assert again["count"] == 0
    final = client.get("/api/matching/ledger").json()
    assert len(final["recoveries"]) == 1


def test_revoke_idempotent_retry_returns_same_result(client):
    _seed_and_match(client)
    payload = {"gr_id": "GR-1", "idempotency_key": "REV-IDEM"}
    first = client.post("/api/matching/revocations", json=payload).json()
    second = client.post("/api/matching/revocations", json=payload).json()
    assert first["revoke_id"] == second["revoke_id"]
    data = client.get("/api/matching/ledger").json()
    assert len([r for r in data["match_links"] if r["gr_id"] == "GR-1"]) == 1


def test_failure_at_first_step_rolls_back_and_idempotent_retry_resumes(client):
    _seed_and_match(client)
    ledger = client.get("/api/matching/ledger").json()
    gr3_link = next(link for link in ledger["match_links"] if link["gr_id"] == "GR-3")
    client.post("/api/matching/payments", json={
        "link_id": gr3_link["link_id"], "amount": "400.00", "idempotency_key": "PAY-6"
    })
    # 首步故障：该步事务回滚，撤销头仍持久，原匹配保留
    arm_failure("revoke_RELEASE_UNPAID")
    with pytest.raises(RuntimeError):
        svc.revoke_gr("GR-3", idempotency_key="REV-RESUME",
                      failure_point=domain.STEP_RELEASE_UNPAID)
    mid = client.get("/api/matching/ledger").json()
    gr3 = next(row for row in mid["goods_receipts"] if row["gr_id"] == "GR-3")
    assert gr3["status"] == domain.GR_ACTIVE
    assert mid["kpis"]["current_payable"] == "1530.00"

    # 用同一幂等键重试：挂接原撤销单，不新建、不重复
    result = svc.revoke_gr("GR-3", idempotency_key="REV-RESUME")
    assert result["resumed"] is True
    again = svc.revoke_gr("GR-3", idempotency_key="REV-RESUME")
    assert again["revoke_id"] == result["revoke_id"]
    data = client.get("/api/matching/ledger").json()
    assert data["kpis"]["current_payable"] == "1130.00"
    assert len(data["recoveries"]) == 1
    assert data["invariants"]["ok"] is True


def test_resume_after_final_step_interruption(client):
    _seed_and_match(client)
    # 在最后一步 REVOKE_GR 注入故障：前两步已提交，验收单翻转随该步回滚
    arm_failure("revoke_REVOKE_GR")
    with pytest.raises(RuntimeError):
        svc.revoke_gr("GR-1", idempotency_key="REV-VC",
                      failure_point=domain.STEP_REVOKE_GR)
    mid = client.get("/api/matching/ledger").json()
    gr1 = next(row for row in mid["goods_receipts"] if row["gr_id"] == "GR-1")
    assert gr1["status"] == domain.GR_ACTIVE
    link = next(row for row in mid["match_links"] if row["gr_id"] == "GR-1")
    assert link["status"] == domain.LINK_RELEASED
    # 恢复：仅补做最后一步，释放不重复
    resumed = client.post("/api/matching/revocations/resume").json()
    assert resumed["count"] == 1
    assert resumed["resumed"][0]["resumed_steps"] == [domain.STEP_REVOKE_GR]
    done = client.get("/api/matching/ledger").json()
    gr1 = next(row for row in done["goods_receipts"] if row["gr_id"] == "GR-1")
    assert gr1["status"] == domain.GR_REVOKED
    assert done["invariants"]["ok"] is True


def test_payment_idempotent_retry_does_not_double_charge(client):
    _seed_and_match(client)
    link = client.get("/api/matching/ledger").json()["match_links"][0]
    payload = {"link_id": link["link_id"], "amount": "100.00", "idempotency_key": "PAY-IDEM"}
    client.post("/api/matching/payments", json=payload)
    client.post("/api/matching/payments", json=payload)
    data = client.get("/api/matching/ledger").json()
    row = next(item for item in data["match_links"] if item["link_id"] == link["link_id"])
    assert row["paid_amount"] == "100.00"


def test_revoked_gr_cannot_match_and_paid_link_cannot_repay(client):
    _seed_and_match(client)
    client.post("/api/matching/revocations", json={"gr_id": "GR-1", "idempotency_key": "REV-7"})
    blocked = client.post("/api/matching/matches", json={
        "gr_id": "GR-1", "invoice_id": "IV-1", "amount": "10.00"
    })
    assert blocked.status_code == 422
    assert blocked.json()["detail"]["code"] == "GR_REVOKED_NOT_MATCHABLE"

    ledger = client.get("/api/matching/ledger").json()
    paid_link = next(link for link in ledger["match_links"] if link["gr_id"] == "GR-2")
    client.post("/api/matching/payments", json={
        "link_id": paid_link["link_id"], "amount": "530.00", "idempotency_key": "PAY-FULL"
    })
    repay = client.post("/api/matching/payments", json={
        "link_id": paid_link["link_id"], "amount": "1.00", "idempotency_key": "PAY-AGAIN"
    })
    assert repay.status_code == 422
    assert repay.json()["detail"]["code"] == "LINK_NOT_PAYABLE"


def test_cross_tax_group_match_rejected(client):
    _seed_and_match(client)
    resp = client.post("/api/matching/matches", json={
        "gr_id": "GR-3", "invoice_id": "IV-1", "amount": "1.00"
    })
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "TAX_GROUP_MISMATCH"


def test_overmatch_beyond_available_rejected(client):
    _seed_and_match(client)
    resp = client.post("/api/matching/matches", json={
        "gr_id": "GR-1", "invoice_id": "IV-1", "amount": "600.01"
    })
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "GR_BALANCE_EXCEEDED"


def test_auto_match_uses_only_active_available_balances(client):
    client.post("/api/matching/seed")
    result = client.post("/api/matching/auto-match", json={"invoice_id": "IV-1"}).json()
    assert to_decimal(result["amount"]) == Decimal("1130.00")
    # 再来一次没有可用余额，不产生新凭证
    again = client.post("/api/matching/auto-match", json={"invoice_id": "IV-1"}).json()
    assert again["links"] == []
