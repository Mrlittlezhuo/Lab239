# -*- coding: utf-8 -*-
import json
from typing import List, Sequence, Tuple, Union

from langchain_classic.agents.output_parsers.tools import ToolAgentAction
from langchain_core.agents import AgentAction
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    BaseMessage,
    ToolMessage,
)
from langchain_chatchat.agent_toolkits import BaseToolOutput
from langchain_chatchat.agent_toolkits.all_tools.code_interpreter_tool import (
    CodeInterpreterToolOutput,
)
from langchain_chatchat.agent_toolkits.all_tools.drawing_tool import DrawingToolOutput
from langchain_chatchat.agent_toolkits.all_tools.web_browser_tool import (
    WebBrowserToolOutput,
)
from langchain_chatchat.agents.output_parsers.tools_output.code_interpreter import (
    CodeInterpreterAgentAction,
)
from langchain_chatchat.agents.output_parsers.tools_output.drawing_tool import DrawingToolAgentAction
from langchain_chatchat.agents.output_parsers.tools_output.web_browser import WebBrowserAgentAction


def _create_tool_message(
    agent_action: Union[ToolAgentAction, AgentAction], observation: Union[str, BaseToolOutput]
) -> ToolMessage:
    """Convert agent action and observation into a function message.
    Args:
        agent_action: the tool invocation request from the agent
        observation: the result of the tool invocation
    Returns:
        FunctionMessage that corresponds to the original tool invocation
    """
    if not isinstance(observation, str):
        try:
            content = json.dumps(observation, ensure_ascii=False)
        except Exception:
            content = str(observation)
    else:
        content = observation

    tool_call_id = "abc"
    if isinstance(agent_action, ToolAgentAction):
        tool_call_id = agent_action.tool_call_id

    return ToolMessage(
        tool_call_id=tool_call_id,
        content=content,
        additional_kwargs={"name": agent_action.tool},
    )

def _tool_input_blocks(tool_input: Union[str, dict]) -> str:
    """把工具参数按 prompt 要求的 XML 子标签拼回来。"""
    if not isinstance(tool_input, dict):
        return str(tool_input)
    blocks = []
    for key, value in tool_input.items():
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        blocks.append(f"<{key}>{text}</{key}>")
    return "\n".join(blocks)


def _render_platform_tool_call(agent_action: AgentAction) -> str:
    """还原模型这一轮的输出：它写过的散文 + 它发起的那次工具调用。

    格式必须跟 system prompt 里规定的 Tool Use Formatting 一致（工具名做外层标签，
    参数各占一层子标签），否则模型看到的 assistant 消息是一种它没写过的写法。
    """
    log_text = (agent_action.log or "").strip()
    if getattr(agent_action, "server_name", None):
        # MCPToolAction：prompt 规定的写法是 use_mcp_tool 外层 + server_name/tool_name
        call = (
            "<use_mcp_tool>\n"
            f"<server_name>{agent_action.server_name}</server_name>\n"
            f"<tool_name>{agent_action.tool}</tool_name>\n"
            f"{_tool_input_blocks(agent_action.tool_input)}\n"
            "</use_mcp_tool>"
        )
    else:
        call = (
            f"<{agent_action.tool}>\n"
            f"{_tool_input_blocks(agent_action.tool_input)}\n"
            f"</{agent_action.tool}>"
        )
    return f"{log_text}\n\n{call}" if log_text else call


def _render_observation(agent_action: AgentAction, observation: Union[str, BaseToolOutput]) -> str:
    """工具结果按协议放在 user 消息里（prompt：结果会出现在 user's response 中），
    但要写清这是哪次调用的结果，否则模型会把这段文本当成用户新提的要求。"""
    text = observation if isinstance(observation, str) else str(observation)
    if getattr(agent_action, "server_name", None):
        label = (
            f"MCP 工具「{agent_action.tool}」"
            f"（服务「{agent_action.server_name}」）的执行结果"
        )
    else:
        label = f"工具「{agent_action.tool}」的执行结果"
    return f"{label}：\n{text}"


def format_to_platform_tool_messages(
    intermediate_steps: Sequence[Tuple[AgentAction, BaseToolOutput]],
) -> List[BaseMessage]:
    """Convert (AgentAction, tool output) tuples into FunctionMessages.

    Args:
        intermediate_steps: Steps the LLM has taken to date, along with observations

    Returns:
        list of messages to send to the LLM for the next prediction
    """
    messages = []

    for idx, (agent_action, observation) in enumerate(intermediate_steps):
        # === CodeInterpreter ===
        if isinstance(agent_action, CodeInterpreterAgentAction):
            if isinstance(observation, CodeInterpreterToolOutput):
                sandbox_type = observation.platform_params.get("sandbox", "auto")
                if sandbox_type == "auto":
                    new_messages = [
                        AIMessage(content=str(observation.code_input)),
                        _create_tool_message(agent_action, observation),
                    ]
                elif sandbox_type == "none":
                    new_messages = [
                        AIMessage(content=str(observation.code_input)),
                        _create_tool_message(agent_action, observation.code_output),
                    ]
                else:
                    raise ValueError(f"Unknown sandbox type: {sandbox_type}")
                messages.extend([m for m in new_messages if m not in messages])
            else:
                raise ValueError(f"Unknown observation type: {type(observation)}")

        # === DrawingTool ===
        elif isinstance(agent_action, DrawingToolAgentAction):
            if isinstance(observation, DrawingToolOutput):
                messages.append(AIMessage(content=str(observation)))
            else:
                raise ValueError(f"Unknown observation type: {type(observation)}")

        # === WebBrowser ===
        elif isinstance(agent_action, WebBrowserAgentAction):
            if isinstance(observation, WebBrowserToolOutput):
                messages.append(AIMessage(content=str(observation)))
            else:
                raise ValueError(f"Unknown observation type: {type(observation)}")

        # === ToolAgentAction ===
        elif isinstance(agent_action, ToolAgentAction):
            ai_msgs = AIMessage(
                content=f"arguments='{agent_action.tool_input}', name='{agent_action.tool}'",
                additional_kwargs={
                    "tool_calls": [
                        {
                            "index": idx,
                            "id": agent_action.tool_call_id,
                            "type": "function",
                            "function": {
                                "name": agent_action.tool,
                                "arguments": json.dumps(agent_action.tool_input, ensure_ascii=False),
                            },
                        }
                    ]
                },
            )
            messages.extend([ai_msgs, _create_tool_message(agent_action, observation)])

        # === Generic AgentAction ===
        elif isinstance(agent_action, AgentAction):
            # 这里假设 observation 是本项目自定义prompt tools，而不是 模型测tools
            #
            # 模型自己那次调用必须留在上下文里。以前 AIMessage 只放
            # agent_action.log，而 XML 解析出来的 log 只是标签外的散文（模型常常
            # 不写，实测就是空串），于是 scratchpad 变成「空 assistant 消息 + 一段
            # 用户消息里的工具结果」：模型看不到自己已经调过这个工具，把同一个
            # 调用原样再发一遍，直到撞到 max_iterations。
            # 现场（2026-10-12 15:25 日志）：search_local_knowledgebase 每次都
            # 返回了相关文档，仍然连发 4 次（第 4 次与第 1 次参数、结果完全相同）。
            ai_msgs = AIMessage(content=_render_platform_tool_call(agent_action))
            messages.extend(
                [ai_msgs, HumanMessage(content=_render_observation(agent_action, observation))]
            )

        # === Fallback ===
        else:
            messages.append(AIMessage(content=getattr(agent_action, "log", str(agent_action))))

    return messages
