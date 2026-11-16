import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from app.errors import DomainError
from . import domain
from .money import CENT, ZERO, as_text, to_decimal
from .repository import LedgerStore
from .solver import subset_match


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12].upper()}"


# ---------------------------------------------------------------------------
# 单据登记：匹配页面接入真实单据
# ---------------------------------------------------------------------------
def register_gr(gr_id: str, sku: str, tax_rate: str, amount) -> dict:
    amount = to_decimal(amount)
    if amount <= ZERO:
        raise DomainError("GR_AMOUNT_INVALID", "验收单金额必须为正")
    if not gr_id or not sku:
        raise DomainError("GR_FIELD_MISSING", "验收单编号与货号必填")
    with LedgerStore() as store:
        existing = store.get_gr(gr_id)
        if existing is not None and existing["status"] != domain.GR_ACTIVE:
            raise DomainError(
                "GR_FROZEN",
                f"验收单 {gr_id} 已撤销，禁止改写；如需新单据请使用新编号",
                409,
            )
        store.upsert_gr(gr_id, sku, str(to_decimal(tax_rate)), as_text(amount), _now())
        return {"gr_id": gr_id, "amount": as_text(amount), "status": domain.GR_ACTIVE}


def register_invoice(invoice_id: str, sku: str, tax_rate: str, amount) -> dict:
    amount = to_decimal(amount)
    if amount <= ZERO:
        raise DomainError("INVOICE_AMOUNT_INVALID", "发票金额必须为正")
    if not invoice_id or not sku:
        raise DomainError("INVOICE_FIELD_MISSING", "发票编号与货号必填")
    with LedgerStore() as store:
        store.upsert_invoice(
            invoice_id, sku, str(to_decimal(tax_rate)), as_text(amount), _now()
        )
        return {"invoice_id": invoice_id, "amount": as_text(amount)}


# ---------------------------------------------------------------------------
# 匹配登记：占用可匹配余额，凭证永久保留不删除
# ---------------------------------------------------------------------------
def _gr_available(store: LedgerStore, gr_row) -> Decimal:
    if gr_row["status"] != domain.GR_ACTIVE:
        return ZERO
    occupied = ZERO
    for link in store.list_links():
        if link["gr_id"] != gr_row["gr_id"] or link["status"] == domain.LINK_RELEASED:
            continue
        occupied += to_decimal(link["amount"]) - to_decimal(link["released_amount"])
    return to_decimal(gr_row["amount"]) - occupied


def _invoice_available(store: LedgerStore, invoice_row) -> Decimal:
    occupied = ZERO
    for link in store.list_links():
        if link["invoice_id"] != invoice_row["invoice_id"] or link["status"] == domain.LINK_RELEASED:
            continue
        occupied += to_decimal(link["amount"]) - to_decimal(link["released_amount"])
    return to_decimal(invoice_row["amount"]) - occupied


def register_match(gr_id: str, invoice_id: str, amount) -> dict:
    amount = to_decimal(amount)
    if amount <= ZERO:
        raise DomainError("MATCH_AMOUNT_INVALID", "匹配金额必须为正")
    with LedgerStore() as store:
        gr = store.get_gr(gr_id)
        invoice = store.get_invoice(invoice_id)
        if gr is None:
            raise DomainError("GR_NOT_FOUND", f"验收单 {gr_id} 不存在", 404)
        if invoice is None:
            raise DomainError("INVOICE_NOT_FOUND", f"发票 {invoice_id} 不存在", 404)
        if gr["status"] != domain.GR_ACTIVE:
            raise DomainError("GR_REVOKED_NOT_MATCHABLE", "已撤销验收单不能新增匹配")
        if gr["sku"] != invoice["sku"] or to_decimal(gr["tax_rate"]) != to_decimal(invoice["tax_rate"]):
            raise DomainError("TAX_GROUP_MISMATCH", "货号或税率不一致，禁止跨组匹配")
        gr_free = _gr_available(store, gr)
        invoice_free = _invoice_available(store, invoice)
        if amount > gr_free:
            raise DomainError(
                "GR_BALANCE_EXCEEDED",
                f"超过验收单可匹配余额：可用 {as_text(gr_free)}，申请 {as_text(amount)}",
            )
        if amount > invoice_free:
            raise DomainError(
                "INVOICE_BALANCE_EXCEEDED",
                f"超过发票可匹配余额：可用 {as_text(invoice_free)}，申请 {as_text(amount)}",
            )
        link_id = _new_id("LNK")
        store.add_link(link_id, gr_id, invoice_id, as_text(amount), _now())
        return {
            "link_id": link_id,
            "gr_id": gr_id,
            "invoice_id": invoice_id,
            "amount": as_text(amount),
            "status": domain.LINK_ACTIVE,
        }


