"""统一资源接口：查询、导出、登记和动作按模块 key 分派到领域服务。"""
from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, EntryPayload, PageResult
from app.services import service_for

router = APIRouter(prefix="/api", tags=["业务资源"])


def require_service(module: str):
    service = service_for(module)
    if service is None:
        raise HTTPException(status_code=404, detail=f"未知业务模块：{module}")
    return service


@router.get("/{module}", response_model=PageResult[dict])
def list_entries(
    module: str,
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    service = require_service(module)
    filters = {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in service.LIST_FIELDS and value.strip()
    }
    items, total = service.list_entries(
        filters=filters, keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{module}/export")
def export_entries(
    module: str,
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> StreamingResponse:
    service = require_service(module)
    filters = {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in service.LIST_FIELDS and value.strip()
    }
    items, _ = service.list_entries(
        filters=filters, keyword=keyword, status=status, page=1, size=1_000_000)
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *service.LIST_FIELDS, "status"])
    for item in items:
        values = [item.get("id"), *(item.get(field, "") for field in service.LIST_FIELDS),
                  item.get("status", "")]
        writer.writerow([
            "'" + str(value) if str(value or "").startswith(("=", "+", "-", "@")) else value
            for value in values
        ])
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{module}.csv"'},
    )


@router.get("/cold_storage/candidates", response_model=PageResult[dict])
def list_cold_storage_candidates(
    asset_id: str | None = Query(default=None),
    activation_id: str | None = Query(default=None),
    policy_number: str | None = Query(default=None),
    policy_version: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    """本轮可提交核验的冷存任务：只包含来源已关联且未提交核验的记录。"""
    service = require_service("cold_storage")
    items, total = service.list_candidates(
        asset_id=asset_id, activation_id=activation_id,
        policy_number=policy_number, policy_version=policy_version,
        page=page, size=size,
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/cold_storage/index", response_model=dict)
def get_cold_storage_index() -> dict[str, Any]:
    """分层执行数据索引：策略生效编号 ↔ 冷存任务 ↔ 真实执行分区。"""
    service = require_service("cold_storage")
    entries = service.execution_index_rows()
    linked = sum(1 for row in entries if row.get("来源状态") == "已关联")
    return {
        "total": len(entries),
        "linked": linked,
        "unknown": len(entries) - linked,
        "entries": entries,
    }


@router.get("/cold_storage/unresolved", response_model=dict)
def list_cold_storage_unresolved(
    asset_id: str | None = Query(default=None),
) -> dict[str, Any]:
    """仍缺策略来源的冷存任务，核验前用来核对哪些迁移没有生效依据。"""
    service = require_service("cold_storage")
    items = service.list_unresolved(asset_id=asset_id)
    return {"total": len(items), "items": items}


@router.get("/cold_storage/activation/{activation_id}", response_model=dict)
def get_cold_storage_activation(activation_id: str) -> dict[str, Any]:
    """按策略生效编号回溯生效依据和引用它的冷存任务。"""
    service = require_service("cold_storage")
    activation, tasks = service.trace_activation(activation_id)
    if activation is None and not tasks:
        raise HTTPException(
            status_code=404, detail=f"策略生效编号 {activation_id} 不存在")
    return {
        "activation": activation,
        "tasks": tasks,
        "missing_source": [
            task for task in tasks if not task.get("来源策略生效编号")
        ],
    }


@router.get("/{module}/{entry_id}", response_model=dict)
def get_entry(module: str, entry_id: int) -> dict[str, Any]:
    service = require_service(module)
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"{service.ENTITY} {entry_id} 不存在或已归档")
    return entry


@router.post("/{module}", response_model=ActionResult)
def create_entry(module: str, payload: EntryPayload) -> ActionResult:
    service = require_service(module)
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message=f"{service.ENTITY}已登记", entry=entry)


@router.post("/{module}/{entry_id}/actions", response_model=ActionResult)
def run_action(module: str, entry_id: int, payload: ActionPayload) -> ActionResult:
    service = require_service(module)
    entry, message = service.run_action(entry_id, payload.action.strip())
    return ActionResult(ok=entry is not None, message=message, entry=entry)
