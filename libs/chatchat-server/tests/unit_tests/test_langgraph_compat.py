"""任务 013 §9：最小离线 LangGraph 兼容原型与验证。

本文件是纯离线测试夹具/原型，**不参与任何生产请求路径**（不被
chatchat / langchain_chatchat 生产代码导入，也不接入 Chat API、WebUI、
真实 ``AgentRun/AgentStep``）。它仅用于在当前依赖基线上证明后续
LangGraph 迁移（任务 014/015/016）需要的关键机制可用：

- TypedDict 强类型 Graph State；
- 3 个节点（plan / execute / decide）；
- 条件边（decide → {execute | END}）；
- 回到前置节点的有界循环（decide → execute，步数上限 MAX_STEPS）；
- 异步调用（``ainvoke``）与异步事件流（``astream_events``）；
- 内存（``InMemorySaver``）与临时 SQLite（``SqliteSaver``）Checkpointer；
- 使用稳定 ``thread_id`` 的状态恢复（含跨连接重开后恢复）。

语义与文档建议一致，全部使用确定性假数据：

    plan → execute → decide
                      ├─ replan → execute（有界）
                      ├─ answer → END
                      └─ fail   → END

后续 Agent 运行接口方向（任务 014 预留，仅设计说明，不在本任务实现）::

    class AgentRuntime:
        async def stream(...): ...   # 异步事件流
        async def resume(...): ...   # 按 thread_id 恢复
        async def cancel(...): ...   # 取消运行中任务
        async def get_state(...): ...# 读取当前状态快照
"""

import asyncio  # noqa: F401  (保留给同步上下文中的 asyncio.run 备用)
from typing import Protocol, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, StateGraph

# 有界循环的步数上限
MAX_STEPS = 5

# 确定性假工具输出：步骤名 → 证据文本；"boom" 为假故障
FAKE_TOOL_OUTPUTS = {
    "search": "evidence: 找到 3 篇相关论文摘要",
    "read": "evidence: 已读取论文核心段落",
    "compare": "evidence: 已完成两篇论文方法对比",
    "boom": "ERROR: 假工具故障",
}


class ResearchState(TypedDict, total=False):
    """原型 Graph State（TypedDict 强类型）。"""

    queue: list  # 待执行步骤（假规划产物）
    steps: int  # 已执行步数（有界循环计数）
    trace: list  # 节点轨迹：plan / execute / decide
    evidence: list  # 假工具输出（证据）
    last_step: str  # 最近执行的步骤名
    answer: str  # 最终答案
    status: str  # pending | replan | answered | failed


class _AgentRuntimeShape(Protocol):
    """任务 014 预留接口方向（设计说明，非生产实现）。

    仅在此声明接口形状，供后续 LangGraph AgentRuntime 设计对齐；
    本任务不接入任何生产路径。
    """

    async def stream(self, *args, **kwargs): ...

    async def resume(self, *args, **kwargs): ...

    async def cancel(self, *args, **kwargs): ...

    async def get_state(self, *args, **kwargs): ...


def plan_node(state: ResearchState) -> dict:
    """假规划节点：登记轨迹，真实规划由输入 queue 提供。"""
    return {"trace": [*state.get("trace", []), "plan"]}


def execute_node(state: ResearchState) -> dict:
    """假执行节点：弹出队首步骤并调用确定性假工具。"""
    queue = list(state.get("queue", []))
    steps = state.get("steps", 0)
    if not queue or steps >= MAX_STEPS:
        return {}
    step = queue.pop(0)
    return {
        "queue": queue,
        "steps": steps + 1,
        "last_step": step,
        "evidence": [*state.get("evidence", []), FAKE_TOOL_OUTPUTS.get(step, f"evidence: {step}")],
        "trace": [*state.get("trace", []), "execute"],
    }


def decide_node(state: ResearchState) -> dict:
    """假决策节点：产出条件边所需 status。"""
    steps = state.get("steps", 0)
    last = state.get("last_step", "")
    if last in ("boom",) and FAKE_TOOL_OUTPUTS.get(last, "").startswith("ERROR"):
        status = "failed"
    elif not state.get("queue"):
        status = "answered"
        answer = "综合结论: " + " | ".join(state.get("evidence", []))
    elif steps >= MAX_STEPS:
        # 有界循环达到上限，强制收敛
        status = "answered"
        answer = "达到最大步数上限，基于已有证据收敛: " + " | ".join(state.get("evidence", []))
    else:
        status = "replan"
    result = {"status": status, "trace": [*state.get("trace", []), "decide"]}
    if status == "answered":
        result["answer"] = answer
    return result


def decide_router(state: ResearchState) -> str:
    """条件边路由：replan 回到 execute（有界），answer/fail 到 END。"""
    if state.get("status") == "replan":
        return "execute"
    return END


def build_graph(checkpointer=None):
    """构建原型图：plan → execute → decide（条件边回边）。"""
    g = StateGraph(ResearchState)
    g.add_node("plan", plan_node)
    g.add_node("execute", execute_node)
    g.add_node("decide", decide_node)
    g.set_entry_point("plan")
    g.add_edge("plan", "execute")
    g.add_edge("execute", "decide")
    g.add_conditional_edges("decide", decide_router, {"execute": "execute", END: END})
    return g.compile(checkpointer=checkpointer)