def auto_match(invoice_id: str) -> dict:
    """在相同货号/税率分组内，用 ACTIVE 验收单的可用余额做组合匹配。"""
    with LedgerStore() as store:
        invoice = store.get_invoice(invoice_id)
        if invoice is None:
            raise DomainError("INVOICE_NOT_FOUND", f"发票 {invoice_id} 不存在", 404)
        target = _invoice_available(store, invoice)
        if target <= ZERO:
            return {"invoice_id": invoice_id, "links": [], "amount": "0.00"}
        candidates = []
        for gr in store.list_gr():
            if (
                gr["status"] == domain.GR_ACTIVE
                and gr["sku"] == invoice["sku"]
                and to_decimal(gr["tax_rate"]) == to_decimal(invoice["tax_rate"])
            ):
                available = _gr_available(store, gr)
                if available > ZERO:
                    candidates.append(
                        {"id": gr["gr_id"], "amount": as_text(available)}
                    )
        selected = subset_match(candidates, target)
        created = []
        for row in selected:
            link_id = _new_id("LNK")
            store.add_link(
                link_id, row["id"], invoice_id, as_text(to_decimal(row["amount"])), _now()
            )
            created.append(
                {"link_id": link_id, "gr_id": row["id"], "amount": row["amount"]}
            )
        total = sum((to_decimal(item["amount"]) for item in created), ZERO)
        return {
            "invoice_id": invoice_id,
            "links": created,
            "amount": as_text(total),
        }


# ---------------------------------------------------------------------------
# 付款登记：只允许在 ACTIVE 凭证上登记
# ---------------------------------------------------------------------------
def register_payment(link_id: str, amount, idempotency_key: str | None = None) -> dict:
    amount = to_decimal(amount)
    if amount <= ZERO:
        raise DomainError("PAYMENT_AMOUNT_INVALID", "付款金额必须为正")
    key = idempotency_key or f"PAY-{link_id}-{as_text(amount)}-{uuid.uuid4().hex[:8]}"
    with LedgerStore() as store:
        cached = store.cached_response(key)
        if cached is not None:
            return json.loads(cached)
        link = store.get_link(link_id)
        if link is None:
            raise DomainError("LINK_NOT_FOUND", f"匹配凭证 {link_id} 不存在", 404)
        if link["status"] != domain.LINK_ACTIVE:
            raise DomainError(
                "LINK_NOT_PAYABLE",
                f"凭证状态 {link['status']} 不可登记付款；已付款关联被原样保留",
            )
        unpaid = to_decimal(link["amount"]) - to_decimal(link["paid_amount"])
        if amount > unpaid:
            raise DomainError(
                "PAYMENT_EXCEEDS_LINK",
                f"付款超过凭证未付额：未付 {as_text(unpaid)}，申请 {as_text(amount)}",
            )
        new_paid = to_decimal(link["paid_amount"]) + amount
        new_status = (
            domain.LINK_PAID if new_paid == to_decimal(link["amount"]) else domain.LINK_ACTIVE
        )
        store.mark_link_paid(link_id, as_text(new_paid), new_status)
        result = {
            "link_id": link_id,
            "paid_amount": as_text(new_paid),
            "status": new_status,
            "idempotency_key": key,
        }
        store.save_response(key, "PAYMENT", json.dumps(result, ensure_ascii=False), _now())
        return result


