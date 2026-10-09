"""LangGraphRuntime：生产单 Agent 闭环的 LangGraph 运行时（任务 014，默认路径）。

统一接口（与 ``LegacyRuntime`` 完全一致）：

- 属性面：``run_id`` / ``status`` / ``final_result`` / ``error`` /
  ``max_steps`` / ``timeout_seconds`` / ``model_name`` /
  ``source_message_id``；
- ``run()``：异步事件生成器，事件类型与 legacy 完全对齐
  （``run_created`` / ``plan_ready`` / ``step_started`` /
  ``step_finished`` / ``replanned`` / ``run_completed`` /
  ``run_failed`` / ``run_cancelled``，恢复路径另发 ``run_resumed``）。

持久化与恢复（§5/§7）：

- ``run_id == thread_id``，checkpoint 由持久化 ``AsyncSqliteSaver``
  保存（生产环境由应用 lifespan 初始化；未初始化时按默认路径惰性
  自愈，仍保证本地可持久化）；``astream`` 使用
  ``durability="sync"``，每个超级步骤的 checkpoint 先于下一步同步落盘，
  任意崩溃点处落盘链尾即真实恢复位置（不再依赖事件循环里的
  异步 checkpoint 任务被及时执行）；
- 新运行：``astream(初始状态, cfg)``；恢复：``astream(None, cfg)``，
  从上次 checkpoint 的下一节点继续，已完成工具步骤绝不重跑，复用
  既有观察时也不再重复发该步骤的 ``step_finished`` 事件；
- 已终态运行的重复恢复是幂等空操作（只回报终态）；
- 非终态运行的并发恢复由 per-run 进程内锁串行化，拿到锁后重读运行
  状态：等待期间已被其他恢复收敛为终态时幂等回报，不重放图；
  跨进程由 Run/Step 与 checkpoint 的残态检测兜底，宁可可控失败也不
  重复执行工具；
- 请求中断/异常/消费者关闭时尽力收敛终态，不留永久 ``running``。
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Dict, Optional

from chatchat.server.agent_run import schema as S  # noqa: F401  (状态字段语义对齐)
from chatchat.server.agent_run.common import (
    converge_run_best_effort,
    step_payload,
)
from chatchat.server.agent_run.runtime.checkpoint import (
    get_agent_checkpoint_store,
    initialize_agent_checkpoint_store,
    latest_agent_checkpoint_id,
)
from chatchat.server.agent_run.runner import (
    DEFAULT_MAX_STEPS,
    DEFAULT_TIMEOUT_SECONDS,
)
from chatchat.server.db.repository import agent_run_repository as repo
from chatchat.settings import Settings

# 同一运行的重复恢复在进程内串行化（跨进程由残态检测兜底）。
# 同一进程在测试/热重载后可能换用新事件循环，锁必须随循环重建。
_RESUME_LOCKS: Dict[
    str, tuple[asyncio.AbstractEventLoop, asyncio.Lock]
] = {}


def _resume_lock(run_id: str) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    entry = _RESUME_LOCKS.get(run_id)
    if entry is None or entry[0] is not loop:
        lock = asyncio.Lock()
        _RESUME_LOCKS[run_id] = (loop, lock)
        return lock
    return entry[1]


async def _latest_checkpoint_id(store: Any, thread_id: str) -> Optional[str]:
    """确定线程真实最新（链尾）checkpoint 的 ``checkpoint_id``。

    ``langgraph-checkpoint-sqlite 3.1.1`` 的 ``aget_tuple``"取最新"按
    ``ORDER BY checkpoint_id DESC``（uuid6 字典序）选择；同一毫秒内写入
    的多个 checkpoint（低时延运行很常见）字典序与时间序不一致，可能
    取到历史 checkpoint，导致恢复从早期节点重放、重复执行已完成工具
    步骤。链尾 = 不出现在任何记录 parent 中的唯一 checkpoint，该判据
    不依赖 id 排序。链异常（多链尾/无链尾）时返回 None，由调用方可控
    失败——绝不猜测恢复点。
    """
    rows = [
        t
        async for t in store.alist({"configurable": {"thread_id": thread_id}})
    ]
    if not rows:
        return None
    ids = {t.checkpoint.get("id") for t in rows if t.checkpoint.get("id")}
    parents: set = set()
    for t in rows:
        pid = (t.parent_config or {}).get("configurable", {}).get("checkpoint_id")
        if pid:
            parents.add(pid)
    tails = [cid for cid in ids if cid not in parents]
    if len(tails) != 1:
        return None
    return tails[0]


class LangGraphRuntime:
    def __init__(
        self,
        session_factory,
        owner_id: str,
        planner: Any,
        tools: Dict[str, Any],
        goal: str,
        *,
        conversation_id: Optional[str] = None,
        source_message_id: Optional[str] = None,
        history: Optional[list] = None,
        memory_messages: Optional[list] = None,
        max_steps: Optional[int] = None,
        timeout_seconds: Optional[float] = None,
        model_name: str = "",
        checkpointer: Any = None,
        existing_run_id: Optional[str] = None,
    ):
        if not owner_id:
            raise ValueError("owner_id 不能为空（必须来自服务端认证上下文）")
        if max_steps is None:
            max_steps = getattr(
                Settings.model_settings, "AGENT_MAX_STEPS", DEFAULT_MAX_STEPS
            )
        if timeout_seconds is None:
            timeout_seconds = getattr(
                Settings.model_settings,
                "AGENT_RUN_TIMEOUT_SECONDS",
                DEFAULT_TIMEOUT_SECONDS,
            )
        self.session_factory = session_factory
        self.owner_id = owner_id
        self.planner = planner
        self.tools = dict(tools or {})
        self.goal = (goal or "").strip()
        self.conversation_id = conversation_id
        self.source_message_id = source_message_id
        self.history = history or []
        self.memory_messages = memory_messages or []
        self.max_steps = max(1, int(max_steps))
        self.timeout_seconds = max(1, float(timeout_seconds))
        self.model_name = model_name
        self.checkpointer = checkpointer
        self.existing_run_id = existing_run_id
        self.run_id: Optional[str] = existing_run_id
        self.status: str = "pending"
        self.final_result: Optional[str] = None
        self.error: Optional[str] = None

    # -- 统一事件流 -------------------------------------------------------

    async def run(self) -> AsyncIterator[dict]:
        if self.existing_run_id:
            inner = self._run_resume()
        else:
            if not self.goal:
                raise ValueError("goal 不能为空")
            inner = self._run_fresh()
        try:
            async for ev in inner:
                yield ev
        finally:
            # 消费者提前关闭（请求中断）时必须显式关闭内层生成器：
            # 异步生成器没有 yield-from 联动，外层 aclose 不会传导到
            # 内层，只有显式 aclose 才能让内层的 GeneratorExit 收敛路径
            # （终态收敛/会话关闭）在 aclose 返回前确定性执行（§7）
            await inner.aclose()

    # -- 新建运行 ---------------------------------------------------------

    async def _run_fresh(self) -> AsyncIterator[dict]:
        session = self.session_factory()
        try:
            run = repo.create_run(
                session,
                self.owner_id,
                self.goal,
                conversation_id=self.conversation_id,
                source_message_id=self.source_message_id,
                max_steps=self.max_steps,
            )
            self.run_id = run.id
            yield {"type": "run_created", "run_id": self.run_id,
                   "status": "pending", "model": self.model_name}

            # 规划前先看一次取消信号（pending 阶段即可被取消）
            if repo.run_status(session, self.run_id, self.owner_id) == "cancelled":
                self.status = "cancelled"
                yield self._terminal_event("cancelled")
                return

            repo.start_run(session, self.run_id, self.owner_id)
            self.status = "running"
            yield {"type": "run_created", "run_id": self.run_id,
                   "status": "running", "model": self.model_name}

            async for ev in self._stream_graph(session, resume=False):
                yield ev
        except asyncio.CancelledError:
            converge_run_best_effort(self.session_factory, self.run_id, self.owner_id)
            raise
        except GeneratorExit:
            converge_run_best_effort(self.session_factory, self.run_id, self.owner_id)
            raise
        except Exception as exc:
            try:
                self._finish_failed_safe(session, f"运行异常：{exc}")
            except Exception:
                converge_run_best_effort(self.session_factory, self.run_id, self.owner_id)
            self.status = "failed"
            yield self._terminal_event("failed")
        finally:
            # 双重兜底：任何路径下都不允许留下 pending/running
            converge_run_best_effort(self.session_factory, self.run_id, self.owner_id)
            try:
                session.close()
            except Exception:
                pass

    # -- 恢复运行 ---------------------------------------------------------

    async def _run_resume(self) -> AsyncIterator[dict]:
        assert self.run_id
        session = self.session_factory()
        try:
            run = repo.get_owned_run(session, self.run_id, self.owner_id)
            if run is None:
                raise ValueError("运行不存在或无权访问")
            if run.status in repo.RUN_TERMINAL_STATUSES:
                # 重复恢复已终态运行：幂等空操作，仅回报终态
                self.status = run.status
                if run.status == "completed":
                    self.final_result = run.final_result or ""
                elif run.status == "failed":
                    self.error = run.error or ""
                yield self._terminal_event(run.status)
                return
            self.status = "running"
            yield {"type": "run_resumed", "run_id": self.run_id,
                   "status": "running"}
            # 并发恢复保护：同一运行的恢复在进程内串行化；拿到锁后重读
            # 运行状态——等待期间已被其他恢复收敛为终态时幂等回报，不重放图
            async with _resume_lock(self.run_id):
                # 必须用全新会话重读：长会话的 ORM 身份映射与读快照停留在
                # 恢复开始时刻，看不到等待期间其他恢复/取消的提交
                probe = self.session_factory()
                try:
                    fresh = repo.get_owned_run(probe, self.run_id, self.owner_id)
                finally:
                    probe.close()
                if fresh is None:
                    self._finish_failed_safe(session, "运行记录不可用")
                    self.status = "failed"
                    yield self._terminal_event("failed")
                    return
                if fresh.status in repo.RUN_TERMINAL_STATUSES:
                    self.status = fresh.status
                    if fresh.status == "completed":
                        self.final_result = fresh.final_result or ""
                    elif fresh.status == "failed":
                        self.error = fresh.error or ""
                    yield self._terminal_event(fresh.status)
                    return
                async for ev in self._stream_graph(session, resume=True):
                    yield ev
        except asyncio.CancelledError:
            converge_run_best_effort(self.session_factory, self.run_id, self.owner_id)
            raise
        except GeneratorExit:
            converge_run_best_effort(self.session_factory, self.run_id, self.owner_id)
            raise
        except Exception as exc:
            try:
                self._finish_failed_safe(session, f"恢复异常：{exc}")
            except Exception:
                converge_run_best_effort(self.session_factory, self.run_id, self.owner_id)
            self.status = "failed"
            yield self._terminal_event("failed")
        finally:
            converge_run_best_effort(self.session_factory, self.run_id, self.owner_id)
            try:
                session.close()
            except Exception:
                pass

    # -- 图执行 -----------------------------------------------------------

    def _initial_state(self) -> Dict[str, Any]:
        import time as _time

        return {
            "run_id": self.run_id,
            "owner_id": self.owner_id,
            "goal": self.goal,
            "max_steps": self.max_steps,
            "deadline_ts": _time.time() + self.timeout_seconds,
            "model_name": self.model_name,
            "plan": {},
            "queue": [],
            "steps_done": 0,
            "results": [],
            "replan_steps": [],
            "status": "running",
            "final_result": "",
            "error": "",
        }

    async def _stream_graph(
        self, session: Any, *, resume: bool
    ) -> AsyncIterator[dict]:
        """执行图并映射 SSE 事件；流结束后统一发一次终态事件。"""
        from chatchat.server.agent_run.runtime.graph import build_agent_graph

        store = self.checkpointer or get_agent_checkpoint_store()
        if store is None:
            # 生产由 lifespan 初始化；此处惰性自愈，仍使用本地持久化文件
            store = await initialize_agent_checkpoint_store()
        cfg = {"configurable": {"thread_id": self.run_id}}
        if resume:
            # 把恢复点钉在链尾 checkpoint 上：优先经独立只读连接直接查
            # checkpoint 文件（不受活动连接读快照影响，也不依赖 uuid6
            # 字典序"取最新"）；活动 store 非全局单例（显式注入的
            # checkpointer）时退回 store 上的链尾计算。durability="sync"
            # 保证每个超级步骤的 checkpoint 先于下一步落盘，链尾恒唯一；
            # 链异常（无 checkpoint/多链尾）绝不当新图重放，可控失败。
            latest_id = await latest_agent_checkpoint_id(self.run_id)
            if latest_id is None:
                latest_id = await _latest_checkpoint_id(store, self.run_id)
            if latest_id is None:
                # 旧版（legacy 运行时）运行没有 checkpoint，或 checkpoint
                # 链异常：绝不当作新图重放，可控失败并给出明确状态
                # （该运行仅可查看或取消，任务 014 §6/§7）
                self._finish_failed_safe(
                    session,
                    "该运行没有可恢复的 LangGraph checkpoint（可能由 legacy "
                    "运行时创建，或 checkpoint 链异常），无法安全恢复；"
                    "仅可查看或取消",
                )
                # 与终态事件保持一致：非流式聚合结果按 runner.status 回报
                self.status = "failed"
                yield self._terminal_event("failed")
                return
            cfg = {
                "configurable": {
                    "thread_id": self.run_id,
                    "checkpoint_id": latest_id,
                }
            }
        graph = build_agent_graph(self, store)
        input_state = None if resume else self._initial_state()
        merged: Dict[str, Any] = {} if resume else dict(input_state)

        # durability="sync"：每个超级步骤的 checkpoint 在下一步开始前同步
        # 落盘。默认的 "async" 模式把 checkpoint 写入排成不阻塞事件循环
        # 的任务——消费者暂停/断开或进程崩溃时未及执行，落盘 checkpoint
        # 链会明显落后于步骤表，恢复将加载早期 checkpoint、重放
        # prepare_plan 并可能重复执行已完成工具步骤。
        async for chunk in graph.astream(
            input_state, cfg, stream_mode="updates", durability="sync"
        ):
            for node_name, update in (chunk or {}).items():
                if isinstance(update, dict):
                    merged.update(update)
                ev = self._node_event(session, node_name, update or {})
                if ev is not None:
                    yield ev

        # 图结束后统一收敛并输出唯一终态事件
        final_status = merged.get("status")
        if final_status not in repo.RUN_STATUSES:
            # 兜底查询用全新会话：长会话的读快照可能看不到节点刚提交的终态
            probe = self.session_factory()
            try:
                final_status = repo.run_status(probe, self.run_id, self.owner_id) or "failed"
            finally:
                probe.close()
        if final_status not in repo.RUN_TERMINAL_STATUSES:
            # 防御：图已结束但未收敛（不应发生）——强制收敛
            self._finish_failed_safe(session, "运行未正常收敛")
            final_status = "failed"
        probe = self.session_factory()
        try:
            run_row = repo.get_owned_run(probe, self.run_id, self.owner_id)
        finally:
            probe.close()
        self.status = final_status
        if final_status == "completed":
            self.final_result = merged.get("final_result") or (
                run_row.final_result if run_row is not None else ""
            ) or ""
        elif final_status == "failed":
            self.error = merged.get("error") or (
                run_row.error if run_row is not None else None
            ) or "未知原因"
        yield self._terminal_event(final_status)

    # -- 节点更新 -> SSE 事件映射 ------------------------------------------

    def _node_event(self, session: Any, node_name: str, update: Dict[str, Any]):
        if update.get("status") in repo.RUN_TERMINAL_STATUSES and node_name != "prepare_plan":
            # 门禁/步骤/判断把运行带向终态：不发中间事件，
            # 终态由流结束后的统一事件表达
            return None
        if node_name == "prepare_plan":
            plan = update.get("plan")
            if isinstance(plan, dict):
                return {"type": "plan_ready", "run_id": self.run_id, "plan": plan}
            return None
        if node_name in ("step_start", "step_run"):
            if node_name == "step_run" and update.get("reused_step"):
                # 恢复时复用了上进程已完成的步骤观察：step_finished 已在
                # 上一段流中发出，恢复流不再重复发（Run/Step 步骤行是
                # 用户可见时间线的唯一事实来源，§5）
                return None
            step = self._latest_step(session)
            if step is None:
                return None
            etype = "step_started" if node_name == "step_start" else "step_finished"
            return {"type": etype, "run_id": self.run_id, "step": step_payload(step)}
        if node_name == "decide":
            replan = update.get("replan_steps")
            if replan:
                return {"type": "replanned", "run_id": self.run_id,
                        "steps": replan}
            return None
        # gate 为内部门禁节点，不产生对外事件
        return None

    def _latest_step(self, session: Any):
        steps = repo.get_owned_steps(session, self.run_id, self.owner_id)
        return steps[-1] if steps else None

    # -- 收敛辅助 ---------------------------------------------------------

    def _terminal_event(self, status: str) -> dict:
        if status == "cancelled":
            return {"type": "run_cancelled", "run_id": self.run_id,
                    "status": "cancelled"}
        if status == "failed":
            return {"type": "run_failed", "run_id": self.run_id,
                    "status": "failed", "error": self.error}
        return {"type": "run_completed", "run_id": self.run_id,
                "status": "completed", "final_result": self.final_result}

    def _finish_failed_safe(self, session: Any, error: str) -> None:
        """在调用方传入的会话上收敛 failed；失败时回滚。"""
        if self.run_id is None:
            return
        msg = S.truncate_text(error, 1000)
        try:
            repo.finalize_run(
                session, self.run_id, self.owner_id, "failed", error=msg
            )
        except Exception:
            try:
                session.rollback()
            except Exception:
                pass
        self.error = msg


__all__ = ["LangGraphRuntime"]
