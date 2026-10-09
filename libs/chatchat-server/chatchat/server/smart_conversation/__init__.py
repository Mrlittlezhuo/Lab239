"""智能对话总图（任务 015）。

统一一次对话请求的路由与分发：route → 直接回答 / 知识库任务 / 单工具任务 /
多步骤 Agent → 统一输出。复用现有意图路由、普通聊天与任务 014 的 Agent
Runtime，不新建第二套实现。
"""
from chatchat.server.smart_conversation.graph import (
    EXEC_DIRECT_ANSWER,
    EXEC_KNOWLEDGE_TASK,
    EXEC_MULTI_STEP_AGENT,
    EXEC_SINGLE_TOOL_TASK,
    EXECUTION_MODE_LABELS,
    KB_TOOL_NAME,
    ONE_STEP_MAX_STEPS,
    SmartConversationState,
    build_smart_conversation_graph,
    get_smart_conversation_graph,
)
from chatchat.server.smart_conversation.service import run_smart_conversation

__all__ = [
    "EXEC_DIRECT_ANSWER",
    "EXEC_KNOWLEDGE_TASK",
    "EXEC_MULTI_STEP_AGENT",
    "EXEC_SINGLE_TOOL_TASK",
    "EXECUTION_MODE_LABELS",
    "KB_TOOL_NAME",
    "ONE_STEP_MAX_STEPS",
    "SmartConversationState",
    "build_smart_conversation_graph",
    "get_smart_conversation_graph",
    "run_smart_conversation",
]