# ---------------------------------------------------------------------------
# 撤销补偿：saga 三步骤，中断可恢复，重试不重复冲抵，失败保留原匹配
# ---------------------------------------------------------------------------
def _do_release_unpaid(store: LedgerStore, revoke_id: str) -> list:
    """步骤一：未付款关联才释放（部分付款按比例拆分，未付部分释放）。"""
    revocation = store.get_revocation(revoke_id)
    gr_id = revocation["gr_id"]
    effects = []
    for link in store.active_links_for_gr(gr_id):
        total = to_decimal(link["amount"])
        paid = to_decimal(link["paid_amount"])
        unpaid = total - paid
        if unpaid <= ZERO:
            continue
        if paid == ZERO:
            store.release_link(link["link_id"], as_text(total), domain.LINK_RELEASED)
            effects.append(
                {"link_id": link["link_id"], "action": "RELEASED", "amount": as_text(total)}
            )
        else:
            # 部分付款：未付部分释放，已付部分保留，凭证不删除
            store.release_link(link["link_id"], as_text(unpaid), domain.LINK_PARTIAL_RETAINED)
            store.retain_link(link["link_id"], as_text(paid), domain.LINK_PARTIAL_RETAINED)
            effects.append(
                {
                    "link_id": link["link_id"],
                    "action": "PARTIAL_RELEASED",
                    "released": as_text(unpaid),
                    "retained": as_text(paid),
                }
            )
    return effects


def _do_retain_paid(store: LedgerStore, revoke_id: str) -> list:
    """步骤二：已付款关联原样保留，逐笔登记待追回（红字到账前不增加可匹配余额）。"""
    revocation = store.get_revocation(revoke_id)
    gr_id = revocation["gr_id"]
    effects = []
    for link in store.list_links():
        if link["gr_id"] != gr_id:
            continue
        if link["status"] not in (domain.LINK_PAID, domain.LINK_PARTIAL_RETAINED):
            continue
        if link["status"] == domain.LINK_PAID:
            # 全额付款凭证：已付额整体转为保留额，凭证原样保留
            retained = to_decimal(link["paid_amount"])
            store.retain_link(link["link_id"], as_text(retained), domain.LINK_RETAINED)
        else:
            retained = to_decimal(link["retained_amount"])
        if retained <= ZERO:
            continue
        recovery_id = _new_id("REC")
        store.add_recovery(
            recovery_id,
            revoke_id,
            link["link_id"],
            gr_id,
            link["invoice_id"],
            as_text(retained),
        )
        effects.append(
            {
                "link_id": link["link_id"],
                "action": "RETAINED",
                "recovery_id": recovery_id,
                "amount": as_text(retained),
            }
        )
    return effects


def _do_revoke_gr(store: LedgerStore, revoke_id: str) -> None:
    """步骤三：乐观锁翻转验收单；版本过期说明并发撤销/改写，拒绝并回滚。"""
    revocation = store.get_revocation(revoke_id)
    ok = store.bump_gr_version_revoke(
        revocation["gr_id"], revocation["expected_version"]
    )
    if not ok:
        gr = store.get_gr(revocation["gr_id"])
        raise DomainError(
            "GR_VERSION_CONFLICT",
            f"验收单状态已过期（当前版本 {gr['version']}，期望 {revocation['expected_version']}），拒绝并发撤销",
            409,
        )


def _run_saga(store: LedgerStore, revoke_id: str, failure_point: str | None) -> list:
    """逐步骤执行 saga；每步一个独立事务，提交后即持久，中断后可安全恢复。"""
    effects: list = []
    steps = [
        (domain.STEP_RELEASE_UNPAID, lambda: _do_release_unpaid(store, revoke_id)),
        (domain.STEP_RETAIN_PAID, lambda: _do_retain_paid(store, revoke_id)),
        (domain.STEP_REVOKE_GR, lambda: _do_revoke_gr(store, revoke_id)),
    ]
    for step, action in steps:
        if store.step_done(revoke_id, step):
            continue
        store.begin()
        try:
            if failure_point == step:
                store.trigger_failure(f"revoke_{step}")
            result = action()
            store.mark_step_done(revoke_id, step)
            store.commit()
        except Exception:
            store.rollback()
            raise
        if result:
            effects.extend(result)
    store.begin()
    try:
        store.complete_revocation(revoke_id)
        store.commit()
    except Exception:
        store.rollback()
        raise
    return effects


