"""运行实例业务规则：状态流转、吞吐字段校验与不可变终态落库。

一次运行只会结束一次：结束动作在锁内校验当前状态与吞吐字段，把终态、结束时间、
读取行数和写入行数一起写进 ``store.terminals``，随后才投影到运行行并同步异常重跑。
重复结束、终态之后的迟到吞吐更新都只读不写。
"""
from __future__ import annotations

from typing import Any

from app.runtime_terminal import (
    DRAFT,
    FAILED,
    RUNNING,
    STATUS_ORDER,
    SUCCESS,
    TERMINATED,
    build_terminal,
    ensure_terminal,
    normalize_count,
    project_entry,
    snapshot,
)
from app.services.rerun import RerunService
from app.store import store

MODULE = "runtime"
REQUIRED_FIELDS = ["记录编号", "作业名称", "开始时间"]

# 值分别是「允许的当前状态」与「推进后的目标状态」。
ACTION_RULES: dict[str, tuple[str, str]] = {
    "启动作业": (DRAFT, RUNNING),
    "标记成功": (RUNNING, SUCCESS),
    "标记失败": (RUNNING, FAILED),
    "终止作业": (RUNNING, TERMINATED),
}
ACTION_ORDER = ["启动作业", "标记成功", "标记失败", "终止作业"]

READ_FIELD = "读取行数"
WRITE_FIELD = "写入行数"


