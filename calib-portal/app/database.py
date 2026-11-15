"""SQLite 持久化：批次、逐行进度、允差规则与符合性签发。

进度按 (门户地址, 设备编号, 校准日期) 唯一持久化，
浏览器关闭、服务重启、重新上传同一 CSV 都可以续办。

符合性评定与签发使用同一个数据库读事务取得「进度 + 活动规则」快照；
签发使用 IMMEDIATE 写事务，在读到的快照上重新校验指纹后才落库，
因此后台补录与规则替换并发时不会出现「进度与规则来自不同时刻」的判定。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator

from . import config, conformity

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    portal_url TEXT NOT NULL,
    filename TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES batches(id),
    portal_url TEXT NOT NULL,
    device_no TEXT NOT NULL,
    cal_date TEXT NOT NULL,
    indication_error TEXT NOT NULL,
    line_no INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    detail TEXT NOT NULL DEFAULT '',
    portal_record_url TEXT NOT NULL DEFAULT '',
    portal_record_id TEXT NOT NULL DEFAULT '',
    screenshot TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    UNIQUE(portal_url, device_no, cal_date)
);
CREATE INDEX IF NOT EXISTS idx_rows_batch ON rows(batch_id);
CREATE INDEX IF NOT EXISTS idx_rows_status ON rows(status);
CREATE TABLE IF NOT EXISTS batch_ignored_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES batches(id),
    line_no INTEGER NOT NULL,
    device_no TEXT NOT NULL,
    cal_date TEXT NOT NULL,
    indication_error TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ignored_lines_batch
    ON batch_ignored_lines(batch_id);
CREATE TABLE IF NOT EXISTS rulesets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    rule_count INTEGER NOT NULL,
    uploaded_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_rulesets_active
    ON rulesets(active) WHERE active=1;
CREATE TABLE IF NOT EXISTS rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ruleset_id INTEGER NOT NULL REFERENCES rulesets(id),
    line_no INTEGER NOT NULL,
    device_no TEXT NOT NULL,
    tolerance TEXT NOT NULL,
    uncertainty TEXT NOT NULL,
    tolerance_raw TEXT NOT NULL,
    uncertainty_raw TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rules_ruleset ON rules(ruleset_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_rules_ruleset_device
    ON rules(ruleset_id, device_no);
CREATE TABLE IF NOT EXISTS signatures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES batches(id),
    ruleset_id INTEGER NOT NULL REFERENCES rulesets(id),
    signed_at TEXT NOT NULL,
    signed_by TEXT NOT NULL DEFAULT '',
    progress_fingerprint TEXT NOT NULL,
    rules_fingerprint TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'valid',
    stale_at TEXT NOT NULL DEFAULT '',
    stale_reason TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_signatures_batch ON signatures(batch_id);
CREATE INDEX IF NOT EXISTS idx_signatures_status ON signatures(status);
"""


def now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


def init() -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    with conn() as c:
        c.executescript(SCHEMA)