def revoke_gr(gr_id: str, idempotency_key: str | None = None,
              expected_version: int | None = None,
              failure_point: str | None = None) -> dict:
    key = idempotency_key or f"REVOKE-{gr_id}-{uuid.uuid4().hex[:8]}"
    # 长连接、autocommit 模式：由 saga 自己管理每步事务
    store = LedgerStore(autocommit=True)
    try:
        store.begin()
        cached = store.cached_response(key)
        if cached is not None:
            store.commit()
            return json.loads(cached)
        # 同幂等键的上次尝试若在 saga 中途中断：挂接到原撤销单恢复，而不是新建
        existing = store.find_revocation_for_gr(gr_id)
        in_flight = next(
            (row for row in existing if row["status"] == "IN_PROGRESS"), None
        )
        if in_flight is not None:
            store.commit()
            effects = _run_saga(store, in_flight["revoke_id"], failure_point)
            result = {
                "revoke_id": in_flight["revoke_id"],
                "gr_id": gr_id,
                "deducted_amount": in_flight["amount"],
                "effects": effects,
                "status": "COMPLETED",
                "idempotency_key": key,
                "resumed": True,
            }
            store.begin()
            store.save_response(key, "REVOKE", json.dumps(result, ensure_ascii=False), _now())
            store.commit()
            return result
        gr = store.get_gr(gr_id)
        if gr is None:
            store.rollback()
            raise DomainError("GR_NOT_FOUND", f"验收单 {gr_id} 不存在", 404)
        if gr["status"] != domain.GR_ACTIVE:
            store.rollback()
            raise DomainError(
                "GR_NOT_REVOKABLE",
                f"验收单状态为 {gr['status']}，仅 ACTIVE 可撤销",
                409,
            )
        if expected_version is not None and expected_version != gr["version"]:
            store.rollback()
            raise DomainError(
                "GR_VERSION_CONFLICT",
                f"验收单版本过期：当前 {gr['version']}，请求 {expected_version}",
                409,
            )
        revoke_id = _new_id("REV")
        store.create_revocation(revoke_id, gr_id, gr["amount"], gr["version"], _now())
        store.commit()

        # 每步独立事务：中断时已完成步骤保留，未完成步骤回滚，原匹配不受损
        effects = _run_saga(store, revoke_id, failure_point)

        store.begin()
        result = {
            "revoke_id": revoke_id,
            "gr_id": gr_id,
            "deducted_amount": gr["amount"],
            "effects": effects,
            "status": "COMPLETED",
            "idempotency_key": key,
        }
        store.save_response(key, "REVOKE", json.dumps(result, ensure_ascii=False), _now())
        store.commit()
        return result
    except Exception:
        store.rollback()
        raise
    finally:
        store.db.close()
        store._closed = True


def resume_revocations() -> dict:
    """补偿写入中断后恢复：只推进未完成步骤，已完成步骤天然不重复执行。"""
    resumed = []
    store = LedgerStore(autocommit=True)
    try:
        pending = store.list_in_progress_revocations()
        for row in pending:
            before = {
                step: store.step_done(row["revoke_id"], step) for step in domain.SAGA_STEPS
            }
            effects = _run_saga(store, row["revoke_id"], None)
            resumed.append(
                {
                    "revoke_id": row["revoke_id"],
                    "gr_id": row["gr_id"],
                    "resumed_steps": [
                        step for step, done in before.items() if not done
                    ],
                    "effects": effects,
                }
            )
        return {"resumed": resumed, "count": len(resumed)}
    finally:
        store.db.close()
        store._closed = True


# ---------------------------------------------------------------------------
# 红字冲抵：到账前只列待追回，到账后结清，任何阶段都不增加可匹配余额
# ---------------------------------------------------------------------------
def register_credit_note(recovery_id: str, amount, idempotency_key: str) -> dict:
    amount = to_decimal(amount)
    if amount <= ZERO:
        raise DomainError("CREDIT_AMOUNT_INVALID", "红字冲抵金额必须为正")
    if not idempotency_key:
        raise DomainError("IDEMPOTENCY_REQUIRED", "红字冲抵必须提供幂等键，重试不重复冲抵")
    with LedgerStore() as store:
        cached = store.cached_response(idempotency_key)
        if cached is not None:
            return json.loads(cached)
        recovery = store.get_recovery(recovery_id)
        if recovery is None:
            raise DomainError("RECOVERY_NOT_FOUND", f"待追回 {recovery_id} 不存在", 404)
        outstanding = to_decimal(recovery["amount"]) - to_decimal(recovery["settled_amount"])
        if amount > outstanding:
            raise DomainError(
                "CREDIT_EXCEEDS_RECOVERY",
                f"红字金额超过待追回余额：待追回 {as_text(outstanding)}，申请 {as_text(amount)}",
            )
        credit_id = _new_id("CN")
        store.add_credit_note(
            credit_id, recovery_id, as_text(amount), idempotency_key, _now()
        )
        new_settled = to_decimal(recovery["settled_amount"]) + amount
        new_status = (
            domain.RECOVERY_SETTLED
            if new_settled == to_decimal(recovery["amount"])
            else domain.RECOVERY_PENDING
        )
        store.settle_recovery(recovery_id, as_text(new_settled), new_status)
        result = {
            "credit_note_id": credit_id,
            "recovery_id": recovery_id,
            "amount": as_text(amount),
            "settled_amount": as_text(new_settled),
            "status": new_status,
            "idempotency_key": idempotency_key,
        }
        store.save_response(
            idempotency_key, "CREDIT_NOTE", json.dumps(result, ensure_ascii=False), _now()
        )
        return result


