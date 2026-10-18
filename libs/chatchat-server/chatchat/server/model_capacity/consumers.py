"""生成模型消费者登记表（里程碑0B · 重新设计方案 §8.2、§16.1B 第 1/5 条）。

「消费者类型」回答的是"这次实际模型调用是谁发起的"，「业务通道」回答的是
"它该排在谁前面"。通道由登记表推导，业务代码只声明消费者类型就能得到正确
的排队位置，避免每个调用点各自决定优先级（那样一定会写歪）。

三类通道（同一通道内先进先出，优先级数字越小越靠前）：

1. ``interactive`` 前台交互：智能聊天、知识库问答、项目研究讨论，以及为它们
   服务的意图路由与 Agent 规划/工具调用；
2. ``user_task`` 用户主动短任务：提取研究定义、生成检索方案、导师手动预审、
   学生手动自检；
3. ``background`` 后台批任务：论文深度提取、提交后自动预审、批量比较、
   LaTeX 进度草稿。

高优先级可以排在尚未开始的低优先级之前，但**不能打断已经开始的生成**：闸门
只在"尚未取得容量"时比较优先级，已取得容量的调用一定跑完自己那一次调用。

新增生成模型调用点必须在这里登记一个消费者类型，否则运行时会落到
``CONSUMER_UNDECLARED`` 并写告警日志，``audit`` 模块的扫描也会把未登记的构造
点暴露给启动检查与自动化测试（§16.1B 第 5 条）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

# ---------------------------------------------------------------------------
# 业务通道
# ---------------------------------------------------------------------------

CHANNEL_INTERACTIVE = "interactive"
CHANNEL_USER_TASK = "user_task"
CHANNEL_BACKGROUND = "background"

#: 通道优先级：数字越小越先取得容量。只在调用尚未开始时比较（§8.2）。
CHANNEL_PRIORITY: Dict[str, int] = {
    CHANNEL_INTERACTIVE: 0,
    CHANNEL_USER_TASK: 1,
    CHANNEL_BACKGROUND: 2,
}

CHANNEL_LABELS: Dict[str, str] = {
    CHANNEL_INTERACTIVE: "前台交互",
    CHANNEL_USER_TASK: "用户主动短任务",
    CHANNEL_BACKGROUND: "后台批任务",
}

#: 未登记消费者的回落通道。刻意选**前台交互**而不是后台：现有聊天/知识库回答
#: 的构造点（chat/chat.py、kb_chat.py、file_chat.py）还没声明消费者，落到后台
#: 通道会让用户聊天排在尚未开始的后台批任务后面，直接违反 §16.1B 第 2/3 条。
#: 集成阶段把后台消费者的 ``consumer=`` 补上后，它们才会真正降到低优先级；
#: 未登记本身由告警日志 + status() 里的 ``undeclared_generation_call`` 暴露。
DEFAULT_UNDECLARED_CHANNEL = CHANNEL_INTERACTIVE


def normalize_channel(channel: Optional[str]) -> Optional[str]:
    """把通道名规范化成登记表里的取值；无法识别时返回 ``None``。"""
    name = (channel or "").strip().lower()
    return name if name in CHANNEL_PRIORITY else None


def channel_priority(channel: Optional[str]) -> int:
    """未知通道按最低优先级处理，保证排序不会因为脏数据崩掉。"""
    return CHANNEL_PRIORITY.get(normalize_channel(channel) or "", 99)


# ---------------------------------------------------------------------------
# 消费者类型常量
# ---------------------------------------------------------------------------

# —— 前台交互 ——
CONSUMER_SMART_CHAT = "smart_chat"  # 智能聊天/普通聊天的回答生成
CONSUMER_KB_CHAT = "kb_chat"  # 知识库问答（含文件问答）的回答生成
CONSUMER_PROJECT_DISCUSSION = "project_discussion"  # 项目研究讨论
CONSUMER_INTENT_ROUTE = "intent_route"  # 自动模式 LLM 意图路由
CONSUMER_AGENT_PLAN = "agent_plan"  # Agent Run 的规划/重规划调用
CONSUMER_AGENT_FINAL_ANSWER = "agent_final_answer"  # Agent Run 的最终回答
CONSUMER_AGENT_TOOL = "agent_tool"  # Agent 工具内部的模型调用（text2sql 等）

# —— 用户主动短任务 ——
CONSUMER_RESEARCH_DEFINITION = "research_definition"  # 从聊天提取结构化研究定义
CONSUMER_RESEARCH_PLAN = "research_plan"  # 生成论文检索方案
CONSUMER_MENTOR_REVIEW = "mentor_review"  # 导师手动 AI 预审
CONSUMER_STUDENT_CHECK = "student_check"  # 学生手动自检

# —— 后台批任务 ——
CONSUMER_AUTO_REVIEW = "auto_review"  # 正式提交后的自动预审
CONSUMER_PAPER_EXTRACT = "paper_extract"  # 论文深度内容提取
CONSUMER_PAPER_COMPARE = "paper_compare"  # 批量论文比较/关联分析
CONSUMER_PROGRESS_DRAFT = "progress_draft"  # LaTeX 进度草稿生成
CONSUMER_KB_SUMMARY = "kb_summary"  # 知识库文档总结（现有 kb_summary_api）
CONSUMER_SESSION_SUMMARY = "session_summary"  # 未来启用的 LLM 会话摘要
CONSUMER_RESEARCH_REPORT = "research_report"  # 论文研究报告生成（批量产出）

# —— 兼容接口 ——
CONSUMER_LEGACY_COMPLETION = "legacy_completion"  # /other/completion 补全式调用

# —— 回落 ——
CONSUMER_UNDECLARED = "undeclared_generation_call"  # 未登记调用（应被修掉）


@dataclass(frozen=True)
class ConsumerSpec:
    """一个生成模型消费者的登记信息。"""

    consumer_type: str
    label: str
    channel: str
    #: 该消费者实际对应的调用点/落地说明，便于集成时核对与排查
    notes: str = ""


def _spec(consumer_type: str, label: str, channel: str, notes: str = "") -> ConsumerSpec:
    return ConsumerSpec(
        consumer_type=consumer_type,
        label=label,
        channel=channel,
        notes=notes,
    )


#: 登记表。键为消费者类型，值含中文说明与所属通道。
CONSUMER_REGISTRY: Dict[str, ConsumerSpec] = {
    s.consumer_type: s
    for s in [
        _spec(
            CONSUMER_SMART_CHAT,
            "智能聊天回答",
            CHANNEL_INTERACTIVE,
            "chat/chat.py、smart_conversation 直接回答分支的回答生成",
        ),
        _spec(
            CONSUMER_KB_CHAT,
            "知识库/文件问答回答",
            CHANNEL_INTERACTIVE,
            "chat/kb_chat.py、chat/file_chat.py 的回答生成",
        ),
        _spec(
            CONSUMER_PROJECT_DISCUSSION,
            "项目研究讨论",
            CHANNEL_INTERACTIVE,
            "里程碑2 之后的项目内讨论（尚未接入，先登记避免遗漏）",
        ),
        _spec(
            CONSUMER_INTENT_ROUTE,
            "自动模式意图路由",
            CHANNEL_INTERACTIVE,
            "intent_router：路由结束必须先释放，回答再重新排队（§8.2）",
        ),
        _spec(
            CONSUMER_AGENT_PLAN,
            "Agent 规划调用",
            CHANNEL_INTERACTIVE,
            "agent_run/runtime：每一步规划都是一次独立调用",
        ),
        _spec(
            CONSUMER_AGENT_FINAL_ANSWER,
            "Agent 最终回答",
            CHANNEL_INTERACTIVE,
            "agent_run/runtime 收束阶段的回答生成",
        ),
        _spec(
            CONSUMER_AGENT_TOOL,
            "Agent 工具内模型调用",
            CHANNEL_INTERACTIVE,
            "工具（text2sql/text2promql 等）内部再次调用模型：独立排队，"
            "不得复用外层租约",
        ),
        _spec(
            CONSUMER_RESEARCH_DEFINITION,
            "提取研究定义",
            CHANNEL_USER_TASK,
            "从聊天内容提取结构化研究定义",
        ),
        _spec(
            CONSUMER_RESEARCH_PLAN,
            "生成检索方案",
            CHANNEL_USER_TASK,
            "research/plan.py 生成论文检索方案",
        ),
        _spec(
            CONSUMER_MENTOR_REVIEW,
            "导师手动预审",
            CHANNEL_USER_TASK,
            "AI预审方案 §12.2：与导师手动预审同一通道，按进入队列顺序执行",
        ),
        _spec(
            CONSUMER_STUDENT_CHECK,
            "学生手动自检",
            CHANNEL_USER_TASK,
            "AI预审方案 §12.2：与学生手动自检同一通道",
        ),
        _spec(
            CONSUMER_AUTO_REVIEW,
            "提交后自动预审",
            CHANNEL_BACKGROUND,
            "默认关闭；只在正式提交新版本后创建；按 AC 批次逐批取放容量",
        ),
        _spec(
            CONSUMER_PAPER_EXTRACT,
            "论文深度提取",
            CHANNEL_BACKGROUND,
            "每篇/每子任务一次调用后释放容量并重新排队（§12.2）",
        ),
        _spec(
            CONSUMER_PAPER_COMPARE,
            "批量论文比较",
            CHANNEL_BACKGROUND,
            "research/comparison.py；按子任务释放，不得长期霸占唯一模型",
        ),
        _spec(
            CONSUMER_PROGRESS_DRAFT,
            "进度草稿生成",
            CHANNEL_BACKGROUND,
            "LaTeX 进度草稿生成",
        ),
        _spec(
            CONSUMER_KB_SUMMARY,
            "知识库文档总结",
            CHANNEL_BACKGROUND,
            "knowledge_base/kb_summary_api.py 的 map/reduce 调用；"
            "属于批量后台任务，按文档释放",
        ),
        _spec(
            CONSUMER_SESSION_SUMMARY,
            "LLM 会话摘要",
            CHANNEL_BACKGROUND,
            "未来启用的模型摘要（§8.2 最后一项）；当前会话上下文仍用规则摘要",
        ),
        _spec(
            CONSUMER_RESEARCH_REPORT,
            "论文研究报告生成",
            CHANNEL_BACKGROUND,
            "research/report.py：一次报告生成可能触发多次模型调用，"
            "每次调用单独取放容量",
        ),
        _spec(
            CONSUMER_LEGACY_COMPLETION,
            "补全式兼容接口调用",
            CHANNEL_INTERACTIVE,
            "chat/completion.py 的 /other/completion；前台调用，走补全式模型",
        ),
        _spec(
            CONSUMER_UNDECLARED,
            "未登记的生成调用",
            DEFAULT_UNDECLARED_CHANNEL,
            "回落通道：说明该调用点没有声明消费者，必须补登记；"
            "运行时会写告警日志并在 status() 中可见",
        ),
    ]
}


def lookup_consumer(consumer_type: Optional[str]) -> Optional[ConsumerSpec]:
    return CONSUMER_REGISTRY.get((consumer_type or "").strip())


def is_registered_consumer(consumer_type: Optional[str]) -> bool:
    return lookup_consumer(consumer_type) is not None


def channel_of(consumer_type: Optional[str]) -> str:
    """消费者所属通道；未登记时回落到 ``DEFAULT_UNDECLARED_CHANNEL``。"""
    spec = lookup_consumer(consumer_type)
    if spec is not None:
        return spec.channel
    return DEFAULT_UNDECLARED_CHANNEL


def describe_consumers() -> List[Dict[str, str]]:
    """给观测端点用：按通道优先级排序的登记表快照。"""
    ordered = sorted(
        CONSUMER_REGISTRY.values(),
        key=lambda s: (channel_priority(s.channel), s.consumer_type),
    )
    return [
        {
            "consumer_type": s.consumer_type,
            "label": s.label,
            "channel": s.channel,
            "channel_label": CHANNEL_LABELS.get(s.channel, s.channel),
            "notes": s.notes,
        }
        for s in ordered
    ]


__all__ = [
    "CHANNEL_INTERACTIVE",
    "CHANNEL_USER_TASK",
    "CHANNEL_BACKGROUND",
    "CHANNEL_PRIORITY",
    "CHANNEL_LABELS",
    "DEFAULT_UNDECLARED_CHANNEL",
    "normalize_channel",
    "channel_priority",
    "CONSUMER_SMART_CHAT",
    "CONSUMER_KB_CHAT",
    "CONSUMER_PROJECT_DISCUSSION",
    "CONSUMER_INTENT_ROUTE",
    "CONSUMER_AGENT_PLAN",
    "CONSUMER_AGENT_FINAL_ANSWER",
    "CONSUMER_AGENT_TOOL",
    "CONSUMER_RESEARCH_DEFINITION",
    "CONSUMER_RESEARCH_PLAN",
    "CONSUMER_MENTOR_REVIEW",
    "CONSUMER_STUDENT_CHECK",
    "CONSUMER_AUTO_REVIEW",
    "CONSUMER_PAPER_EXTRACT",
    "CONSUMER_PAPER_COMPARE",
    "CONSUMER_PROGRESS_DRAFT",
    "CONSUMER_KB_SUMMARY",
    "CONSUMER_SESSION_SUMMARY",
    "CONSUMER_RESEARCH_REPORT",
    "CONSUMER_LEGACY_COMPLETION",
    "CONSUMER_UNDECLARED",
    "ConsumerSpec",
    "CONSUMER_REGISTRY",
    "lookup_consumer",
    "is_registered_consumer",
    "channel_of",
    "describe_consumers",
]