@contextmanager
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(config.DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    try:
        yield c
        c.commit()
    finally:
        c.close()


class SourceChanged(Exception):
    """签发时发现进度或规则已与页面展示的快照不同。"""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class SigningUnavailable(Exception):
    """不满足签发条件（有待核行 / 无已完成行 / 无规则等）。"""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


def create_batch(portal_url: str, filename: str) -> int:
    with conn() as c:
        cur = c.execute(
            "INSERT INTO batches(portal_url, filename, status, created_at,"
            " updated_at) VALUES (?, ?, 'running', ?, ?)",
            (portal_url, filename, now(), now()),
        )
        return int(cur.lastrowid)


def get_batch(batch_id: int) -> sqlite3.Row | None:
    with conn() as c:
        return c.execute(
            "SELECT * FROM batches WHERE id=?", (batch_id,)
        ).fetchone()


def list_batches() -> list[sqlite3.Row]:
    with conn() as c:
        return list(
            c.execute("SELECT * FROM batches ORDER BY id DESC").fetchall()
        )


def set_batch_status(batch_id: int, status: str, note: str | None = None) -> None:
    with conn() as c:
        if note is None:
            c.execute(
                "UPDATE batches SET status=?, updated_at=? WHERE id=?",
                (status, now(), batch_id),
            )
        else:
            c.execute(
                "UPDATE batches SET status=?, note=?, updated_at=? WHERE id=?",
                (status, note, now(), batch_id),
            )


def upsert_row(
    batch_id: int,
    portal_url: str,
    device_no: str,
    cal_date: str,
    indication_error: str,
    line_no: int,
) -> tuple[int, str]:
    """插入一行；若 (门户, 设备, 日期) 已存在则复用原进度。

    返回 (row_id, action)，action 为 'created' / 'reused' / 'dup_in_csv'。
    """
    with conn() as c:
        existing = c.execute(
            "SELECT * FROM rows WHERE portal_url=? AND device_no=? AND cal_date=?",
            (portal_url, device_no, cal_date),
        ).fetchone()
        if existing is not None:
            if existing["batch_id"] == batch_id:
                # 同一个 CSV 文件内出现重复键
                return existing["id"], "dup_in_csv"
            return existing["id"], "reused"
        cur = c.execute(
            "INSERT INTO rows(batch_id, portal_url, device_no, cal_date,"
            " indication_error, line_no, status, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
            (batch_id, portal_url, device_no, cal_date,
             indication_error, line_no, now()),
        )
        return int(cur.lastrowid), "created"


def add_ignored_line(
    batch_id: int,
    line_no: int,
    device_no: str,
    cal_date: str,
    indication_error: str,
    reason: str,
) -> None:
    """记录 CSV 内重复行：不参与补录，但会在评定原因列列明忽略原因。

    不修改同键真实行的进度（该行可能已在本批次或此前批次完成）。
    """
    with conn() as c:
        c.execute(
            "INSERT INTO batch_ignored_lines(batch_id, line_no, device_no,"
            " cal_date, indication_error, reason, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (batch_id, line_no, device_no, cal_date, indication_error,
             reason, now()),
        )


def list_ignored_lines(batch_id: int) -> list[sqlite3.Row]:
    with conn() as c:
        return list(
            c.execute(
                "SELECT * FROM batch_ignored_lines WHERE batch_id=?"
                " ORDER BY line_no, id",
                (batch_id,),
            ).fetchall()
        )


def list_rows(batch_id: int) -> list[sqlite3.Row]:
    with conn() as c:
        return list(
            c.execute(
                "SELECT * FROM rows WHERE batch_id=? ORDER BY line_no, id",
                (batch_id,),
            ).fetchall()
        )


def get_row(row_id: int) -> sqlite3.Row | None:
    with conn() as c:
        return c.execute("SELECT * FROM rows WHERE id=?", (row_id,)).fetchone()


def update_row(row_id: int, **fields: Any) -> None:
    """更新行进度；若该批次已有有效签发，立即把签发标为过期。

    签发后进度一旦变化，原判定即不再代表当前进度，必须过期；
    原签发内容（payload）保持原样。
    """
    if not fields:
        return
    fields["updated_at"] = now()
    cols = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [row_id]
    with conn() as c:
        c.execute(f"UPDATE rows SET {cols} WHERE id=?", vals)
        _stale_signatures_of_row(
            c, row_id, "批次进度在签发后发生变化"
        )


def increment_attempt(row_id: int) -> None:
    with conn() as c:
        c.execute(
            "UPDATE rows SET attempts=attempts+1, updated_at=? WHERE id=?",
            (now(), row_id),
        )


def _stale_signatures_of_row(
    c: sqlite3.Connection, row_id: int, reason: str
) -> None:
    """在给定事务内，把该行所属批次的有效签发标为过期。"""
    batch_id_row = c.execute(
        "SELECT batch_id FROM rows WHERE id=?", (row_id,)
    ).fetchone()
    if batch_id_row is None:
        return
    c.execute(
        "UPDATE signatures SET status=?, stale_at=?, stale_reason=?"
        " WHERE batch_id=? AND status=?",
        (
            config.SIGNED_STALE, now(), reason,
            int(batch_id_row["batch_id"]), config.SIGNED_VALID,
        ),
    )