# ---------------------------------------------------------------------------
# 对账派生：原额、占用、可用、待追回
# ---------------------------------------------------------------------------
def _link_live_amount(link) -> Decimal:
    """仍占用余额的凭证金额（释放部分不再占用）。"""
    return to_decimal(link["amount"]) - to_decimal(link["released_amount"])


def ledger() -> dict:
    with LedgerStore() as store:
        links = store.list_links()
        gr_rows = store.list_gr()
        invoice_rows = store.list_invoices()
        recovery_rows = store.list_recovery()

        gr_view = []
        current_payable = ZERO
        total_original = ZERO
        total_revoked = ZERO
        for gr in gr_rows:
            original = to_decimal(gr["amount"])
            occupied = ZERO
            paid = ZERO
            retained = ZERO
            for link in links:
                if link["gr_id"] != gr["gr_id"]:
                    continue
                if link["status"] != domain.LINK_RELEASED:
                    occupied += _link_live_amount(link)
                paid += to_decimal(link["paid_amount"])
                retained += to_decimal(link["retained_amount"])
            available = (
                original - occupied if gr["status"] == domain.GR_ACTIVE else ZERO
            )
            total_original += original
            if gr["status"] == domain.GR_ACTIVE:
                current_payable += original
            else:
                total_revoked += original
            gr_view.append(
                {
                    "gr_id": gr["gr_id"],
                    "sku": gr["sku"],
                    "tax_rate": gr["tax_rate"],
                    "original_amount": as_text(original),
                    "occupied_amount": as_text(occupied),
                    "available_amount": as_text(available),
                    "paid_amount": as_text(paid),
                    "retained_amount": as_text(retained),
                    "status": gr["status"],
                    "version": gr["version"],
                }
            )

        invoice_view = []
        for invoice in invoice_rows:
            original = to_decimal(invoice["amount"])
            occupied = ZERO
            paid = ZERO
            for link in links:
                if link["invoice_id"] != invoice["invoice_id"]:
                    continue
                if link["status"] != domain.LINK_RELEASED:
                    occupied += _link_live_amount(link)
                paid += to_decimal(link["paid_amount"])
            invoice_view.append(
                {
                    "invoice_id": invoice["invoice_id"],
                    "sku": invoice["sku"],
                    "tax_rate": invoice["tax_rate"],
                    "original_amount": as_text(original),
                    "occupied_amount": as_text(occupied),
                    "available_amount": as_text(original - occupied),
                    "paid_amount": as_text(paid),
                }
            )

        link_view = [
            {
                "link_id": link["link_id"],
                "gr_id": link["gr_id"],
                "invoice_id": link["invoice_id"],
                "amount": as_text(to_decimal(link["amount"])),
                "paid_amount": as_text(to_decimal(link["paid_amount"])),
                "released_amount": as_text(to_decimal(link["released_amount"])),
                "retained_amount": as_text(to_decimal(link["retained_amount"])),
                "status": link["status"],
            }
            for link in links
        ]

        recovery_view = []
        pending_total = ZERO
        settled_total = ZERO
        for recovery in recovery_rows:
            amount = to_decimal(recovery["amount"])
            settled = to_decimal(recovery["settled_amount"])
            if recovery["status"] == domain.RECOVERY_PENDING:
                pending_total += amount - settled
            settled_total += settled
            recovery_view.append(
                {
                    "recovery_id": recovery["recovery_id"],
                    "revoke_id": recovery["revoke_id"],
                    "link_id": recovery["link_id"],
                    "gr_id": recovery["gr_id"],
                    "invoice_id": recovery["invoice_id"],
                    "amount": as_text(amount),
                    "settled_amount": as_text(settled),
                    "outstanding_amount": as_text(amount - settled),
                    "status": recovery["status"],
                }
            )

        credit_view = [
            {
                "credit_note_id": row["credit_note_id"],
                "recovery_id": row["recovery_id"],
                "amount": as_text(to_decimal(row["amount"])),
                "idempotency_key": row["idempotency_key"],
            }
            for row in store.list_credit_notes()
        ]

        invariants = _check_invariants(
            gr_view, invoice_view, link_view, recovery_view, credit_view,
            current_payable, total_revoked,
        )
        return {
            "kpis": {
                "current_payable": as_text(current_payable),
                "original_total": as_text(total_original),
                "revoked_total": as_text(total_revoked),
                "pending_recovery": as_text(pending_total),
                "settled_recovery": as_text(settled_total),
            },
            "goods_receipts": gr_view,
            "invoices": invoice_view,
            "match_links": link_view,
            "recoveries": recovery_view,
            "credit_notes": credit_view,
            "invariants": invariants,
        }


