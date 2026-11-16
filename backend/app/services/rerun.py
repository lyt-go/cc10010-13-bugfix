"""异常重跑业务规则：从不可变运行终态读取续做范围，并随运行实例重算。

重跑任务不自己保存终态和吞吐量：每次读取都回到运行终态的不可变记录上取
「终态、读写行数、结束时间」，运行记录与执行状态共同决定重跑区间与覆盖策略。
同一原始运行编号最多保留一条重跑任务，成功终态会把失败重跑清理掉。
"""
from __future__ import annotations

from typing import Any

from app.runtime_terminal import FAILED, SUCCESS, TERMINATED, ensure_terminal, snapshot
from app.store import store

MODULE = "rerun"
REQUIRED_FIELDS = ["原始运行编号", "重跑区间开始"]
STATUS_ORDER = ["草稿", "重跑审批中", "执行中", "已完成", "已取消"]
# 值分别是「允许的当前状态」与「推进后的目标状态」。
ACTION_RULES: dict[str, tuple[str, str]] = {
    "提交重跑审批": ("草稿", "重跑审批中"),
    "启动重跑": ("重跑审批中", "执行中"),
    "取消重跑": ("草稿", "已取消"),
}
RERUN_ACTIONS = list(ACTION_RULES)
CLOSED_STATUSES = ("已完成", "已取消")

RUNTIME_MODULE = "runtime"
RUNTIME_RECORD_FIELD = "记录编号"
SOURCE_FIELD = "原始运行编号"


def _number_to_text(value: Any) -> str:
    return "" if value is None else str(value)


def _scope_for(status: str) -> tuple[str, str, str]:
    """运行记录与执行状态共同决定续做范围：覆盖策略、依赖级联与重跑原因。"""
    if status == FAILED:
        return "失败区间全量覆盖", "是", "运行实例标记失败，续做失败区间"
    if status == TERMINATED:
        return "已终止区间增量补跑", "否", "运行实例被终止，续做未完成区间"
    return "不重跑", "否", "运行实例已是成功终态，无需重跑"