def rows_by_status(batch_id: int, statuses: set[str]) -> list[sqlite3.Row]:
    marks = ",".join("?" for _ in statuses)
    with conn() as c:
        return list(
            c.execute(
                f"SELECT * FROM rows WHERE batch_id=? AND status IN ({marks})"
                " ORDER BY line_no, id",
                (batch_id, *statuses),
            ).fetchall()
        )


def batch_counts(batch_id: int) -> dict[str, int]:
    with conn() as c:
        rs = c.execute(
            "SELECT status, COUNT(*) AS n FROM rows WHERE batch_id=?"
            " GROUP BY status",
            (batch_id,),
        ).fetchall()
    counts = {r["status"]: r["n"] for r in rs}
    counts["total"] = sum(counts.values())
    return counts


def in_flight_rows() -> list[sqlite3.Row]:
    """服务重启时：停留在 querying（在途）的行。

    暂停态是人工待办，不会被自动反复重查；
    人工"重新查询"会把行置回 querying，从而获得一次性只查不提复核。
    """
    with conn() as c:
        return list(
            c.execute(
                "SELECT * FROM rows WHERE status='querying'"
                " ORDER BY batch_id, line_no, id"
            ).fetchall()
        )


def running_batches() -> list[sqlite3.Row]:
    with conn() as c:
        return list(
            c.execute(
                "SELECT * FROM batches WHERE status='running' ORDER BY id"
            ).fetchall()
        )


# ----------------------------------------------------------------------
# 规则集
#
# 替换活动规则是一个 IMMEDIATE 写事务：旧规则集的 active 置 0 与
# 新规则集（含全部行）的写入要么都成功，要么都不生效。校验在事务内
# 基于最新数据重做，避免两个并发上传都以为自己是唯一的活动规则。
# ----------------------------------------------------------------------
def _insert_ruleset(
    c: sqlite3.Connection,
    filename: str,
    rules: list[conformity.Rule],
) -> int:
    fingerprint = conformity.rules_fingerprint(None, rules)
    cur = c.execute(
        "INSERT INTO rulesets(filename, fingerprint, rule_count, uploaded_at,"
        " active) VALUES (?, ?, ?, ?, 0)",
        (filename, fingerprint, len(rules), now()),
    )
    ruleset_id = int(cur.lastrowid)
    c.executemany(
        "INSERT INTO rules(ruleset_id, line_no, device_no, tolerance,"
        " uncertainty, tolerance_raw, uncertainty_raw)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (
                ruleset_id,
                r.line_no,
                r.device_no,
                conformity.canonical_decimal(r.tolerance),
                conformity.canonical_decimal(r.uncertainty),
                r.tolerance_raw,
                r.uncertainty_raw,
            )
            for r in rules
        ],
    )
    # 指纹绑定规则集 ID，保存最终值
    fingerprint = conformity.rules_fingerprint(ruleset_id, rules)
    c.execute(
        "UPDATE rulesets SET fingerprint=? WHERE id=?",
        (fingerprint, ruleset_id),
    )
    return ruleset_id


def replace_active_ruleset(
    filename: str, rules: list[conformity.Rule]
) -> int:
    """原子替换活动规则；返回新规则集 ID。

    同一写事务内：停用旧规则集 -> 写入新规则集全部行 -> 启用 ->
    把基于其他规则集的有效签发标为过期。任一步失败都整体回滚，
    旧规则继续有效。
    """
    with conn() as c:
        c.execute("BEGIN IMMEDIATE")
        c.execute("UPDATE rulesets SET active=0 WHERE active=1")
        ruleset_id = _insert_ruleset(c, filename, rules)
        c.execute("UPDATE rulesets SET active=1 WHERE id=?", (ruleset_id,))
        c.execute(
            "UPDATE signatures SET status=?, stale_at=?, stale_reason=?"
            " WHERE status=? AND ruleset_id<>?",
            (
                config.SIGNED_STALE,
                now(),
                "活动允差规则已被替换",
                config.SIGNED_VALID,
                ruleset_id,
            ),
        )
        c.execute("COMMIT")
    return ruleset_id


