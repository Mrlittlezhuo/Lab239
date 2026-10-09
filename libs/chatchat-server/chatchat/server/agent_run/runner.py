"""单 Agent 运行器（任务 011）。

``AgentRunner`` 是独立于现有 Agent 执行器的编排/控制层：

```text
创建运行(pending) -> 启动(running) -> 规划
  -> [取消/步数/超时检查] -> 执行步骤(先建步骤再执行, 立即持久化)
     -> 观察 -> 判断(answer / replan / fail) -> 循环
  -> 收敛终态(completed / failed / cancelled)
```

- 每步执行**前**创建步骤行、执行**后**立即保存结果或错误（逐步持久化）；
- 每一步开始前直读数据库状态：被取消则不再开始新步骤；
- 步数上限/总超时来自配置（有限默认值），达到即收敛为 failed；
- 工具异常只让该步骤 failed，交由判断器决定重规划或失败，不崩溃服务；
- 规划/判断输出无效（``PlanError``）→ 运行可控失败；
- 请求中断/异常时兜底收敛（``ensure_terminal``），不留下永久 running；
- 轨迹只持久化结构化计划、工具行为、截断的观察/错误摘要与状态，
  不含模型隐藏思维链。

``run()`` 为异步生成器，产出事件（供上层转为流式输出/展示）：

- ``run_created``：运行已创建（含 run_id 与初始状态）；
- ``plan_ready``：结构化计划就绪；
- ``step_started`` / ``step_finished``：步骤开始/结束（含工具名、参数、
  截断后的观察/错误摘要）；
- ``replanned``：判断器补充了后续步骤；
- ``run_completed`` / ``run_failed`` / ``run_cancelled``：终态事件。
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from typing import Any, AsyncIterator, Dict, Optional

from chatchat.server.db.models.agent_run_model import AgentRunModel  # noqa: F401  (注册表到 Base)
from chatchat.server.db.models.agent_step_model import AgentStepModel  # noqa: F401
from chatchat.server.agent_run import schema as S
from chatchat.server.agent_run.common import (
    converge_run_best_effort as _converge_common,
    invoke_tool as _invoke_tool_common,
    set_plan_text as _set_plan_text_common,
)
from chatchat.server.agent_run.common import step_payload as _step_payload
from chatchat.server.db.repository import agent_run_repository as repo
from chatchat.settings import Settings

DEFAULT_MAX_STEPS = 8
DEFAULT_TIMEOUT_SECONDS = 600


class AgentRunner:
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
        self.run_id: Optional[str] = None
        self.status: str = "pending"
        self.final_result: Optional[str] = None
        self.error: Optional[str] = None

    # -- 内部工具 -------------------------------------------------------

    def _context(self) -> S.PlanContext:
        return S.PlanContext(
            goal=self.goal,
            tools=self.tools,
            history=list(self.history),
            memory_messages=list(self.memory_messages),
            max_steps=self.max_steps,
        )

    def _set_status(
        self, status: str, *, final_result: str | None = None, error: str | None = None
    ) -> None:
        self.status = status
        if final_result is not None:
            self.final_result = final_result
        if error is not None:
            self.error = error

    async def _tool_invoke(self, tool, args: dict) -> str:
        """调用普通或异步工具，异常向上抛出。

        语义与 ``agent_run.common.invoke_tool`` 完全一致（优先 ``ainvoke``，
        兼容只有同步 ``invoke`` 的旧工具/测试替身；不使用
        ``asyncio.to_thread`` 以免离线环境死锁）。
        """
        return await _invoke_tool_common(tool, args)

    def _terminal_event(self, status: str) -> dict:
        if status == "cancelled":
            return {"type": "run_cancelled", "run_id": self.run_id,
                    "status": "cancelled"}
        if status == "failed":
            return {"type": "run_failed", "run_id": self.run_id,
                    "status": "failed", "error": self.error}
        return {"type": "run_completed", "run_id": self.run_id,
                "status": "completed", "final_result": self.final_result}

    # -- 主流程 ----------------------------------------------------------

    async def run(self) -> AsyncIterator[dict]:
        if not self.goal:
            raise ValueError("goal 不能为空")
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
                self._set_status("cancelled")
                yield self._terminal_event("cancelled")
                return

            repo.start_run(session, self.run_id, self.owner_id)
            self._set_status("running")
            yield {"type": "run_created", "run_id": self.run_id,
                   "status": "running", "model": self.model_name}

            deadline = time.monotonic() + self.timeout_seconds
            ctx = self._context()

            # 1) 规划（输出无效 -> 可控失败）
            try:
                plan = self.planner.plan(ctx)
            except S.PlanError as exc:
                self._finish_failed(session, f"计划生成失败（模型输出无效）：{exc}")
                yield self._terminal_event("failed")
                return
            except Exception as exc:
                self._finish_failed(session, f"计划生成异常：{exc}")
                yield self._terminal_event("failed")
                return

            self._set_plan_text(session, plan)
            yield {"type": "plan_ready", "run_id": self.run_id,
                   "plan": plan.to_dict()}

            # 2) 直接回答路径（无需工具）
            if plan.is_direct_answer:
                step = repo.add_step(
                    session, self.run_id, self.owner_id, 1,
                    stype="answer", goal="直接回答", tool_name=None,
                )
                if step is not None:
                    repo.update_step(
                        session, step.id, self.owner_id, status="completed",
                        output_summary=S.truncate_text(plan.direct_answer, 2000),
                        end_time_now=True,
                    )
                self._finish_completed(session, plan.direct_answer)
                yield self._terminal_event("completed")
                return

            # 3) 执行—观察—判断循环（有界：步数/超时/取消）
            queue = deque(plan.steps)
            seq = 0
            results: list[S.StepResult] = []
            while queue:
                # 每一步开始前的门禁：取消 / 步数上限 / 总超时
                current = repo.run_status(session, self.run_id, self.owner_id)
                if current is None:
                    self._finish_failed(session, "运行记录不可用")
                    yield self._terminal_event("failed")
                    return
                if current == "cancelled":
                    self._set_status("cancelled")
                    yield self._terminal_event("cancelled")
                    return
                if current in ("completed", "failed"):
                    self._set_status(current)
                    yield self._terminal_event(current)
                    return
                if seq + 1 > self.max_steps:
                    msg = (
                        f"达到最大步数（{self.max_steps}），"
                        "停止继续调用工具，任务未完成"
                    )
                    self._finish_failed(session, msg)
                    yield self._terminal_event("failed")
                    return
                if time.monotonic() > deadline:
                    msg = f"运行超时（超过 {int(self.timeout_seconds)} 秒），停止执行"
                    self._finish_failed(session, msg)
                    yield self._terminal_event("failed")
                    return

                ps = queue.popleft()
                seq += 1

                # 先建步骤行（执行前持久化），再执行
                step = repo.add_step(
                    session, self.run_id, self.owner_id, seq,
                    stype="tool_call", goal=ps.goal, tool_name=ps.tool,
                    tool_input=S.truncate_text(
                        json.dumps(ps.args, ensure_ascii=False), 2000
                    ),
                )
                if step is None:
                    # 与取消 API 的竞态：门禁检查后运行已被置为终态
                    self._set_status(
                        repo.run_status(session, self.run_id, self.owner_id)
                        or "cancelled"
                    )
                    yield self._terminal_event(self.status)
                    return
                repo.update_step(session, step.id, self.owner_id,
                                 start_time_now=True)
                yield {"type": "step_started", "run_id": self.run_id,
                       "step": _step_payload(step)}

                # 执行工具（异常只让本步骤失败，不崩溃服务）
                output: str = ""
                err: str = ""
                tool = self.tools.get(ps.tool)
                if tool is None:
                    err = f"工具 '{ps.tool}' 当前不可用"
                else:
                    try:
                        result = await self._tool_invoke(tool, ps.args)
                        output = result if isinstance(result, str) else str(result)
                    except Exception as exc:
                        err = str(exc)
                if err:
                    repo.update_step(
                        session, step.id, self.owner_id, status="failed",
                        error_summary=S.truncate_text(err, 1000), end_time_now=True,
                    )
                else:
                    repo.update_step(
                        session, step.id, self.owner_id, status="completed",
                        output_summary=S.truncate_text(output, 2000),
                        end_time_now=True,
                    )
                step_after = repo.update_step(session, step.id, self.owner_id)
                yield {"type": "step_finished", "run_id": self.run_id,
                       "step": _step_payload(step_after or step)}

                results.append(
                    S.StepResult(seq=seq, goal=ps.goal, tool=ps.tool,
                                 args=ps.args, output=output, error=err)
                )

                # 先完成当前计划批次内已经确定的后续步骤。
                # 否则一个初始计划包含多个步骤时，第一步之后的
                # answer 判断会跳过余下计划，导致“有计划但未执行”。
                if queue:
                    continue

                # 当前计划批次全部完成后再判断（输出无效 -> 可控失败）
                try:
                    decision = self.planner.decide(ctx, results)
                except S.PlanError as exc:
                    self._finish_failed(
                        session, f"执行判断失败（模型输出无效）：{exc}"
                    )
                    yield self._terminal_event("failed")
                    return
                except Exception as exc:
                    self._finish_failed(session, f"执行判断异常：{exc}")
                    yield self._terminal_event("failed")
                    return

                if decision.action == "answer":
                    answer_step = repo.add_step(
                        session, self.run_id, self.owner_id, seq + 1,
                        stype="answer", goal="综合观察形成最终答案",
                        tool_name=None,
                    )
                    if answer_step is not None:
                        repo.update_step(
                            session, answer_step.id, self.owner_id,
                            status="completed",
                            output_summary=S.truncate_text(decision.answer, 2000),
                            end_time_now=True,
                        )
                    self._finish_completed(session, decision.answer)
                    yield self._terminal_event("completed")
                    return
                if decision.action == "fail":
                    reason = decision.reason or "Agent 判定无法完成任务"
                    self._finish_failed(session, reason)
                    yield self._terminal_event("failed")
                    return
                # replan：补充步骤（循环门禁保证有界）
                queue.extend(decision.steps)
                yield {"type": "replanned", "run_id": self.run_id,
                       "steps": [s.to_dict() for s in decision.steps]}

            # 队列耗尽仍未收敛（理论上不会发生，防御性兜底）
            self._finish_failed(session, "计划步骤已全部执行但未得到最终判断")
            yield self._terminal_event("failed")
        except asyncio.CancelledError:
            # 请求中断：收敛终态后继续抛出，交由上层结束流
            self._converge_best_effort()
            raise
        except GeneratorExit:
            # 消费者关闭（客户端断连）：同步兜底收敛，不允许再 yield
            self._converge_best_effort()
            raise
        except Exception as exc:
            # 运行器内部意外错误：可控失败，不留永久 running
            try:
                self._finish_failed(session, f"运行异常：{exc}")
            except Exception:
                self._converge_best_effort()
            self.status = "failed"
            yield {"type": "run_failed", "run_id": self.run_id,
                   "status": "failed", "error": str(exc)}
        finally:
            # 双重兜底：任何路径下都不允许留下 pending/running
            self._converge_best_effort()
            try:
                session.close()
            except Exception:
                pass

    # -- 收敛辅助 --------------------------------------------------------

    def _set_plan_text(self, session, plan: S.Plan) -> None:
        _set_plan_text_common(session, self.run_id, self.owner_id, plan)

    def _finish_completed(self, session, answer: str) -> None:
        self.final_result = answer
        repo.finalize_run(
            session, self.run_id, self.owner_id, "completed",
            final_result=S.truncate_text(answer, 8000),
        )
        self._set_status("completed", final_result=answer)

    def _finish_failed(self, session, error: str) -> None:
        msg = S.truncate_text(error, 1000)
        repo.finalize_run(
            session, self.run_id, self.owner_id, "failed", error=msg
        )
        self._set_status("failed", error=msg)

    def _converge_best_effort(self) -> None:
        """用独立会话兜底收敛；任何异常都吞掉，绝不阻断中断/关闭路径。"""
        if self.run_id is None:
            return
        if _converge_common(self.session_factory, self.run_id, self.owner_id):
            self._set_status("failed", error="运行中断，未正常结束")


__all__ = ["AgentRunner", "DEFAULT_MAX_STEPS", "DEFAULT_TIMEOUT_SECONDS"]
