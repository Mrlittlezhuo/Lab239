"""Agent 运行 checkpoint 存储与应用生命周期（任务 014 §2/§5）。

生产 Agent 是异步流式路径，checkpoint 必须使用官方异步持久化实现
``AsyncSqliteSaver``（``langgraph-checkpoint-sqlite`` 提供的
``langgraph.checkpoint.sqlite.aio.AsyncSqliteSaver`` + ``aiosqlite``），
不能在事件循环中调用同步 ``SqliteSaver`` 的异步接口。

- 生产：``create_app`` 的 lifespan 在启动时调用
  :func:`initialize_agent_checkpoint_store` 打开
  ``CHATCHAT_ROOT/data/agent_run_checkpoints.sqlite``，关闭时调用
  :func:`shutdown_agent_checkpoint_store` 释放 ``aiosqlite`` 连接
  （``AsyncSqliteSaver`` 自身不提供 ``close``，连接必须手工关闭，
  否则进程退出前连接泄漏）。
- 离线测试：同样调用 :func:`initialize_agent_checkpoint_store`，
  传入 ``tmp_path`` 下的临时文件即可，测试结束调用 shutdown 清理。

``run_id`` 直接作为 LangGraph ``thread_id``，与用户身份
（``owner_id`` 存于 ``agent_run`` 行）保持稳定映射：只有认证上下文中的
所有者能读取/恢复/取消该 thread。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from chatchat.settings import Settings

# 模块级单例：一次进程一个 checkpoint 存储（与 FastAPI 应用生命周期一致）
_checkpointer: Optional[AsyncSqliteSaver] = None
_conn = None  # AsyncSqliteSaver 持有的 aiosqlite.Connection（需手工关闭）
_conn_path: Optional[Path] = None  # 存储文件绝对路径（供独立只读连接查链尾）
_event_loop: Optional[asyncio.AbstractEventLoop] = None


def default_checkpoint_path() -> Path:
    """生产环境 checkpoint 文件：``CHATCHAT_ROOT/data/agent_run_checkpoints.sqlite``。"""
    path = Path(Settings.CHATCHAT_ROOT) / "data" / "agent_run_checkpoints.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def get_agent_checkpoint_store() -> Optional[AsyncSqliteSaver]:
    """返回当前事件循环可用的全局 checkpointer。

    ``AsyncSqliteSaver`` 内部包含绑定事件循环的异步锁。测试、
    热重载或应用重建可能在同一进程中换用新事件循环，此时旧
    实例不能继续复用，由初始化函数关闭并重建。
    """
    if _checkpointer is None:
        return None
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        return _checkpointer
    if _event_loop is not current_loop:
        return None
    return _checkpointer


async def shutdown_agent_checkpoint_store() -> None:
    """关闭 aiosqlite 连接并清空单例（幂等）。"""
    global _checkpointer
    global _conn
    global _conn_path
    global _event_loop
    conn = _conn
    _conn = None
    _checkpointer = None
    _conn_path = None
    _event_loop = None
    if conn is not None:
        try:
            await conn.close()
        except Exception:
            pass


async def latest_agent_checkpoint_id(thread_id: str) -> Optional[str]:
    """确定线程真实最新（链尾）checkpoint 的 ``checkpoint_id``。

    两条独立于活动连接的保证：

    1. **不依赖字典序"取最新"**：``AsyncSqliteSaver.aget_tuple`` 按
       ``ORDER BY checkpoint_id DESC``（uuid6）选择，同一毫秒内写入的多个
       checkpoint（低时延运行很常见）字典序与时间序不一致，可能取到历史
       checkpoint，导致恢复从早期节点重放、重复执行已完成工具步骤。
       链尾 = 不出现在任何记录 ``parent_checkpoint_id`` 中的唯一
       checkpoint，该判据不依赖 id 排序。
    2. **不依赖活动连接的读快照**：用一次性只读 ``aiosqlite`` 连接直接
       查文件（打开即完成 WAL 读取），活连接上未落盘的异步 checkpoint
       future 或陈旧读快照都不会影响结果。

    未初始化存储、线程无任何 checkpoint（legacy 运行时创建的运行）或
    链异常（多链尾/无链尾）时返回 ``None``，由调用方可控失败——绝不
    猜测恢复点。
    """
    if _conn_path is None:
        return None
    import aiosqlite

    uri = f"file:{_conn_path}?mode=ro"
    try:
        async with aiosqlite.connect(uri, uri=True) as conn:
            cur = await conn.execute(
                "SELECT checkpoint_id, parent_checkpoint_id"
                " FROM checkpoints WHERE thread_id = ? AND checkpoint_ns = ''",
                (str(thread_id),),
            )
            rows = await cur.fetchall()
    except Exception:
        return None
    if not rows:
        return None
    ids = {cid for cid, _ in rows}
    parents = {pid for _, pid in rows if pid}
    tails = [cid for cid in ids if cid not in parents]
    if len(tails) != 1:
        return None
    return tails[0]


async def initialize_agent_checkpoint_store(
    path: Optional[Path] = None,
) -> AsyncSqliteSaver:
    """（重新）初始化全局 checkpointer。

    - ``path=None`` 且已有实例：直接复用（幂等）；
    - ``path=None`` 且无实例：使用 :func:`default_checkpoint_path`；
    - 传入 ``path``：先关闭旧实例，再打开指定文件（用于测试隔离）。

    ``AsyncSqliteSaver`` 的构造与 ``setup()`` 都要求在运行中的事件循环内
    执行，因此本函数为异步，且必须由 lifespan / 异步 fixture 调用。
    """
    global _checkpointer
    global _conn
    global _conn_path
    global _event_loop
    current_loop = asyncio.get_running_loop()
    if (
        _checkpointer is not None
        and path is None
        and _event_loop is current_loop
    ):
        return _checkpointer
    if _checkpointer is not None:
        # 显式换库，或同一进程已切换到新事件循环。
        await shutdown_agent_checkpoint_store()
    if path is None:
        path = default_checkpoint_path()
    else:
        Path(path).parent.mkdir(parents=True, exist_ok=True)

    import aiosqlite

    conn = await aiosqlite.connect(str(path))
    try:
        saver = AsyncSqliteSaver(conn)
        await saver.setup()
    except Exception:
        await conn.close()
        raise
    _conn = conn
    _checkpointer = saver
    _conn_path = Path(path).resolve()
    _event_loop = current_loop
    return saver
