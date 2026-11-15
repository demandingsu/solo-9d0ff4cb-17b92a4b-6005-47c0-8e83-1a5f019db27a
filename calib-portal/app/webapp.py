"""FastAPI Web 应用：上传 CSV、查看逐行状态、人工续办、规则与符合性签发。"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import config, conformity, database as db
from .automation import AutomationRunner, normalize_base_url
from .csvutil import CsvError, parse_csv
from .worker import Worker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("web")

app = FastAPI(title="校准读数补录助手")
templates = Jinja2Templates(
    directory=str(Path(__file__).parent / "templates")
)
config.SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
app.mount(
    "/screenshots",
    StaticFiles(directory=str(config.SCREENSHOT_DIR)),
    name="screenshots",
)

automation = AutomationRunner()
worker = Worker(automation)


@app.on_event("startup")
def _startup() -> None:
    db.init()
    automation.start()
    worker.start()


@app.on_event("shutdown")
def _shutdown() -> None:
    worker.stop()
    automation.stop()


# ----------------------------------------------------------------------
# 页面
# ----------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    batches = db.list_batches()
    active_ruleset = db.active_ruleset()
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "batches": batches,
            "labels": config.STATUS_LABELS,
            "portal_external": config.PORTAL_EXTERNAL,
            "active_ruleset": active_ruleset,
        },
    )


@app.get("/rules", response_class=HTMLResponse)
def rules_page(request: Request):
    rulesets = db.list_rulesets()
    active = db.active_ruleset()
    rules = db.rules_for(int(active["id"])) if active else []
    return templates.TemplateResponse(
        request,
        "rules.html",
        {"rulesets": rulesets, "active": active, "rules": rules},
    )


@app.get("/batches/{batch_id}", response_class=HTMLResponse)
def batch_detail(request: Request, batch_id: int):
    batch = db.get_batch(batch_id)
    if batch is None:
        return RedirectResponse("/", status_code=303)
    rows = db.list_rows(batch_id)
    counts = db.batch_counts(batch_id)
    return templates.TemplateResponse(
        request,
        "batch.html",
        {
            "batch": batch,
            "rows": rows,
            "counts": counts,
            "labels": config.STATUS_LABELS,
            "paused_states": sorted(config.PAUSED_STATES),
            "done_states": sorted(config.DONE_STATES),
        },
    )


@app.get("/batches/{batch_id}/conformity", response_class=HTMLResponse)
def conformity_page(request: Request, batch_id: int):
    batch = db.get_batch(batch_id)
    if batch is None:
        return RedirectResponse("/", status_code=303)
    # 先把有效签发与当前来源比对（后台可能已补录/替换规则），
    # 再在同一读事务中取评定快照与签发列表，页面状态自洽。
    db.reconcile_signatures()
    evaluation = db.load_evaluation(batch_id)
    signatures = db.list_signatures(batch_id)
    payloads = {
        int(s["id"]): json.loads(s["payload"]) for s in signatures
    }
    return templates.TemplateResponse(
        request,
        "conformity.html",
        {
            "batch": batch,
            "evaluation": evaluation,
            "signatures": signatures,
            "payloads": payloads,
            "signature_labels": config.SIGNED_STATUS_LABELS,
            "result_labels": conformity.RESULT_LABELS,
        },
    )


# ----------------------------------------------------------------------
# API
# ----------------------------------------------------------------------
@app.post("/api/batches")
async def create_batch(
    portal_url: str = Form(...), file: UploadFile = File(...)
):
    portal_url = normalize_base_url(portal_url)
    if not file.filename or not file.filename.lower().endswith(".csv"):
        return JSONResponse(
            {"ok": False, "error": "请上传 .csv 文件"}, status_code=400
        )
    content = await file.read()
    try:
        parsed = parse_csv(content)
    except CsvError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except UnicodeDecodeError:
        return JSONResponse(
            {"ok": False, "error": "CSV 需为 UTF-8 编码"}, status_code=400
        )

    # 先通过浏览器确认门户可打开（不调用其数据接口）
    try:
        ok, err = automation.health_check(portal_url)
    except Exception as exc:  # 浏览器尚未就绪等
        ok, err = False, str(exc)
    if not ok:
        return JSONResponse(
            {"ok": False, "error": f"门户连通性检查失败：{err}"},
            status_code=502,
        )

    saved = config.UPLOAD_DIR / file.filename
    saved.write_bytes(content)

    batch_id = db.create_batch(portal_url, file.filename)
    dup_notes: list[str] = []
    for r in parsed:
        row_id, action = db.upsert_row(
            batch_id, portal_url, r.device_no, r.cal_date,
            r.indication_error, r.line_no,
        )
        if action == "dup_in_csv":
            reason = f"与本文件更早行同键（设备 {r.device_no} / 日期 {r.cal_date}）"
            db.add_ignored_line(
                batch_id, r.line_no, r.device_no, r.cal_date,
                r.indication_error, reason,
            )
            dup_notes.append(f"第 {r.line_no} 行重复已忽略")
        elif action == "reused":
            dup_notes.append(
                f"第 {r.line_no} 行 {r.device_no}/{r.cal_date} "
                "已有持久化进度，续办"
            )

    worker.kick()
    return JSONResponse(
        {
            "ok": True,
            "batch_id": batch_id,
            "rows": len(parsed),
            "notes": dup_notes,
            "redirect": f"/batches/{batch_id}",
        }
    )


@app.get("/api/batches/{batch_id}/conformity")
def api_conformity(batch_id: int):
    db.reconcile_signatures()
    evaluation = db.load_evaluation(batch_id)
    if evaluation is None:
        return JSONResponse({"ok": False, "error": "批次不存在"}, status_code=404)
    signatures = [dict(s) for s in db.list_signatures(batch_id)]
    for s in signatures:
        s["status_label"] = config.SIGNED_STATUS_LABELS.get(
            s["status"], s["status"]
        )
        s["payload"] = json.loads(s["payload"])
    return JSONResponse(
        {"ok": True, "evaluation": evaluation, "signatures": signatures}
    )


@app.post("/api/batches/{batch_id}/sign")
def sign_batch(
    batch_id: int,
    progress_fingerprint: str = Form(...),
    rules_fingerprint: str = Form(...),
    signed_by: str = Form(""),
):
    """签发：只接受与页面展示快照一致的进度和规则。

    服务端在写事务内重新读取进度与活动规则并重算，指纹不一致
    （来源已变）直接拒绝；成功后留存完整判定内容。
    """
    batch = db.get_batch(batch_id)
    if batch is None:
        return JSONResponse({"ok": False, "error": "批次不存在"}, status_code=404)
    try:
        signature_id = db.sign_batch(
            batch_id, progress_fingerprint, rules_fingerprint,
            signed_by.strip(),
        )
    except db.SourceChanged as exc:
        # 来源变化时同时刷新页面可用的最新快照提示
        latest = db.load_evaluation(batch_id)
        return JSONResponse(
            {
                "ok": False,
                "error": exc.message,
                "code": "source_changed",
                "latest": latest,
            },
            status_code=409,
        )
    except db.SigningUnavailable as exc:
        return JSONResponse(
            {"ok": False, "error": exc.message, "code": "not_eligible"},
            status_code=409,
        )
    return JSONResponse(
        {
            "ok": True,
            "signature_id": signature_id,
            "detail_url": f"/signatures/{signature_id}",
        }
    )


@app.get("/api/rulesets")
def api_rulesets():
    return JSONResponse(
        {
            "ok": True,
            "rulesets": [dict(r) for r in db.list_rulesets()],
        }
    )


@app.post("/api/rulesets")
async def upload_ruleset(file: UploadFile = File(...)):
    """上传规则 CSV：整份校验，任一无效即整份拒收，旧规则继续有效。"""
    if not file.filename or not file.filename.lower().endswith(".csv"):
        return JSONResponse(
            {"ok": False, "error": "请上传 .csv 文件"}, status_code=400
        )
    content = await file.read()
    try:
        rules = conformity.parse_rules_csv(content)
    except conformity.RuleCsvError as exc:
        return JSONResponse(
            {
                "ok": False,
                "error": "规则 CSV 整份拒收，原有活动规则保持不变",
                "errors": exc.errors,
            },
            status_code=400,
        )
    ruleset_id = db.replace_active_ruleset(file.filename, rules)
    db.reconcile_signatures()
    return JSONResponse(
        {
            "ok": True,
            "ruleset_id": ruleset_id,
            "rule_count": len(rules),
            "redirect": "/rules",
        }
    )


@app.get("/signatures/{signature_id}", response_class=HTMLResponse)
def signature_page(request: Request, signature_id: int):
    signature = db.refresh_signature(signature_id)
    if signature is None:
        return RedirectResponse("/", status_code=303)
    batch = db.get_batch(int(signature["batch_id"]))
    return templates.TemplateResponse(
        request,
        "signature.html",
        {
            "signature": signature,
            "batch": batch,
            "payload": json.loads(signature["payload"]),
            "signature_labels": config.SIGNED_STATUS_LABELS,
            "result_labels": conformity.RESULT_LABELS,
        },
    )


@app.get("/api/signatures/{signature_id}")
def api_signature(signature_id: int):
    signature = db.refresh_signature(signature_id)
    if signature is None:
        return JSONResponse({"ok": False, "error": "签发记录不存在"}, 404)
    data = dict(signature)
    data["payload"] = json.loads(signature["payload"])
    data["status_label"] = config.SIGNED_STATUS_LABELS.get(
        signature["status"], signature["status"]
    )
    return JSONResponse({"ok": True, "signature": data})


@app.get("/api/batches/{batch_id}/rows")
def api_rows(batch_id: int):
    if db.get_batch(batch_id) is None:
        return JSONResponse({"ok": False}, status_code=404)
    rows = [dict(r) for r in db.list_rows(batch_id)]
    ignored_lines = [dict(r) for r in db.list_ignored_lines(batch_id)]
    counts = db.batch_counts(batch_id)
    batch = dict(db.get_batch(batch_id))
    for r in rows:
        r["status_label"] = config.STATUS_LABELS.get(r["status"], r["status"])
    return JSONResponse(
        {"ok": True, "batch": batch, "rows": rows,
         "ignored_lines": ignored_lines, "counts": counts,
         "labels": config.STATUS_LABELS}
    )


@app.post("/api/batches/{batch_id}/resume")
def resume_batch(batch_id: int):
    """续办：批次重新进入运行态，工作线程只处理 pending 行；
    暂停行保持暂停，需逐行人工处理。"""
    batch = db.get_batch(batch_id)
    if batch is None:
        return JSONResponse({"ok": False, "error": "批次不存在"}, 404)
    db.set_batch_status(batch_id, config.B_RUNNING, note="人工续办")
    worker.kick()
    return JSONResponse({"ok": True})


# ---- 逐行人工操作 -------------------------------------------------------
@app.post("/api/rows/{row_id}/retry-query")
def row_retry_query(row_id: int):
    """暂停行重新查询（只查不提）。系统会对该行走恢复逻辑。"""
    row = db.get_row(row_id)
    if row is None:
        return JSONResponse({"ok": False, "error": "行不存在"}, 404)
    if row["status"] not in config.PAUSED_STATES:
        return JSONResponse(
            {"ok": False, "error": "仅暂停状态的行可重新查询"}, 409
        )
    # 置为 querying 后由下一轮恢复循环"只查不提"处理
    db.update_row(row_id, status=config.QUERYING,
                  detail="人工触发重新查询（只查不提）")
    worker.kick()
    return JSONResponse({"ok": True})


@app.post("/api/rows/{row_id}/mark-done")
def row_mark_done(row_id: int, portal_record_url: str = Form("")):
    """人工核实后手动记完成，必须提供门户记录链接。"""
    row = db.get_row(row_id)
    if row is None:
        return JSONResponse({"ok": False, "error": "行不存在"}, 404)
    if row["status"] not in config.PAUSED_STATES:
        return JSONResponse({"ok": False, "error": "仅暂停行可标记"}, 409)
    url = portal_record_url.strip()
    if not url.startswith(("http://", "https://")):
        return JSONResponse(
            {"ok": False, "error": "请填写可访问的门户记录链接"}, 400
        )
    db.update_row(
        row_id, status=config.DONE_EXISTING,
        portal_record_url=url,
        detail=f"人工核实后记完成：{url}",
    )
    worker.kick()
    return JSONResponse({"ok": True})


@app.post("/api/rows/{row_id}/ignore")
def row_ignore(row_id: int, reason: str = Form("")):
    row = db.get_row(row_id)
    if row is None:
        return JSONResponse({"ok": False, "error": "行不存在"}, 404)
    if row["status"] in config.TERMINAL_STATES:
        return JSONResponse({"ok": False, "error": "该行已完结"}, 409)
    db.update_row(
        row_id, status=config.IGNORED,
        detail=f"人工忽略：{reason or '未填写原因'}",
    )
    worker.kick()
    return JSONResponse({"ok": True})


@app.post("/api/rows/{row_id}/resubmit")
def row_resubmit(row_id: int):
    """显式人工授权：对"查无记录"暂停行重新走完整 查→提→复核 流程。

    系统绝不自动重提；只有人工点击才会回到 pending。
    """
    row = db.get_row(row_id)
    if row is None:
        return JSONResponse({"ok": False, "error": "行不存在"}, 404)
    allowed = {config.PAUSED_NO_RECORD, config.PAUSED_SUBMIT_FAIL,
               config.PAUSED_QUERY_FAIL}
    if row["status"] not in allowed:
        return JSONResponse(
            {"ok": False, "error": "该状态不允许重新提交"}, 409
        )
    db.update_row(
        row_id, status=config.PENDING,
        detail="人工授权重新提交（将先查询、确认无同值后再提交）",
    )
    db.set_batch_status(row["batch_id"], config.B_RUNNING,
                        note="人工授权重提后续办")
    worker.kick()
    return JSONResponse({"ok": True})
