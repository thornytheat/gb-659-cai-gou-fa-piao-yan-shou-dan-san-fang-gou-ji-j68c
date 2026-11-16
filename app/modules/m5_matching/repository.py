import sqlite3
import threading
from decimal import Decimal
from app.config import DATABASE_PATH
from .money import to_decimal

_LOCK = threading.RLock()

# 测试专用故障注入：进程内单触发标志（恢复点本身持久化在 m5_revocation_step）
_FAILURE_FLAGS: dict[str, bool] = {}


def arm_failure(name: str) -> None:
    _FAILURE_FLAGS[name] = True


def clear_failures() -> None:
    _FAILURE_FLAGS.clear()


SCHEMA = """
CREATE TABLE IF NOT EXISTS m5_goods_receipt (
    gr_id TEXT PRIMARY KEY,
    sku TEXT NOT NULL,
    tax_rate TEXT NOT NULL,
    amount TEXT NOT NULL,
    status TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS m5_invoice (
    invoice_id TEXT PRIMARY KEY,
    sku TEXT NOT NULL,
    tax_rate TEXT NOT NULL,
    amount TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS m5_match_link (
    link_id TEXT PRIMARY KEY,
    gr_id TEXT NOT NULL REFERENCES m5_goods_receipt(gr_id),
    invoice_id TEXT NOT NULL REFERENCES m5_invoice(invoice_id),
    amount TEXT NOT NULL,
    paid_amount TEXT NOT NULL DEFAULT '0.00',
    released_amount TEXT NOT NULL DEFAULT '0.00',
    retained_amount TEXT NOT NULL DEFAULT '0.00',
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS m5_recovery (
    recovery_id TEXT PRIMARY KEY,
    revoke_id TEXT NOT NULL,
    link_id TEXT NOT NULL REFERENCES m5_match_link(link_id),
    gr_id TEXT NOT NULL,
    invoice_id TEXT NOT NULL,
    amount TEXT NOT NULL,
    settled_amount TEXT NOT NULL DEFAULT '0.00',
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS m5_credit_note (
    credit_note_id TEXT PRIMARY KEY,
    recovery_id TEXT NOT NULL REFERENCES m5_recovery(recovery_id),
    amount TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS m5_revocation (
    revoke_id TEXT PRIMARY KEY,
    gr_id TEXT NOT NULL REFERENCES m5_goods_receipt(gr_id),
    amount TEXT NOT NULL,
    expected_version INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS m5_revocation_step (
    revoke_id TEXT NOT NULL REFERENCES m5_revocation(revoke_id),
    step TEXT NOT NULL,
    done INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (revoke_id, step)
);
CREATE TABLE IF NOT EXISTS m5_idempotency (
    idempotency_key TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def init_ledger_db() -> None:
    with _LOCK:
        DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(DATABASE_PATH, timeout=30)
        try:
            db.execute("PRAGMA journal_mode = WAL")
            db.execute("PRAGMA foreign_keys = ON")
            db.executescript(SCHEMA)
        finally:
            db.close()


class LedgerStore:
    """SQLite 访问封装。

    - 简单操作：with LedgerStore() as store 自动开启/提交一个写事务，异常回滚。
    - saga 补偿：用 begin()/commit()/rollback() 把每个步骤切成独立事务，
      已提交的步骤在中断后仍然存在，恢复时跳过，保证不重复冲抵。
    """

    def __init__(self, autocommit: bool = False):
        init_ledger_db()
        self._closed = False
        self.db = sqlite3.connect(DATABASE_PATH, timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.execute("PRAGMA journal_mode = WAL")
        self._in_tx = False
        if not autocommit:
            self.begin()

    def begin(self) -> None:
        if self._in_tx:
            return
        self.db.execute("BEGIN IMMEDIATE")
        self._in_tx = True

    def commit(self) -> None:
        if self._in_tx:
            self.db.execute("COMMIT")
            self._in_tx = False

    def rollback(self) -> None:
        if self._in_tx:
            self.db.execute("ROLLBACK")
            self._in_tx = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close(commit=exc_type is None)

    def close(self, commit: bool):
        if self._closed:
            return
        try:
            if commit:
                self.commit()
            else:
                self.rollback()
        finally:
            self.db.close()
            self._closed = True

    # ---------- 故障注入（测试专用，单触发后自动解除） ----------
    def trigger_failure(self, name: str) -> None:
        if _FAILURE_FLAGS.pop(name, False):
            raise RuntimeError(f"注入故障触发: {name}")

    # ---------- 幂等 ----------
    def cached_response(self, key: str):
        row = self.db.execute(
            "SELECT response_json FROM m5_idempotency WHERE idempotency_key=?", (key,)
        ).fetchone()
        return row["response_json"] if row else None

    def save_response(self, key: str, scope: str, response_json: str, now: str) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO m5_idempotency VALUES(?,?,?,?)",
            (key, scope, response_json, now),
        )

    # ---------- 单据 ----------
    def upsert_gr(self, gr_id: str, sku: str, tax_rate: str, amount: str, now: str) -> None:
        self.db.execute(
            "INSERT INTO m5_goods_receipt(gr_id, sku, tax_rate, amount, status, created_at) "
            "VALUES(?,?,?,?,'ACTIVE',?) ON CONFLICT(gr_id) DO UPDATE SET "
            "sku=excluded.sku, tax_rate=excluded.tax_rate, amount=excluded.amount",
            (gr_id, sku, tax_rate, amount, now),
        )

    def upsert_invoice(self, invoice_id: str, sku: str, tax_rate: str, amount: str, now: str) -> None:
        self.db.execute(
            "INSERT INTO m5_invoice(invoice_id, sku, tax_rate, amount, created_at) "
            "VALUES(?,?,?,?,?) ON CONFLICT(invoice_id) DO UPDATE SET "
            "sku=excluded.sku, tax_rate=excluded.tax_rate, amount=excluded.amount",
            (invoice_id, sku, tax_rate, amount, now),
        )

    def get_gr(self, gr_id: str):
        return self.db.execute(
            "SELECT * FROM m5_goods_receipt WHERE gr_id=?", (gr_id,)
        ).fetchone()

    def get_invoice(self, invoice_id: str):
        return self.db.execute(
            "SELECT * FROM m5_invoice WHERE invoice_id=?", (invoice_id,)
        ).fetchone()

    def list_gr(self):
        return self.db.execute(
            "SELECT * FROM m5_goods_receipt ORDER BY gr_id"
        ).fetchall()

    def list_invoices(self):
        return self.db.execute("SELECT * FROM m5_invoice ORDER BY invoice_id").fetchall()

    def active_links_for_gr(self, gr_id: str):
        return self.db.execute(
            "SELECT * FROM m5_match_link WHERE gr_id=? AND status='ACTIVE' ORDER BY link_id",
            (gr_id,),
        ).fetchall()

    def add_link(self, link_id: str, gr_id: str, invoice_id: str, amount: str, now: str) -> None:
        self.db.execute(
            "INSERT INTO m5_match_link(link_id, gr_id, invoice_id, amount, status, created_at) "
            "VALUES(?,?,?,?,'ACTIVE',?)",
            (link_id, gr_id, invoice_id, amount, now),
        )

    def get_link(self, link_id: str):
        return self.db.execute(
            "SELECT * FROM m5_match_link WHERE link_id=?", (link_id,)
        ).fetchone()

    def list_links(self):
        return self.db.execute("SELECT * FROM m5_match_link ORDER BY link_id").fetchall()

    def mark_link_paid(self, link_id: str, new_paid: str, status: str) -> None:
        self.db.execute(
            "UPDATE m5_match_link SET paid_amount=?, status=? WHERE link_id=?",
            (new_paid, status, link_id),
        )

    def release_link(self, link_id: str, released_amount: str, status: str) -> None:
        self.db.execute(
            "UPDATE m5_match_link SET released_amount=?, status=? WHERE link_id=?",
            (released_amount, status, link_id),
        )

    def retain_link(self, link_id: str, retained_amount: str, status: str) -> None:
        self.db.execute(
            "UPDATE m5_match_link SET retained_amount=?, status=? WHERE link_id=?",
            (retained_amount, status, link_id),
        )

    def add_recovery(self, recovery_id: str, revoke_id: str, link_id: str,
                     gr_id: str, invoice_id: str, amount: str) -> None:
        self.db.execute(
            "INSERT INTO m5_recovery(recovery_id, revoke_id, link_id, gr_id, invoice_id, "
            "amount, status) VALUES(?,?,?,?,?,?,'PENDING')",
            (recovery_id, revoke_id, link_id, gr_id, invoice_id, amount),
        )

    def get_recovery(self, recovery_id: str):
        return self.db.execute(
            "SELECT * FROM m5_recovery WHERE recovery_id=?", (recovery_id,)
        ).fetchone()

    def list_recovery(self):
        return self.db.execute(
            "SELECT * FROM m5_recovery ORDER BY recovery_id"
        ).fetchall()

    def settle_recovery(self, recovery_id: str, new_settled: str, status: str) -> None:
        self.db.execute(
            "UPDATE m5_recovery SET settled_amount=?, status=? WHERE recovery_id=?",
            (new_settled, status, recovery_id),
        )

    def add_credit_note(self, credit_note_id: str, recovery_id: str,
                        amount: str, key: str, now: str) -> None:
        self.db.execute(
            "INSERT INTO m5_credit_note(credit_note_id, recovery_id, amount, "
            "idempotency_key, created_at) VALUES(?,?,?,?,?)",
            (credit_note_id, recovery_id, amount, key, now),
        )

    def list_credit_notes(self):
        return self.db.execute(
            "SELECT * FROM m5_credit_note ORDER BY credit_note_id"
        ).fetchall()

    # ---------- 撤销 saga ----------
    def create_revocation(self, revoke_id: str, gr_id: str, amount: str,
                          expected_version: int, now: str) -> None:
        self.db.execute(
            "INSERT INTO m5_revocation(revoke_id, gr_id, amount, expected_version, "
            "status, created_at) VALUES(?,?,?,?,'IN_PROGRESS',?)",
            (revoke_id, gr_id, amount, expected_version, now),
        )
        for step in ("RELEASE_UNPAID", "RETAIN_PAID", "REVOKE_GR"):
            self.db.execute(
                "INSERT OR IGNORE INTO m5_revocation_step(revoke_id, step, done) "
                "VALUES(?,?,0)",
                (revoke_id, step),
            )

    def get_revocation(self, revoke_id: str):
        return self.db.execute(
            "SELECT * FROM m5_revocation WHERE revoke_id=?", (revoke_id,)
        ).fetchone()

    def find_revocation_for_gr(self, gr_id: str):
        return self.db.execute(
            "SELECT * FROM m5_revocation WHERE gr_id=? ORDER BY created_at DESC, revoke_id DESC",
            (gr_id,),
        ).fetchall()

    def step_done(self, revoke_id: str, step: str) -> bool:
        row = self.db.execute(
            "SELECT done FROM m5_revocation_step WHERE revoke_id=? AND step=?",
            (revoke_id, step),
        ).fetchone()
        return bool(row and row["done"])

    def mark_step_done(self, revoke_id: str, step: str) -> None:
        self.db.execute(
            "UPDATE m5_revocation_step SET done=1 WHERE revoke_id=? AND step=?",
            (revoke_id, step),
        )

    def complete_revocation(self, revoke_id: str) -> None:
        self.db.execute(
            "UPDATE m5_revocation SET status='COMPLETED' WHERE revoke_id=?",
            (revoke_id,),
        )

    def list_in_progress_revocations(self):
        return self.db.execute(
            "SELECT * FROM m5_revocation WHERE status='IN_PROGRESS' ORDER BY revoke_id"
        ).fetchall()

    def bump_gr_version_revoke(self, gr_id: str, expected_version: int) -> bool:
        """乐观锁：版本过期则拒绝并发撤销。返回是否更新成功。"""
        cursor = self.db.execute(
            "UPDATE m5_goods_receipt SET status='REVOKED', version=version+1 "
            "WHERE gr_id=? AND status='ACTIVE' AND version=?",
            (gr_id, expected_version),
        )
        return cursor.rowcount == 1

    def sum_active_gr_amount(self) -> Decimal:
        rows = self.db.execute(
            "SELECT amount FROM m5_goods_receipt WHERE status='ACTIVE'"
        ).fetchall()
        return sum((to_decimal(row["amount"]) for row in rows), Decimal("0.00"))