def _check_invariants(gr_view, invoice_view, link_view, recovery_view,
                      credit_view, current_payable, total_revoked) -> dict:
    """守恒校验：任一破坏即报告，供页面与测试对账。"""
    problems = []

    # 1. 当前应付 = 原额合计 - 撤销金额
    original_sum = sum((to_decimal(row["original_amount"]) for row in gr_view), ZERO)
    if current_payable + total_revoked != original_sum:
        problems.append("当前应付 + 撤销金额 != 验收原额合计")

    # 2. 每张 ACTIVE 验收单：原额 = 占用 + 可用
    for row in gr_view:
        if row["status"] == domain.GR_ACTIVE:
            if to_decimal(row["original_amount"]) != to_decimal(row["occupied_amount"]) + to_decimal(
                row["available_amount"]
            ):
                problems.append(f"验收单 {row['gr_id']} 原额≠占用+可用")
        elif to_decimal(row["available_amount"]) != ZERO:
            problems.append(f"已撤销验收单 {row['gr_id']} 仍有可用余额")

    # 3. 每张发票：原额 = 占用 + 可用
    for row in invoice_view:
        if to_decimal(row["original_amount"]) != to_decimal(row["occupied_amount"]) + to_decimal(
            row["available_amount"]
        ):
            problems.append(f"发票 {row['invoice_id']} 原额≠占用+可用")

    # 4. 凭证守恒：原额 = 已付 + 释放 + 剩余未付；保留额 ≤ 已付
    for link in link_view:
        amount = to_decimal(link["amount"])
        paid = to_decimal(link["paid_amount"])
        released = to_decimal(link["released_amount"])
        retained = to_decimal(link["retained_amount"])
        live = amount - released
        if paid > live + CENT:
            problems.append(f"凭证 {link['link_id']} 已付超过存活金额")
        if retained > paid + CENT:
            problems.append(f"凭证 {link['link_id']} 保留额超过已付额")
        if link["status"] == domain.LINK_RELEASED and (paid != ZERO or retained != ZERO):
            problems.append(f"凭证 {link['link_id']} 已释放但仍带付款/保留")

    # 5. 待追回：已结 ≤ 原额；红字合计 = 已结金额；红字不回补任何可用余额
    credit_sum = sum((to_decimal(row["amount"]) for row in credit_view), ZERO)
    settled_sum = sum((to_decimal(row["settled_amount"]) for row in recovery_view), ZERO)
    if credit_sum != settled_sum:
        problems.append("红字冲抵合计 != 待追回已结合计")
    for row in recovery_view:
        if to_decimal(row["settled_amount"]) > to_decimal(row["amount"]):
            problems.append(f"待追回 {row['recovery_id']} 已结超过原额")

    return {"ok": not problems, "problems": problems}


# ---------------------------------------------------------------------------
# 演示种子：幂等写入一套真实单据
# ---------------------------------------------------------------------------
def seed_demo() -> dict:
    register_gr("GR-1", "SKU-A", "0.13", "600.00")
    register_gr("GR-2", "SKU-A", "0.13", "530.00")
    register_gr("GR-3", "SKU-B", "0.09", "400.00")
    register_invoice("IV-1", "SKU-A", "0.13", "1130.00")
    register_invoice("IV-2", "SKU-B", "0.09", "400.00")
    return {"seeded": ["GR-1", "GR-2", "GR-3", "IV-1", "IV-2"]}
