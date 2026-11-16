"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。

运行实例的终态不跟着可变运行行反复覆盖：一次运行结束只会往 ``terminals`` 追加
一条不可变的运行终态数据记录，之后运行行上看到的终态、结束时间和吞吐量都从这条
记录投影出来。``lock`` 把「校验状态 -> 写终态 -> 同步运行行 -> 落异常重跑」这一组
动作串起来，列表、详情、导出和并发结束请求读到的都是同一份结果。
"""
from __future__ import annotations

import threading
from typing import Any

from app.seed import SEED_ROWS
from app.runtime_terminal import TERMINAL_STATUSES, terminal_from_legacy


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        # 不可变运行终态数据记录：每个运行实例最多一条，落库后不再修改。
        self.terminals: list[dict[str, Any]] = []
        # 运行实例、异常重跑的状态校验与终态落库都需要在同一把可重入锁内完成。
        self.lock = threading.RLock()
        self._backfill_terminals()

    def _backfill_terminals(self) -> None:
        """既有运行历史里已经是终态的行，启动时补一条只读终态记录。"""
        for entry in self.rows("runtime"):
            if str(entry.get("status") or "") not in TERMINAL_STATUSES:
                continue
            if self.find_terminal(int(entry.get("id", 0) or 0)) is not None:
                continue
            self.append_terminal(terminal_from_legacy(entry))

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def find_by_field(self, module: str, field: str, value: Any) -> dict[str, Any] | None:
        """按业务编号查行；编号既可能是字符串也可能是数字。"""
        text = str(value)
        for row in self.rows(module):
            if str(row.get(field, "")) == text:
                return row
        return None

    def find_all_by_field(self, module: str, field: str, value: Any) -> list[dict[str, Any]]:
        """同一业务编号可能有多条历史记录，重算时全部取到再统一收敛。"""
        text = str(value)
        return [row for row in self.rows(module) if str(row.get(field, "")) == text]

    def find_terminal(self, runtime_id: int) -> dict[str, Any] | None:
        """按运行实例 id 找到已经落库的唯一终态记录。"""
        for record in self.terminals:
            if int(record.get("运行记录ID", 0)) == runtime_id:
                return record
        return None

    def find_terminal_by_record(self, record_no: str) -> dict[str, Any] | None:
        """按记录编号找到终态记录；异常重跑通过原始运行编号读取同一份数据。"""
        for record in self.terminals:
            if str(record.get("记录编号", "")) == str(record_no):
                return record
        return None

    def next_terminal_id(self) -> int:
        return max((int(record.get("id", 0)) for record in self.terminals), default=0) + 1

    def append_terminal(self, record: dict[str, Any]) -> dict[str, Any]:
        """追加唯一终态；并发下同一运行已经有时直接返回已有记录，不再追加。"""
        existing = self.find_terminal(int(record.get("运行记录ID", 0)))
        if existing is not None:
            return existing
        record = dict(record)
        record["id"] = self.next_terminal_id()
        self.terminals.append(record)
        return record

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