def list_rulesets() -> list[sqlite3.Row]:
    with conn() as c:
        return list(
            c.execute("SELECT * FROM rulesets ORDER BY id DESC").fetchall()
        )


def active_ruleset(connection: sqlite3.Connection | None = None):
    if connection is not None:
        return connection.execute(
            "SELECT * FROM rulesets WHERE active=1"
        ).fetchone()
    with conn() as c:
        return c.execute("SELECT * FROM rulesets WHERE active=1").fetchone()


def rules_for(ruleset_id: int, connection: sqlite3.Connection | None = None):
    sql = "SELECT * FROM rules WHERE ruleset_id=? ORDER BY line_no, id"
    if connection is not None:
        return list(connection.execute(sql, (ruleset_id,)).fetchall())
    with conn() as c:
        return list(c.execute(sql, (ruleset_id,)).fetchall())


# ----------------------------------------------------------------------
# 快照与评定
# ----------------------------------------------------------------------
def _snapshot(c: sqlite3.Connection, batch_id: int):
    """在调用方给定的读事务中取出一致的（批次、行、活动规则）快照。"""
    batch = c.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
    if batch is None:
        return None, [], None, [], []
    rows = list(
        c.execute(
            "SELECT * FROM rows WHERE batch_id=? ORDER BY line_no, id",
            (batch_id,),
        ).fetchall()
    )
    ruleset = c.execute("SELECT * FROM rulesets WHERE active=1").fetchone()
    rules: list[conformity.Rule] = []
    if ruleset is not None:
        rules = [
            conformity.Rule(
                line_no=r["line_no"],
                device_no=r["device_no"],
                tolerance=conformity.parse_decimal(r["tolerance"]),
                uncertainty=conformity.parse_decimal(r["uncertainty"]),
                tolerance_raw=r["tolerance_raw"],
                uncertainty_raw=r["uncertainty_raw"],
            )
            for r in rules_for(int(ruleset["id"]), connection=c)
        ]
    ignored_lines = list(
        c.execute(
            "SELECT * FROM batch_ignored_lines WHERE batch_id=?"
            " ORDER BY line_no, id",
            (batch_id,),
        ).fetchall()
    )
    return batch, rows, ruleset, rules, ignored_lines


def load_evaluation(batch_id: int) -> dict[str, Any] | None:
    """一致快照下的当前评定（页面展示与签发前校验共用）。"""
    with conn() as c:
        # 显式读事务：同一时刻的 rows 与 rules
        c.execute("BEGIN")
        try:
            batch, rows, ruleset, rules, ignored_lines = _snapshot(c, batch_id)
            if batch is None:
                c.execute("ROLLBACK")
                return None
            result = conformity.build_evaluation(
                batch, rows, ruleset, rules, ignored_lines
            )
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise
    return result


