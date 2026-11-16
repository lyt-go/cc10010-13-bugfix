"""运行终态的共享定义：状态序列、吞吐字段归一化与不可变终态的只读视图。

运行实例结束一次就只落一条终态记录，运行实例列表、运行详情和异常重跑都从这条
记录投影出同样的终态、读取行数、写入行数和结束时间，避免各处各写一份。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

DRAFT = "草稿"
RUNNING = "运行中"
SUCCESS = "成功"
FAILED = "失败"
TERMINATED = "已终止"

STATUS_ORDER = (DRAFT, RUNNING, SUCCESS, FAILED, TERMINATED)
TERMINAL_STATUSES = (SUCCESS, FAILED, TERMINATED)

# 运行终态数据记录对外只暴露这几个字段，异常重跑读取的也是这一份视图。
SNAPSHOT_FIELDS = ("终态", "读取行数", "写入行数", "结束时间")

# 吞吐字段可能以数字、数字字符串或历史遗留的自由文本出现。
COUNT_FIELDS = ("读取行数", "写入行数")


def normalize_count(value: Any) -> int | None:
    """把吞吐字段归一成非负整数；空值与无法识别的内容返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        if value < 0 or not value.is_integer():
            return None
        return int(value)
    text = str(value).strip().replace(",", "").replace("，", "")
    if not text:
        return None
    try:
        number = int(text)
    except ValueError:
        return None
    return number if number >= 0 else None


def normalize_count_or_zero(value: Any) -> int:
    number = normalize_count(value)
    return number if number is not None else 0


def format_end_time(value: Any = None) -> str:
    """结束时间取结束动作带来的值；没有值时用服务端当前时间补齐。"""
    if value is not None and str(value).strip():
        return str(value).strip()
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def build_terminal(
    entry: Mapping[str, Any],
    *,
    status: str,
    read_rows: Any = None,
    write_rows: Any = None,
    ended_at: Any = None,
    reason: Any = None,
    action: str = "",
) -> dict[str, Any]:
    """用同一次结束动作里的状态与吞吐字段构造一条终态记录。"""
    return {
        "运行记录ID": int(entry.get("id", 0) or 0),
        "记录编号": str(entry.get("记录编号") or ""),
        "作业名称": str(entry.get("作业名称") or ""),
        "终态": status,
        "读取行数": normalize_count_or_zero(read_rows),
        "写入行数": normalize_count_or_zero(write_rows),
        "结束时间": format_end_time(ended_at),
        "失败原因": str(reason or "").strip(),
        "结束动作": action,
    }


def terminal_from_legacy(entry: Mapping[str, Any]) -> dict[str, Any]:
    """把历史里已经是终态的运行行补成终态记录，保持既有运行历史可读。"""
    status = str(entry.get("status") or "")
    reason = entry.get("错误信息") if status != SUCCESS else ""
    return build_terminal(
        entry,
        status=status,
        read_rows=entry.get("读取行数"),
        write_rows=entry.get("写入行数"),
        ended_at=entry.get("结束时间"),
        reason=reason,
        action="历史终态",
    )


def ensure_terminal(entry: Mapping[str, Any]) -> dict[str, Any] | None:
    """读取运行实例的唯一终态记录。

    历史里已经是终态、但还没来得及落终态记录的行，在这里补一条不可变记录；
    运行实例和异常重跑都走这个入口，因此看到的是同一份数据。
    """
    from app.store import store

    entry_id = int(entry.get("id", 0) or 0)
    terminal = store.find_terminal(entry_id)
    if terminal is not None:
        return terminal
    if str(entry.get("status") or "") not in TERMINAL_STATUSES:
        return None
    with store.lock:
        terminal = store.find_terminal(entry_id)
        if terminal is not None:
            return terminal
        return store.append_terminal(terminal_from_legacy(entry))


def snapshot(record: Mapping[str, Any]) -> dict[str, Any]:
    """终态记录的唯一只读视图：一个终态、读写行数、结束时间。"""
    return {field: record.get(field) for field in SNAPSHOT_FIELDS}


def project_entry(entry: Mapping[str, Any], terminal: Mapping[str, Any] | None) -> dict[str, Any]:
    """运行实例列表与详情共用的投影，保证终态与吞吐字段一起修正。"""
    view = dict(entry)
    if terminal is not None:
        status = str(terminal.get("终态") or "")
        view["status"] = status
        view["执行状态"] = status
        view["终态"] = status
        view["结束时间"] = terminal.get("结束时间")
        view["读取行数"] = terminal.get("读取行数")
        view["写入行数"] = terminal.get("写入行数")
        view["错误信息"] = str(terminal.get("失败原因") or "")
        view["终态记录"] = snapshot(terminal)
        view["pending"] = False
        view["abnormal"] = status != SUCCESS
        return view

    status = str(entry.get("status") or DRAFT)
    view["status"] = status
    view["执行状态"] = status
    view["终态"] = ""
    # 没结束的运行不应带结束时间，历史样例里的占位值在这里被纠正。
    view["结束时间"] = None
    view["读取行数"] = normalize_count(entry.get("读取行数"))
    view["写入行数"] = normalize_count(entry.get("写入行数"))
    view["错误信息"] = str(entry.get("错误信息") or "")
    view["终态记录"] = None
    return view