class RerunService:
    # ------------------------------------------------------------------ 读取
    def _runtime_row(self, entry: dict[str, Any]) -> dict[str, Any] | None:
        record_no = str(entry.get(SOURCE_FIELD) or "").strip()
        if not record_no:
            return None
        return store.find_by_field(RUNTIME_MODULE, RUNTIME_RECORD_FIELD, record_no)

    def _terminal_for(self, entry: dict[str, Any]) -> dict[str, Any] | None:
        """读取原始运行的唯一终态记录，与运行实例读取的是同一份数据。"""
        runtime = self._runtime_row(entry)
        if runtime is None:
            return None
        return ensure_terminal(runtime)

    def project(self, entry: dict[str, Any]) -> dict[str, Any]:
        view = dict(entry)
        terminal = self._terminal_for(entry)
        runtime = self._runtime_row(entry)
        if terminal is not None and runtime is not None:
            status = str(terminal.get("终态") or "")
            view["原始运行终态"] = status
            view["原始运行结束时间"] = terminal.get("结束时间")
            view["原始运行读取行数"] = terminal.get("读取行数")
            view["原始运行写入行数"] = terminal.get("写入行数")
            view["终态记录"] = snapshot(terminal)
            view["重跑状态"] = view.get("status")
            view["pending"] = str(view.get("重跑状态") or "") not in CLOSED_STATUSES
            view["abnormal"] = status != SUCCESS and view["pending"]
        else:
            view["原始运行终态"] = ""
            view["原始运行结束时间"] = None
            view["原始运行读取行数"] = None
            view["原始运行写入行数"] = None
            view["终态记录"] = None
        return view

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
                if keyword in str(row.get("任务编号", ""))
                or keyword in str(row.get(SOURCE_FIELD, ""))
            ]
        if status:
            rows = [
                row for row in rows if row.get("status") == status or row.get("重跑状态") == status
            ]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        with store.lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None
            return self.project(entry)

    def find_by_source(self, record_no: str) -> dict[str, Any] | None:
        return store.find_by_field(MODULE, SOURCE_FIELD, record_no)

    # ------------------------------------------------------------------ 写入
    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [
            field
            for field in ("原始运行编号",)
            if not str(values.get(field) or "").strip()
        ]
        if missing:
            return None, missing

        source = str(values.get("原始运行编号") or "").strip()
        with store.lock:
            runtime = store.find_by_field(RUNTIME_MODULE, RUNTIME_RECORD_FIELD, source)
            if runtime is None:
                return None, [f"原始运行编号「{source}」对应的运行记录不存在"]

            # 与运行实例读取同一入口：历史终态行在这一步补出唯一终态记录。
            terminal = ensure_terminal(runtime)
            if terminal is None:
                return None, [
                    f"运行记录「{source}」尚未形成终态，不能登记异常重跑；请先结束该次运行"
                ]
            status = str(terminal.get("终态") or "")
            if status == SUCCESS:
                return None, [f"运行记录「{source}」已是成功终态，不需要异常重跑"]

            existing = self.find_by_source(source)
            if existing is not None:
                # 同一运行只保留一条重跑任务，重复登记返回已有记录而不是再显示一条。
                return self.project(existing), []

            entry = self._build_from_terminal(runtime, terminal, values)
            store.rows(MODULE).append(entry)
            return self.project(entry), []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        with store.lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"重跑任务 {entry_id} 不存在或已归档"
            if action not in ACTION_RULES:
                return None, f"动作「{action}」不属于异常重跑可执行范围"

            allowed_from, target = ACTION_RULES[action]
            current = str(entry.get("status") or STATUS_ORDER[0])
            if current in CLOSED_STATUSES:
                return self.project(entry), f"重跑任务已处于「{current}」，不能执行「{action}」"
            if current != allowed_from:
                return (
                    self.project(entry),
                    f"重跑任务当前状态「{current}」，不能执行「{action}」；该动作只能从「{allowed_from}」发起",
                )

            if action == "启动重跑":
                terminal = self._terminal_for(entry)
                if terminal is None:
                    return (
                        self.project(entry),
                        f"原始运行「{entry.get(SOURCE_FIELD)}」没有可读取的终态记录，不能启动重跑",
                    )
                if str(terminal.get("终态") or "") == SUCCESS:
                    return (
                        self.project(entry),
                        f"原始运行「{entry.get(SOURCE_FIELD)}」已是成功终态，不能启动重跑",
                    )

            entry["status"] = target
            entry["重跑状态"] = target
            entry["pending"] = target not in CLOSED_STATUSES
            entry["abnormal"] = target not in CLOSED_STATUSES
            return self.project(entry), f"重跑任务已{action}"

    def sync_terminal(self, terminal: dict[str, Any], runtime: dict[str, Any]) -> None:
        """运行终态落库后重算异常重跑清单；调用方需持有 store.lock。"""
        source = str(terminal.get("记录编号") or "")
        if not source:
            return
        status = str(terminal.get("终态") or "")
        matches = store.find_all_by_field(MODULE, SOURCE_FIELD, source)
        existing = matches[0] if matches else None

        if len(matches) > 1:
            # 历史遗留的重复行收敛成一条，避免同一运行在清单里显示多次。
            for duplicate in matches[1:]:
                store.rows(MODULE).remove(duplicate)

        if status == SUCCESS:
            # 成功终态不允许和失败重跑同时出现：移除关联任务。
            if existing is not None:
                store.rows(MODULE).remove(existing)
            return

        if status not in (FAILED, TERMINATED):
            return

        if existing is None:
            store.rows(MODULE).append(self._build_from_terminal(runtime, terminal, {}))
            return

        # 已有任务随运行终态重算，不新增第二行，也不保留旧的失败原因。
        if str(existing.get("status") or "") in CLOSED_STATUSES:
            existing["status"] = STATUS_ORDER[0]
        existing["重跑状态"] = existing["status"]
        self._apply_scope(existing, runtime, terminal)
        existing["pending"] = True
        existing["abnormal"] = True

    # ------------------------------------------------------------------ 内部
    def _next_task_no(self) -> str:
        """任务编号按 RERU-#### 顺延，避免与既有清单里的编号重复。"""
        numbers = []
        for row in store.rows(MODULE):
            text = str(row.get("任务编号") or "")
            suffix = text.rsplit("-", 1)[-1] if "-" in text else ""
            if suffix.isdigit():
                numbers.append(int(suffix))
        return f"RERU-{max(numbers, default=0) + 1:04d}"

    def _build_from_terminal(
        self,
        runtime: dict[str, Any],
        terminal: dict[str, Any],
        values: dict[str, Any],
    ) -> dict[str, Any]:
        rows = store.rows(MODULE)
        entry: dict[str, Any] = {
            "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
            "任务编号": str(values.get("任务编号") or "").strip() or self._next_task_no(),
            SOURCE_FIELD: str(terminal.get("记录编号") or ""),
            "status": STATUS_ORDER[0],
            "重跑状态": STATUS_ORDER[0],
            "pending": True,
            "abnormal": True,
        }
        self._apply_scope(entry, runtime, terminal)
        for field in ("重跑区间结束", "覆盖策略", "依赖级联", "重跑原因"):
            if values.get(field) not in (None, ""):
                entry[field] = values[field]
        return entry

    @staticmethod
    def _apply_scope(
        entry: dict[str, Any], runtime: dict[str, Any], terminal: dict[str, Any]
    ) -> None:
        status = str(terminal.get("终态") or "")
        strategy, cascade, reason = _scope_for(status)
        entry["重跑区间开始"] = _number_to_text(runtime.get("开始时间")) or _number_to_text(
            terminal.get("结束时间")
        )
        entry["重跑区间结束"] = _number_to_text(terminal.get("结束时间"))
        entry["覆盖策略"] = strategy
        entry["依赖级联"] = cascade
        entry["重跑原因"] = str(terminal.get("失败原因") or "") or reason


__all__ = ["RerunService", "MODULE", "STATUS_ORDER", "RERUN_ACTIONS"]