# ----------------------------------------------------------------------
# 签发
# ----------------------------------------------------------------------
def sign_batch(
    batch_id: int,
    expected_progress_fingerprint: str,
    expected_rules_fingerprint: str,
    signed_by: str = "",
) -> int:
    """在写事务内重算快照并校验指纹，全部匹配才落库签发。

    任何不一致都抛 SourceChanged，绝不按旧快照签发。
    """
    with conn() as c:
        c.execute("BEGIN IMMEDIATE")
        batch, rows, ruleset, rules, ignored_lines = _snapshot(c, batch_id)
        if batch is None:
            c.execute("ROLLBACK")
            raise SigningUnavailable("批次不存在")
        if ruleset is None:
            c.execute("ROLLBACK")
            raise SigningUnavailable("当前没有活动规则，无法签发")

        evaluation = conformity.build_evaluation(
            batch, rows, ruleset, rules, ignored_lines
        )
        if not evaluation["eligible"]:
            c.execute("ROLLBACK")
            raise SigningUnavailable(evaluation["eligibility_reason"])

        if evaluation["progress_fingerprint"] != expected_progress_fingerprint:
            c.execute("ROLLBACK")
            raise SourceChanged(
                "批次进度在评定后已变化（例如后台补录或人工处理），"
                "请重新评定后再签发"
            )
        if evaluation["rules_fingerprint"] != expected_rules_fingerprint:
            c.execute("ROLLBACK")
            raise SourceChanged(
                "允差规则在评定后已被替换，请重新评定后再签发"
            )

        payload = dict(evaluation)
        cur = c.execute(
            "INSERT INTO signatures(batch_id, ruleset_id, signed_at, signed_by,"
            " progress_fingerprint, rules_fingerprint, payload, status)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                batch_id,
                int(ruleset["id"]),
                now(),
                signed_by,
                expected_progress_fingerprint,
                expected_rules_fingerprint,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                config.SIGNED_VALID,
            ),
        )
        signature_id = int(cur.lastrowid)
        c.execute("COMMIT")
    return signature_id


def get_signature(signature_id: int) -> sqlite3.Row | None:
    with conn() as c:
        return c.execute(
            "SELECT * FROM signatures WHERE id=?", (signature_id,)
        ).fetchone()


def refresh_signature(signature_id: int) -> sqlite3.Row | None:
    """读取签发并把仍为有效的记录与当前来源比对。

    这样即使后台补录/规则替换后无人访问评定页，查看签发时也会
    如实显示过期状态；原签发内容完全不改写。
    """
    signature = get_signature(signature_id)
    if signature is None:
        return None
    if signature["status"] == config.SIGNED_VALID:
        mark_signature_stale_if_changed(signature)
        signature = get_signature(signature_id)
    return signature


def list_signatures(batch_id: int, limit: int = 20) -> list[sqlite3.Row]:
    with conn() as c:
        return list(
            c.execute(
                "SELECT * FROM signatures WHERE batch_id=?"
                " ORDER BY id DESC LIMIT ?",
                (batch_id, limit),
            ).fetchall()
        )


def signatures_to_recheck() -> list[sqlite3.Row]:
    with conn() as c:
        return list(
            c.execute(
                "SELECT * FROM signatures WHERE status='valid' ORDER BY id"
            ).fetchall()
        )


def mark_signature_stale(
    signature_id: int, reason: str, connection: sqlite3.Connection | None = None
) -> bool:
    """把仍为有效的签发标为过期；已过期的不覆盖原有过期信息。"""
    sql = (
        "UPDATE signatures SET status=?, stale_at=?, stale_reason=?"
        " WHERE id=? AND status=?"
    )
    args = (
        config.SIGNED_STALE, now(), reason, signature_id,
        config.SIGNED_VALID,
    )
    if connection is not None:
        return connection.execute(sql, args).rowcount > 0
    with conn() as c:
        return c.execute(sql, args).rowcount > 0


def mark_signature_stale_if_changed(signature: sqlite3.Row) -> str:
    """重新读取最新进度与规则；与签发时指纹不同则标为过期。

    返回空串表示仍然有效，否则返回过期原因。
    """
    evaluation = load_evaluation(int(signature["batch_id"]))
    if evaluation is None:
        reason = "批次已不存在"
    elif evaluation["rules_fingerprint"] != signature["rules_fingerprint"]:
        reason = "活动允差规则在签发后被替换"
    elif evaluation["progress_fingerprint"] != signature["progress_fingerprint"]:
        reason = "批次进度在签发后发生变化"
    else:
        return ""
    mark_signature_stale(int(signature["id"]), reason)
    return reason


def reconcile_signatures() -> list[int]:
    """把所有仍标为有效的签发与当前来源比对；返回被标过期的批次 ID。"""
    affected: set[int] = set()
    for sig in signatures_to_recheck():
        reason = mark_signature_stale_if_changed(sig)
        if reason:
            affected.add(int(sig["batch_id"]))
    return sorted(affected)