def _input(queue):
    return {"queue": queue, "steps": 0, "trace": [], "evidence": [], "status": "pending"}


async def test_plan_execute_decide_answer_path():
    """answer 路径：plan → execute → decide(replan) → execute → decide(answered)。"""
    graph = build_graph()
    result = await graph.ainvoke(_input(["search", "read"]))
    assert result["status"] == "answered"
    assert result["steps"] == 2
    assert result["trace"] == ["plan", "execute", "decide", "execute", "decide"]
    assert len(result["evidence"]) == 2
    assert result["answer"].startswith("综合结论: ")
    assert result["queue"] == []


async def test_bounded_loop_terminates_at_max_steps():
    """有界循环：queue 长于上限时，到达 MAX_STEPS 后强制收敛，不会死循环。"""
    graph = build_graph()
    result = await graph.ainvoke(_input(["search"] * 10))
    assert result["status"] == "answered"
    assert result["steps"] == MAX_STEPS
    assert result["trace"].count("execute") == MAX_STEPS
    assert "达到最大步数上限" in result["answer"]


async def test_fail_path_reaches_end():
    """fail 路径：假工具故障 → decide(failed) → END。"""
    graph = build_graph()
    result = await graph.ainvoke(_input(["boom"]))
    assert result["status"] == "failed"
    assert result["trace"] == ["plan", "execute", "decide"]
    assert "answer" not in result


async def test_async_event_stream_covers_all_nodes():
    """异步事件流（astream_events）能看到三个节点的事件。"""
    graph = build_graph()
    seen_nodes = set()
    async for event in graph.astream_events(_input(["search"]), version="v2"):
        if event["event"] == "on_chain_start" and event["name"] in ("plan", "execute", "decide"):
            seen_nodes.add(event["name"])
    assert seen_nodes == {"plan", "execute", "decide"}


async def test_state_restoration_memory_saver_by_thread_id():
    """内存 Checkpointer：新 graph 实例 + 同一 saver + 稳定 thread_id 恢复状态。"""
    saver = InMemorySaver()
    graph = build_graph(checkpointer=saver)
    config = {"configurable": {"thread_id": "t-mem-013"}}
    result = await graph.ainvoke(_input(["search", "compare"]), config)
    assert result["status"] == "answered"

    # 重新构建 graph（同一 saver），仅凭 thread_id 恢复
    restored = build_graph(checkpointer=saver).get_state(config)
    assert restored.values["status"] == "answered"
    assert restored.values["steps"] == 2
    assert restored.values["trace"] == ["plan", "execute", "decide", "execute", "decide"]
    history = list(build_graph(checkpointer=saver).get_state_history(config))
    assert len(history) >= 3  # plan/execute/decide 多步超步快照


def test_state_restoration_sqlite_across_connections(tmp_path):
    """临时 SQLite Checkpointer：关闭连接、重开连接后仍可按 thread_id 恢复。

    说明：``langgraph-checkpoint-sqlite==3.1.1`` 中 ``SqliteSaver`` 仅支持
    同步图接口（异步方法抛 ``NotImplementedError``，需 ``AsyncSqliteSaver``
    + ``aiosqlite``，后者未列入本任务依赖基线）。因此 SQLite 恢复路径用
    同步 ``invoke``/``get_state`` 验证；异步调用/事件流能力已由
    ``InMemorySaver`` 的 ``ainvoke``/``astream_events`` 用例覆盖。
    """
    db = tmp_path / "langgraph_compat.sqlite"
    with SqliteSaver.from_conn_string(str(db)) as saver:
        graph = build_graph(checkpointer=saver)
        config = {"configurable": {"thread_id": "t-sqlite-013"}}
        result = graph.invoke(_input(["search", "read"]), config)
        assert result["status"] == "answered"

    # 第一个连接已关闭；重开新连接，仅凭 thread_id 恢复
    with SqliteSaver.from_conn_string(str(db)) as saver2:
        restored = build_graph(checkpointer=saver2).get_state(config)
        assert restored.values["status"] == "answered"
        assert restored.values["steps"] == 2
        assert restored.values["answer"].startswith("综合结论: ")


def test_prototype_is_offline_and_decoupled_from_production():
    """守卫：原型不得被生产代码导入，也不得引入真实模型/网络依赖。"""
    import os
    import re

    base = os.path.dirname(os.path.abspath(__file__))
    production_roots = [
        os.path.abspath(os.path.join(base, "..", "..", "chatchat")),
        os.path.abspath(os.path.join(base, "..", "..", "langchain_chatchat")),
    ]
    for root in production_roots:
        for dirpath, _, filenames in os.walk(root):
            for fn in filenames:
                if not fn.endswith(".py"):
                    continue
                with open(os.path.join(dirpath, fn), encoding="utf-8") as f:
                    text = f.read()
                assert "test_langgraph_compat" not in text
                assert "ResearchState" not in text
    # 原型自身不引入任何真实 LLM/工具工厂（只扫描 import 行，避免误匹配
    # 本守卫自身的正则文本）
    own_lines = open(__file__, encoding="utf-8").read().splitlines()
    import_lines = [l for l in own_lines if l.strip().startswith(("import ", "from "))]
    joined = "\n".join(import_lines)
    assert not re.search(r"langchain_openai|ChatOpenAI|AgentRunner|tools_factory", joined)
