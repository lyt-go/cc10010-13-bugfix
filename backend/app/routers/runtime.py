"""运行实例接口：维护运行记录，覆盖启动作业、标记成功、标记失败、终止作业等动作。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.runtime import ACTION_ORDER, RuntimeService

router = APIRouter(prefix="/api/runtime", tags=["运行实例"])

service = RuntimeService()

LIST_FIELDS = ["记录编号", "作业名称", "开始时间", "结束时间", "执行状态", "读取行数", "写入行数", "错误信息"]
STATUSES = ["草稿", "运行中", "成功", "失败", "已终止"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按记录编号或作业名称检索"),
    status: str | None = Query(default=None, description="草稿、运行中、成功、失败、已终止"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按记录编号与状态过滤运行实例列表；终态与吞吐量都取自同一条终态记录。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出运行实例清单：返回与列表一致的终态和吞吐字段。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "runtime", "total": total, "items": items, "actions": ACTION_ORDER}


@router.get("/{entry_id}/terminal", response_model=dict)
def get_terminal(entry_id: int) -> dict[str, Any]:
    """读取不可变运行终态记录，只提供一个终态、读写行数和结束时间。"""
    snapshot, message = service.get_terminal(entry_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail=message)
    return snapshot


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条运行记录明细；列表与详情的终态、吞吐字段口径一致。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"运行记录 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条运行记录，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.merged_values())
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="运行记录已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """执行启动作业、标记成功、标记失败、终止作业，或上报运行中的吞吐量。

    结束类动作会同时提交读取行数、写入行数、结束时间与失败原因；同一运行只落一次终态。
    """
    values = payload.merged_values()
    action = str(values.get("action") or "").strip()
    entry, message, ok = service.run_action(entry_id, action, values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=ok, message=message, entry=entry)