class RuntimeService:
    def __init__(self) -> None:
        self.rerun_service = RerunService()

    # ------------------------------------------------------------------ 读取
    def _ensure_terminal(self, entry: dict[str, Any]) -> dict[str, Any] | None:
        """读取唯一终态记录；历史终态行会在这里补一条只读的兼容记录。"""
        return ensure_terminal(entry)

    def project(self, entry: dict[str, Any]) -> dict[str, Any]:
        """列表、详情与导出共用的运行实例视图。"""
        return project_entry(entry, self._ensure_terminal(entry))

    def get_terminal(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """只返回一个终态、读写行数与结束时间，供异常重跑读取。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"运行记录 {entry_id} 不存在或已归档"
        terminal = self._ensure_terminal(entry)
        if terminal is None:
            return None, f"运行记录 {entry_id} 尚未形成终态，不能用于异常重跑"
        return snapshot(terminal), ""

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        with store.lock:
            rows = [self.project(row) for row in store.rows(MODULE)]
        if keyword:
            rows = [
                row
                for row in rows
                if keyword in str(row.get("记录编号", "")) or keyword in str(row.get("作业名称", ""))
            ]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        with store.lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None
            return self.project(entry)

    # ------------------------------------------------------------------ 写入
    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        with store.lock:
            rows = store.rows(MODULE)
            entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
            entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
            entry["status"] = DRAFT
            entry["执行状态"] = DRAFT
            entry["结束时间"] = None
            entry["读取行数"] = None
            entry["写入行数"] = None
            entry["错误信息"] = ""
            entry["pending"] = True
            entry["abnormal"] = False
            rows.append(entry)
            return self.project(entry), []

    def run_action(
        self, entry_id: int, action: str, values: dict[str, Any] | None = None
    ) -> tuple[dict[str, Any] | None, str, bool]:
        values = values or {}
        if action == "更新吞吐量":
            return self.update_throughput(entry_id, values)
        with store.lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"运行记录 {entry_id} 不存在或已归档", False
            if action not in ACTION_RULES:
                return None, f"动作「{action}」不属于运行实例可执行范围", False

            allowed_from, target = ACTION_RULES[action]
            terminal = self._ensure_terminal(entry)
            if terminal is not None:
                # 终态只读：重复结束、以及结束之后再来的动作都不再生效。
                return (
                    self.project(entry),
                    f"运行记录 {entry_id} 已处于终态「{terminal.get('终态')}」，重复结束不会再次生效",
                    False,
                )

            current = str(entry.get("status") or DRAFT)
            if current != allowed_from:
                return (
                    self.project(entry),
                    f"运行记录当前状态「{current}」，不能执行「{action}」；该动作只能从「{allowed_from}」发起",
                    False,
                )

            if target == RUNNING:
                entry["status"] = RUNNING
                entry["执行状态"] = RUNNING
                entry["结束时间"] = None
                entry["错误信息"] = ""
                entry["pending"] = True
                entry["abnormal"] = False
                return self.project(entry), f"运行记录已{action}", True

            # 结束动作：状态、结束时间、吞吐量必须一起校验、一起落库。
            counts, problem = self._resolve_counts(entry, values)
            if problem:
                return self.project(entry), problem, False
            read_rows, write_rows = counts
            ended_at = values.get("结束时间")
            reason = str(values.get("失败原因") or values.get("错误信息") or "").strip()
            if target == SUCCESS:
                reason = ""
            elif not reason:
                reason = "作业执行失败" if target == FAILED else "作业被终止"

            record = store.append_terminal(
                build_terminal(
                    entry,
                    status=target,
                    read_rows=read_rows,
                    write_rows=write_rows,
                    ended_at=ended_at,
                    reason=reason,
                    action=action,
                )
            )
            self._apply_terminal(entry, record)
            # 业务结果同步落到异常重跑清单；成功终态不会留下失败重跑记录。
            self.rerun_service.sync_terminal(record, entry)
            return self.project(entry), f"运行记录已{action}，终态「{target}」已落库", True

    def update_throughput(
        self, entry_id: int, values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str, bool]:
        """运行中的吞吐量上报；终态之后的迟到更新一律拒绝。"""
        with store.lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"运行记录 {entry_id} 不存在或已归档", False

            terminal = self._ensure_terminal(entry)
            if terminal is not None:
                return (
                    self.project(entry),
                    f"运行记录 {entry_id} 已处于终态「{terminal.get('终态')}」，迟到的吞吐量更新不会写入终态记录",
                    False,
                )
            current = str(entry.get("status") or DRAFT)
            if current != RUNNING:
                return (
                    self.project(entry),
                    f"运行记录当前状态「{current}」，尚未进入运行中，不能记录吞吐量",
                    False,
                )

            provided = False
            for field in (READ_FIELD, WRITE_FIELD):
                if field not in values:
                    continue
                provided = True
                number = normalize_count(values.get(field))
                if number is None:
                    return (
                        self.project(entry),
                        f"{field}「{values.get(field)}」不是有效的非负整数，吞吐量未更新",
                        False,
                    )
                entry[field] = number
            if not provided:
                return self.project(entry), "未提供读取行数或写入行数，吞吐量未更新", False
            return self.project(entry), "运行吞吐量已更新", True

    # ------------------------------------------------------------------ 内部
    @staticmethod
    def _resolve_counts(
        entry: dict[str, Any], values: dict[str, Any]
    ) -> tuple[tuple[int, int] | None, str]:
        """结束动作携带的吞吐优先；缺省时沿用运行中最后一次上报。"""
        resolved: list[int] = []
        for field in (READ_FIELD, WRITE_FIELD):
            raw = values.get(field, entry.get(field))
            number = normalize_count(raw)
            if number is None:
                if raw is None or str(raw).strip() == "":
                    number = 0
                else:
                    return None, f"{field}「{raw}」不是有效的非负整数，终态未落库"
            resolved.append(number)
        return (resolved[0], resolved[1]), ""

    @staticmethod
    def _apply_terminal(entry: dict[str, Any], record: dict[str, Any]) -> None:
        """终态记录是唯一来源，运行行只做投影。"""
        status = str(record.get("终态") or "")
        entry["status"] = status
        entry["执行状态"] = status
        entry["结束时间"] = record.get("结束时间")
        entry["读取行数"] = record.get("读取行数")
        entry["写入行数"] = record.get("写入行数")
        entry["错误信息"] = str(record.get("失败原因") or "")
        entry["pending"] = False
        entry["abnormal"] = status != SUCCESS
        entry["终态"] = status
        entry["终态记录ID"] = int(record.get("id", 0) or 0)


__all__ = ["RuntimeService", "MODULE", "STATUS_ORDER", "ACTION_ORDER"]
