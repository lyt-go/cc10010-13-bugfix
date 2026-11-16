"""运行终态保护的回归用例：直接用业务服务层验证状态机、吞吐量与异常重跑口径。

运行方式：``cd backend && .venv/bin/python -m unittest discover -s tests -v``
（不依赖 pytest / httpx，克隆下来即可运行）
"""
from __future__ import annotations

import threading
import unittest

from app.runtime_terminal import TERMINAL_STATUSES
from app.seed import SEED_ROWS
from app.services.rerun import RerunService
from app.services.runtime import RuntimeService
from app.store import store


class RuntimeTerminalTest(unittest.TestCase):
    def setUp(self) -> None:
        store._tables = {name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()}
        store.terminals = []
        store._backfill_terminals()
        self.runtime = RuntimeService()
        self.rerun = RerunService()

    # ---------------------------------------------------------------- helpers
    def _runtime(self, record_no: str, name: str = "回归用例") -> int:
        entry, missing = self.runtime.create_entry(
            {"记录编号": record_no, "作业名称": name, "开始时间": "2026-09-20 08:00:00"}
        )
        self.assertEqual(missing, [])
        assert entry is not None
        return int(entry["id"])

    def _run(self, entry_id: int) -> None:
        _, _, ok = self.runtime.run_action(entry_id, "启动作业")
        self.assertTrue(ok)

    # ---------------------------------------------------------------- 状态机
    def test_draft_cannot_finish(self) -> None:
        entry_id = self._runtime("RUNT-T001")
        for action in ("标记成功", "标记失败", "终止作业"):
            entry, message, ok = self.runtime.run_action(entry_id, action)
            self.assertFalse(ok, message)
            self.assertEqual(entry["status"], "草稿")
        self.assertIsNone(store.find_terminal(entry_id))

    def test_status_only_moves_forward(self) -> None:
        entry_id = self._runtime("RUNT-T002")
        self._run(entry_id)
        _, message, ok = self.runtime.run_action(entry_id, "启动作业")
        self.assertFalse(ok, message)
        _, _, ok = self.runtime.run_action(entry_id, "标记成功")
        self.assertTrue(ok)
        # 终态单向：成功之后不能再终止或失败。
        for action in ("标记成功", "标记失败", "终止作业"):
            _, message, ok = self.runtime.run_action(entry_id, action)
            self.assertFalse(ok, message)
        terminal = store.find_terminal(entry_id)
        assert terminal is not None
        self.assertEqual(terminal["终态"], "成功")

    # ---------------------------------------------------------------- 终态与吞吐
    def test_terminal_and_throughput_land_together(self) -> None:
        entry_id = self._runtime("RUNT-T003")
        self._run(entry_id)
        _, message, ok = self.runtime.run_action(
            entry_id, "标记失败", {"读取行数": 120, "写入行数": 45, "失败原因": "下游锁冲突"}
        )
        self.assertTrue(ok, message)
        terminal = store.find_terminal(entry_id)
        assert terminal is not None
        self.assertEqual(terminal["终态"], "失败")
        self.assertEqual(terminal["读取行数"], 120)
        self.assertEqual(terminal["写入行数"], 45)
        self.assertEqual(terminal["失败原因"], "下游锁冲突")

        detail = self.runtime.get_entry(entry_id)
        assert detail is not None
        self.assertEqual(detail["结束时间"], terminal["结束时间"])
        self.assertEqual(detail["读取行数"], 120)
        self.assertEqual(detail["写入行数"], 45)
        self.assertEqual(detail["终态记录"], self.runtime.get_terminal(entry_id)[0])

    def test_invalid_counts_leave_no_terminal(self) -> None:
        entry_id = self._runtime("RUNT-T004")
        self._run(entry_id)
        _, message, ok = self.runtime.run_action(entry_id, "标记成功", {"读取行数": "很多"})
        self.assertFalse(ok, message)
        self.assertIsNone(store.find_terminal(entry_id))
        detail = self.runtime.get_entry(entry_id)
        assert detail is not None
        self.assertEqual(detail["status"], "运行中")

    def test_late_throughput_is_rejected(self) -> None:
        entry_id = self._runtime("RUNT-T005")
        self._run(entry_id)
        self.runtime.run_action(entry_id, "更新吞吐量", {"读取行数": 10, "写入行数": 8})
        _, _, ok = self.runtime.run_action(entry_id, "标记成功")
        self.assertTrue(ok)
        _, message, ok = self.runtime.run_action(entry_id, "更新吞吐量", {"读取行数": 9999})
        self.assertFalse(ok, message)
        snapshot, _ = self.runtime.get_terminal(entry_id)
        assert snapshot is not None
        self.assertEqual(snapshot["读取行数"], 10)
        self.assertEqual(sorted(snapshot), sorted(["终态", "读取行数", "写入行数", "结束时间"]))

    def test_concurrent_finish_writes_once(self) -> None:
        entry_id = self._runtime("RUNT-T006")
        self._run(entry_id)
        actions = ["标记成功", "标记失败", "终止作业", "标记成功", "标记失败", "终止作业"]
        threads = [
            threading.Thread(
                target=self.runtime.run_action,
                args=(entry_id, action, {"读取行数": index, "写入行数": index}),
            )
            for index, action in enumerate(actions)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        terminals = [item for item in store.terminals if item["运行记录ID"] == entry_id]
        self.assertEqual(len(terminals), 1)
        detail = self.runtime.get_entry(entry_id)
        assert detail is not None
        self.assertEqual(detail["status"], terminals[0]["终态"])
        self.assertEqual(detail["读取行数"], terminals[0]["读取行数"])
        self.assertEqual(detail["结束时间"], terminals[0]["结束时间"])

    # ---------------------------------------------------------------- 异常重跑
    def test_failed_terminal_creates_single_rerun(self) -> None:
        entry_id = self._runtime("RUNT-T007")
        self._run(entry_id)
        self.runtime.run_action(entry_id, "标记失败", {"读取行数": 30, "写入行数": 12})
        rows = [row for row in store.rows("rerun") if row["原始运行编号"] == "RUNT-T007"]
        self.assertEqual(len(rows), 1)

        # 重复登记不会显示第二条。
        entry, problems = self.rerun.create_entry({"原始运行编号": "RUNT-T007"})
        assert entry is not None
        self.assertEqual(problems, [])
        rows = [row for row in store.rows("rerun") if row["原始运行编号"] == "RUNT-T007"]
        self.assertEqual(len(rows), 1)

        view = self.rerun.get_entry(int(entry["id"]))
        assert view is not None
        snapshot, _ = self.runtime.get_terminal(entry_id)
        assert snapshot is not None
        self.assertEqual(view["终态记录"], snapshot)
        self.assertEqual(view["原始运行读取行数"], 30)
        self.assertEqual(view["原始运行写入行数"], 12)
        self.assertIn(view["覆盖策略"], ("失败区间全量覆盖",))

    def test_failed_terminal_cannot_be_replaced_by_success(self) -> None:
        entry_id = self._runtime("RUNT-T008")
        self._run(entry_id)
        self.runtime.run_action(entry_id, "标记失败")
        self.assertTrue([r for r in store.rows("rerun") if r["原始运行编号"] == "RUNT-T008"])
        # 终态唯一：失败不能再改成功，失败重跑也不会和成功记录并存。
        _, message, ok = self.runtime.run_action(entry_id, "标记成功")
        self.assertFalse(ok, message)
        terminal = store.find_terminal(entry_id)
        assert terminal is not None
        self.assertEqual(terminal["终态"], "失败")
        rerun_rows = [r for r in store.rows("rerun") if r["原始运行编号"] == "RUNT-T008"]
        self.assertEqual(len(rerun_rows), 1)

    def test_success_recompute_clears_failed_rerun(self) -> None:
        # 直接走终态重算入口：成功终态不会让失败重跑与成功记录同时出现。
        entry_id = self._runtime("RUNT-T011")
        self._run(entry_id)
        self.runtime.run_action(entry_id, "标记失败")
        runtime = store.find("runtime", entry_id)
        assert runtime is not None
        terminal = store.find_terminal(entry_id)
        assert terminal is not None
        success_terminal = dict(terminal, 终态="成功", 失败原因="")
        self.rerun.sync_terminal(success_terminal, runtime)
        self.assertEqual(
            [r for r in store.rows("rerun") if r["原始运行编号"] == "RUNT-T011"], []
        )

    def test_rerun_requires_terminal(self) -> None:
        entry_id = self._runtime("RUNT-T009")
        entry, problems = self.rerun.create_entry({"原始运行编号": "RUNT-T009"})
        self.assertIsNone(entry)
        self.assertTrue(problems)
        self._run(entry_id)
        entry, problems = self.rerun.create_entry({"原始运行编号": "RUNT-T009"})
        self.assertIsNone(entry)
        self.assertTrue(problems)

    def test_success_runtime_rejects_rerun(self) -> None:
        entry_id = self._runtime("RUNT-T010")
        self._run(entry_id)
        self.runtime.run_action(entry_id, "标记成功")
        entry, problems = self.rerun.create_entry({"原始运行编号": "RUNT-T010"})
        self.assertIsNone(entry)
        self.assertIn("成功终态", problems[0])

    def test_legacy_terminal_rows_are_readable(self) -> None:
        # 种子数据里已经是终态、但没有终态记录的历史行。
        for entry in store.rows("runtime"):
            if str(entry.get("status")) not in TERMINAL_STATUSES:
                continue
            snapshot, message = self.runtime.get_terminal(int(entry["id"]))
            self.assertEqual(message, "")
            assert snapshot is not None
            self.assertEqual(snapshot["终态"], entry["status"])
            self.assertIsInstance(snapshot["读取行数"], int)
            self.assertIsInstance(snapshot["写入行数"], int)


if __name__ == "__main__":
    unittest.main()
