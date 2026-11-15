"""设备允差规则的解析与补录批次符合性评定。

规则 CSV 的每一行给出一台设备的正数允差 L 与非负扩展不确定度 U。
整份文件必须全部有效（设备编号不得重复、L>0、U>=0、数值可解析）；
任何一处无效都整份拒收，调用方据此保留原有活动规则。

评定只使用「已完成」的行（门户已确认同值）：
  |示值误差| + U <= L        合格
  |示值误差| - U >  L        不合格
  其余（含两者之间的不确定区间） 待核
未完成的行、没有对应规则的行也待核；人工忽略的行不参与评定，
但在原因列中列明忽略原因。
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping

from . import config

# 评定结果
R_CONFORMING = "conforming"          # 合格
R_NONCONFORMING = "nonconforming"    # 不合格
R_PENDING = "pending"                # 待核
R_IGNORED = "ignored"                # 已忽略，不参与评定

RESULT_LABELS = {
    R_CONFORMING: "合格",
    R_NONCONFORMING: "不合格",
    R_PENDING: "待核",
    R_IGNORED: "已忽略",
}


class RuleCsvError(ValueError):
    """整份规则 CSV 被拒收；errors 为逐条原因。"""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("；".join(errors))


@dataclass(frozen=True)
class Rule:
    line_no: int
    device_no: str
    tolerance: Decimal       # L，正数
    uncertainty: Decimal     # U，非负
    tolerance_raw: str
    uncertainty_raw: str


# ----------------------------------------------------------------------
# 规则 CSV
# ----------------------------------------------------------------------
RULE_HEADER_ALIASES = {
    "设备编号": "device",
    "设备号": "device",
    "设备编码": "device",
    "device_no": "device",
    "device": "device",
    "设备": "device",
    "允差l": "L",
    "允差": "L",
    "公差": "L",
    "允许误差": "L",
    "最大允许误差": "L",
    "tolerance_l": "L",
    "tolerance": "L",
    "mpe": "L",
    "l": "L",
    "扩展不确定度u": "U",
    "扩展不确定度": "U",
    "不确定度": "U",
    "uncertainty_u": "U",
    "uncertainty": "U",
    "u": "U",
}


_NUM_RE = re.compile(
    r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$"
)


def _canon_header(value: str) -> str:
    value = value.strip()
    mapped = RULE_HEADER_ALIASES.get(value)
    if mapped is None:
        mapped = RULE_HEADER_ALIASES.get(value.lower())
    return mapped or value


def parse_decimal(value: str) -> Decimal:
    """严格解析有限十进制数；不接受空串、NaN、Infinity。"""
    v = value.strip()
    if not v or not _NUM_RE.match(v):
        raise ValueError(f"不是有效数值：{value!r}")
    try:
        d = Decimal(v)
    except InvalidOperation:
        raise ValueError(f"不是有效数值：{value!r}") from None
    if not d.is_finite():
        raise ValueError(f"不是有限数值：{value!r}")
    return d


def canonical_decimal(value: Decimal) -> str:
    """用于指纹比较与展示的规范十进制写法（0.050 -> 0.05）。"""
    d = value.normalize()
    s = format(d, "f")
    if s == "-0":
        s = "0"
    return s


def parse_rules_csv(content: bytes) -> list[Rule]:
    """解析并校验整份规则 CSV；任何错误都抛 RuleCsvError（整份拒收）。"""
    try:
        text = content.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError:
        raise RuleCsvError(["规则 CSV 需为 UTF-8 编码"]) from None

    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        raise RuleCsvError(["规则 CSV 为空"])

    header = [h.strip() for h in rows[0]]
    mapped = [_canon_header(h) for h in header]
    errors: list[str] = []
    idx: dict[str, int] = {}
    for key, label in (
        ("device", "设备编号"), ("L", "允差L"), ("U", "扩展不确定度U")
    ):
        positions = [i for i, m in enumerate(mapped) if m == key]
        if not positions:
            errors.append(
                f"缺少必需列「{label}」，实际表头：{', '.join(header) or '（空）'}"
            )
        elif len(positions) > 1:
            errors.append(f"表头重复列「{label}」")
        else:
            idx[key] = positions[0]
    if errors:
        raise RuleCsvError(errors)

    rules: list[Rule] = []
    seen: dict[str, int] = {}
    for physical, raw in enumerate(rows[1:], start=2):
        if not raw or all(not cell.strip() for cell in raw):
            continue  # 跳过空行
        if len(raw) < len(header):
            raw = raw + [""] * (len(header) - len(raw))

        device_no = raw[idx["device"]].strip()
        l_raw = raw[idx["L"]].strip()
        u_raw = raw[idx["U"]].strip()
        line_errors: list[str] = []
        if not device_no:
            line_errors.append(f"第 {physical} 行设备编号为空")
        if not l_raw:
            line_errors.append(f"第 {physical} 行允差L为空")
        if not u_raw:
            line_errors.append(f"第 {physical} 行扩展不确定度U为空")
        if line_errors:
            errors.extend(line_errors)
            continue

        l_val = u_val = None
        try:
            l_val = parse_decimal(l_raw)
        except ValueError as exc:
            errors.append(f"第 {physical} 行允差L无效：{exc}")
        else:
            if l_val <= 0:
                errors.append(
                    f"第 {physical} 行允差L必须为正数，实际为 {l_raw}"
                )
        try:
            u_val = parse_decimal(u_raw)
        except ValueError as exc:
            errors.append(f"第 {physical} 行扩展不确定度U无效：{exc}")
        else:
            if u_val < 0:
                errors.append(
                    f"第 {physical} 行扩展不确定度U必须为非负数，实际为 {u_raw}"
                )

        if device_no:
            if device_no in seen:
                errors.append(
                    f"第 {physical} 行设备编号 {device_no} 重复"
                    f"（首次出现在第 {seen[device_no]} 行）"
                )
            else:
                seen[device_no] = physical

        if l_val is not None and u_val is not None and not line_errors:
            rules.append(
                Rule(physical, device_no, l_val, u_val, l_raw, u_raw)
            )

    if errors:
        raise RuleCsvError(errors)
    if not rules:
        raise RuleCsvError(["规则 CSV 没有有效数据行"])
    return rules


# ----------------------------------------------------------------------
# 指纹：评定与签发绑定同一份进度 + 规则
# ----------------------------------------------------------------------
def _hash(payload: Any) -> str:
    blob = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _canonical_error(raw: str) -> str:
    try:
        return canonical_decimal(parse_decimal(raw))
    except ValueError:
        return raw.strip()


def progress_fingerprint(
    batch_id: int,
    rows: Iterable[Mapping[str, Any]],
    ignored_lines: Iterable[Mapping[str, Any]] = (),
) -> str:
    """批次进度指纹：任何与评定相关的变化都会改变它。"""
    items = []
    for r in sorted(rows, key=lambda x: int(x["id"])):
        items.append([
            int(r["id"]),
            r["device_no"],
            r["cal_date"],
            _canonical_error(r["indication_error"]),
            r["status"],
            r["portal_record_id"] or "",
            r["portal_record_url"] or "",
            r["detail"] or "",
        ])
    ignored = [
        [
            int(r["line_no"]),
            r["device_no"],
            r["cal_date"],
            _canonical_error(r["indication_error"]),
            r["reason"],
        ]
        for r in sorted(ignored_lines, key=lambda x: (int(x["line_no"]), int(x["id"])))
    ]
    return _hash({"batch_id": int(batch_id), "rows": items,
                  "ignored_lines": ignored})


def rules_fingerprint(
    ruleset_id: int | None, rules: Iterable[Rule | Mapping[str, Any]]
) -> str:
    items = []
    for r in rules:
        if isinstance(r, Mapping):
            device_no = r["device_no"]
            tol = r["tolerance"]
            unc = r["uncertainty"]
        else:
            device_no = r.device_no
            tol = canonical_decimal(r.tolerance)
            unc = canonical_decimal(r.uncertainty)
        items.append([device_no, str(tol), str(unc)])
    items.sort(key=lambda x: x[0])
    return _hash({"ruleset_id": ruleset_id, "rules": items})


# ----------------------------------------------------------------------
# 评定
# ----------------------------------------------------------------------
def _rule_dict(r: Rule) -> dict[str, Any]:
    return {
        "line_no": r.line_no,
        "device_no": r.device_no,
        "tolerance": canonical_decimal(r.tolerance),
        "uncertainty": canonical_decimal(r.uncertainty),
        "tolerance_raw": r.tolerance_raw,
        "uncertainty_raw": r.uncertainty_raw,
    }


def _fmt(d: Decimal) -> str:
    return canonical_decimal(d)


def _judge(row_error: str, rule: Rule) -> tuple[str, str]:
    try:
        e = abs(parse_decimal(row_error))
    except ValueError:
        return (
            R_PENDING,
            f"示值误差 {row_error!r} 无法解析，无法评定",
        )
    plus = e + rule.uncertainty
    minus = e - rule.uncertainty
    l_s = _fmt(rule.tolerance)
    u_s = _fmt(rule.uncertainty)
    e_s = _fmt(e)
    if plus <= rule.tolerance:
        return (
            R_CONFORMING,
            f"|{e_s}| + {u_s} = {_fmt(plus)} ≤ L={l_s}",
        )
    if minus > rule.tolerance:
        return (
            R_NONCONFORMING,
            f"|{e_s}| − {u_s} = {_fmt(minus)} > L={l_s}",
        )
    return (
        R_PENDING,
        f"|{e_s}| + {u_s} = {_fmt(plus)} > L={l_s}，"
        f"且 |{e_s}| − {u_s} = {_fmt(minus)} ≤ L={l_s}，"
        "处于不确定度区间，无法判定",
    )


def build_evaluation(
    batch: Mapping[str, Any],
    rows: Iterable[Mapping[str, Any]],
    ruleset: Mapping[str, Any] | None,
    rules: Iterable[Rule],
    ignored_lines: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """用给定快照计算评定结果（纯函数，不读写数据库）。

    rows 为批次的行进度；ignored_lines 为 CSV 内重复等被忽略的行，
    不参与补录与判定，但会在结果中单列忽略原因。
    """
    row_list = sorted(rows, key=lambda r: (int(r["line_no"]), int(r["id"])))
    ignored_list = sorted(
        ignored_lines, key=lambda r: (int(r["line_no"]), int(r["id"]))
    )
    rule_list = list(rules)
    by_device = {r.device_no: r for r in rule_list}

    results: list[dict[str, Any]] = []
    counts = {
        "total": 0,
        "completed": 0,
        "conforming": 0,
        "nonconforming": 0,
        "pending": 0,
        "ignored": 0,
    }

    for row in row_list:
        status = row["status"]
        row_status_label = config.STATUS_LABELS.get(status, status)
        base = {
            "row_id": int(row["id"]),
            "line_no": int(row["line_no"]),
            "device_no": row["device_no"],
            "cal_date": row["cal_date"],
            "indication_error": row["indication_error"],
            "row_status": status,
            "row_status_label": row_status_label,
            "portal_record_url": row["portal_record_url"] or "",
            "portal_record_id": row["portal_record_id"] or "",
            "tolerance": None,
            "uncertainty": None,
        }
        counts["total"] += 1

        if status == config.IGNORED:
            detail = (row["detail"] or "").strip()
            result = R_IGNORED
            reason = f"行已忽略：{detail}" if detail else "行已忽略（未填写原因）"
            counts["ignored"] += 1
        elif status not in config.DONE_STATES:
            result = R_PENDING
            reason = f"行未完成（当前状态：{row_status_label}）"
            counts["pending"] += 1
        else:
            counts["completed"] += 1
            rule = by_device.get(row["device_no"])
            if rule is None:
                result = R_PENDING
                reason = (
                    f"无对应允差规则（设备编号 {row['device_no']} "
                    "不在当前活动规则中）"
                )
                counts["pending"] += 1
            else:
                result, reason = _judge(row["indication_error"], rule)
                base["tolerance"] = canonical_decimal(rule.tolerance)
                base["uncertainty"] = canonical_decimal(rule.uncertainty)
                if result == R_CONFORMING:
                    counts["conforming"] += 1
                elif result == R_NONCONFORMING:
                    counts["nonconforming"] += 1
                else:
                    counts["pending"] += 1

        base.update(
            result=result,
            result_label=RESULT_LABELS[result],
            reason=reason,
        )
        results.append(base)

    ignored_results: list[dict[str, Any]] = []
    for line in ignored_list:
        counts["total"] += 1
        counts["ignored"] += 1
        ignored_results.append({
            "row_id": None,
            "line_no": int(line["line_no"]),
            "device_no": line["device_no"],
            "cal_date": line["cal_date"],
            "indication_error": line["indication_error"],
            "row_status": config.IGNORED,
            "row_status_label": config.STATUS_LABELS[config.IGNORED],
            "portal_record_url": "",
            "portal_record_id": "",
            "tolerance": None,
            "uncertainty": None,
            "result": R_IGNORED,
            "result_label": RESULT_LABELS[R_IGNORED],
            "reason": f"CSV 重复行已忽略：{line['reason']}",
        })

    eligible = counts["pending"] == 0 and counts["completed"] > 0
    if counts["total"] == 0:
        eligibility_reason = "批次没有行，无法签发"
    elif counts["completed"] == 0:
        eligibility_reason = "没有已完成行，无法签发"
    elif counts["pending"] > 0:
        eligibility_reason = f"仍有 {counts['pending']} 行待核，暂不能签发"
    else:
        eligibility_reason = ""

    ruleset_info = None
    if ruleset is not None:
        ruleset_info = {
            "id": int(ruleset["id"]),
            "filename": ruleset["filename"],
            "uploaded_at": ruleset["uploaded_at"],
            "fingerprint": ruleset["fingerprint"],
            "rule_count": int(ruleset["rule_count"]),
        }

    return {
        "batch": {
            "id": int(batch["id"]),
            "portal_url": batch["portal_url"],
            "filename": batch["filename"],
        },
        "rows": results,
        "ignored_rows": ignored_results,
        "counts": counts,
        "eligible": eligible,
        "eligibility_reason": eligibility_reason,
        "ruleset": ruleset_info,
        "rules": [_rule_dict(r) for r in rule_list],
        "progress_fingerprint": progress_fingerprint(
            int(batch["id"]), row_list, ignored_list
        ),
        "rules_fingerprint": rules_fingerprint(
            None if ruleset is None else int(ruleset["id"]), rule_list
        ),
    }
