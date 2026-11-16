"""异常重跑接口：维护重跑任务，覆盖提交重跑审批、启动重跑、取消重跑等动作。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.rerun import RERUN_ACTIONS, RerunService

router = APIRouter(prefix="/api/rerun", tags=["异常重跑"])

service = RerunService()

LIST_FIELDS = ["任务编号", "原始运行编号", "重跑区间开始", "重跑区间结束", "重跑原因", "覆盖策略", "依赖级联", "重跑状态"]
STATUSES = ["草稿", "重跑审批中", "执行中", "已完成", "已取消"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按任务编号或原始运行编号检索"),
    status: str | None = Query(default=None, description="草稿、重跑审批中、执行中、已完成、已取消"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按任务编号与状态过滤异常重跑列表；续做范围来自原始运行终态记录。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出异常重跑清单：与列表读取同一份运行终态。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "rerun", "total": total, "items": items, "actions": RERUN_ACTIONS}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条重跑任务明细；同时带回原始运行的唯一终态。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"重跑任务 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """按原始运行编号登记一条重跑任务；运行没有终态或已是成功终态时说明原因。"""
    entry, problems = service.create_entry(payload.merged_values())
    if entry is None:
        return ActionResult(ok=False, message=f"未登记重跑任务：{'；'.join(problems)}")
    # 同一原始运行只保留一条任务，重复登记返回已有的那一条。
    return ActionResult(ok=True, message="重跑任务已就绪", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """执行提交重跑审批、启动重跑、取消重跑；不允许的动作会被拦下并说明原因。"""
    action = str(payload.merged_values().get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
