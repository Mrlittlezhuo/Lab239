# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import logging
import time
from typing import (
    Any,
    Dict,
    List,
    Optional,
    Tuple, Union,
    Sequence
)

from langchain_classic.agents.agent import AgentExecutor
from langchain_classic.utilities.asyncio import asyncio_timeout
from langchain_core.agents import AgentAction, AgentFinish, AgentStep
from langchain_core.callbacks import (
    AsyncCallbackManagerForChainRun,
    CallbackManagerForChainRun,
)
from langchain_core.runnables.base import RunnableSequence
from langchain_classic.agents.agent import BaseMultiActionAgent
from langchain_core.tools import BaseTool
from langchain_core.utils import get_color_mapping

from chatchat.server.pydantic_v1 import root_validator
from langchain_chatchat.agent_toolkits.all_tools.struct_type import (
    AdapterAllToolStructType,
)
from langchain_chatchat.agent_toolkits.mcp_kit.tools import MCPStructuredTool

from langchain_chatchat.agents.output_parsers.tools_output.drawing_tool import DrawingToolAgentAction
from langchain_chatchat.agents.output_parsers.tools_output.web_browser import WebBrowserAgentAction
from langchain_chatchat.agents.output_parsers.platform_tools import PlatformToolsAgentOutputParser
from langchain_chatchat.agents.output_parsers import MCPToolAction
from sqlalchemy import Null
logger = logging.getLogger(__name__)

NextStepOutput = List[Union[AgentFinish, MCPToolAction, AgentAction, AgentStep]]


class PlatformToolsAgentExecutor(AgentExecutor):
    mcp_tools: Sequence[MCPStructuredTool] = []

    @root_validator()
    def validate_return_direct_tool(cls, values: Dict) -> Dict:
        """Validate that tools are compatible with agent.
            TODO: platform adapter tool for all  tools,
        """
        agent = values["agent"]
        tools = values["tools"]
        if isinstance(agent.runnable, RunnableSequence):
            if isinstance(agent.runnable.last, PlatformToolsAgentOutputParser):
                for tool in tools:
                    if tool.return_direct:
                        logger.warning(
                            f"Tool {tool.name} has return_direct set to True, but it is not compatible with the "
                            f"current agent."
                        )
        elif isinstance(agent, BaseMultiActionAgent):
            for tool in tools:
                if tool.return_direct:
                    raise ValueError(
                        "Tools that have `return_direct=True` are not allowed "
                        "in multi-action agents"
                    )

        return values

    def _call(
        self,
        inputs: Dict[str, str],
        run_manager: Optional[CallbackManagerForChainRun] = None,
    ) -> Dict[str, Any]:
        """Run text through and get agent response."""
        # Construct a mapping of tool name to tool for easy lookup
        name_to_tool_map = {tool.name: tool for tool in self.tools}
        # We construct a mapping from each tool to a color, used for logging.
        color_mapping = get_color_mapping(
            [tool.name for tool in self.tools], excluded_colors=["green", "red"]
        )
        intermediate_steps: List[Tuple[AgentAction, str]] = inputs.get("intermediate_steps") if inputs.get("intermediate_steps") is not None else []
        # 确保 inputs 里不带 intermediate_steps
        if "intermediate_steps" in inputs:
            inputs = dict(inputs)
            inputs.pop("intermediate_steps")
        # Let's start tracking the number of iterations and time elapsed
        iterations = 0
        time_elapsed = 0.0
        start_time = time.time()
        # We now enter the agent loop (until it returns something).
        while self._should_continue(iterations, time_elapsed):
            next_step_output = self._take_next_step(
                name_to_tool_map,
                color_mapping,
                inputs,
                intermediate_steps,
                run_manager=run_manager,
            )
            if isinstance(next_step_output, AgentFinish):
                return self._return(
                    next_step_output, intermediate_steps, run_manager=run_manager
                )

            intermediate_steps.extend(next_step_output)
            stopped = self._stop_on_missing_tool(next_step_output, name_to_tool_map)
            if stopped is not None:
                return self._return(
                    stopped, intermediate_steps, run_manager=run_manager
                )
            if len(next_step_output) == 1:
                next_step_action = next_step_output[0]
                # See if tool should return directly
                tool_return = self._get_tool_return(next_step_action)
                if tool_return is not None:
                    return self._return(
                        tool_return, intermediate_steps, run_manager=run_manager
                    )
            iterations += 1
            time_elapsed = time.time() - start_time
        output = self.agent.return_stopped_response(
            self.early_stopping_method, intermediate_steps, **inputs
        )
        return self._return(output, intermediate_steps, run_manager=run_manager)

    async def _acall(
        self,
        inputs: Dict[str, str],
        run_manager: Optional[AsyncCallbackManagerForChainRun] = None,
    ) -> Dict[str, str]:
        """Run text through and get agent response."""
        # Construct a mapping of tool name to tool for easy lookup
        name_to_tool_map = {tool.name: tool for tool in self.tools}
        # We construct a mapping from each tool to a color, used for logging.
        color_mapping = get_color_mapping(
            [tool.name for tool in self.tools], excluded_colors=["green"]
        )
        intermediate_steps: List[Tuple[AgentAction, str]] = inputs.get("intermediate_steps") if inputs.get("intermediate_steps") is not None else []
        # 确保 inputs 里不带 intermediate_steps
        if "intermediate_steps" in inputs:
            inputs = dict(inputs)
            inputs.pop("intermediate_steps")
        # Let's start tracking the number of iterations and time elapsed
        iterations = 0
        time_elapsed = 0.0
        start_time = time.time()
        # We now enter the agent loop (until it returns something).
        try:
            async with asyncio_timeout(self.max_execution_time):
                while self._should_continue(iterations, time_elapsed):
                    next_step_output = await self._atake_next_step(
                        name_to_tool_map,
                        color_mapping,
                        inputs,
                        intermediate_steps,
                        run_manager=run_manager,
                    )
                    if isinstance(next_step_output, AgentFinish):
                        return await self._areturn(
                            next_step_output,
                            intermediate_steps,
                            run_manager=run_manager,
                        )

                    intermediate_steps.extend(next_step_output)
                    stopped = self._stop_on_missing_tool(
                        next_step_output, name_to_tool_map
                    )
                    if stopped is not None:
                        return await self._areturn(
                            stopped, intermediate_steps, run_manager=run_manager
                        )
                    if len(next_step_output) >= 1:
                        # TODO: platform adapter status control, but langchain not output message info,
                        #   so where after paser instance object to let's DrawingToolAgentAction WebBrowserAgentAction
                        #   always output AgentFinish instance
                        continue_action = False
                        list1 = list(name_to_tool_map.keys())
                        list2 = [
                            AdapterAllToolStructType.WEB_BROWSER,
                            AdapterAllToolStructType.DRAWING_TOOL,
                        ]

                        exist_tools = list(set(list1) - set(list2))
                        for next_step_action, observation in next_step_output:
                            if next_step_action.tool in exist_tools:
                                continue_action = True
                                break

                        if not continue_action:
                            for next_step_action, observation in next_step_output:
                                if isinstance(next_step_action, DrawingToolAgentAction):
                                    tool_return = AgentFinish(
                                        return_values={"output": str(observation)},
                                        log=str(observation),
                                    )
                                    return await self._areturn(
                                        tool_return,
                                        intermediate_steps,
                                        run_manager=run_manager,
                                    )
                                elif isinstance(
                                    next_step_action, WebBrowserAgentAction
                                ):
                                    tool_return = AgentFinish(
                                        return_values={"output": str(observation)},
                                        log=str(observation),
                                    )
                                    return await self._areturn(
                                        tool_return,
                                        intermediate_steps,
                                        run_manager=run_manager,
                                    )

                    if len(next_step_output) == 1:
                        next_step_action = next_step_output[0]
                        # See if tool should return directly
                        tool_return = self._get_tool_return(next_step_action)
                        if tool_return is not None:
                            return await self._areturn(
                                tool_return, intermediate_steps, run_manager=run_manager
                            )

                    iterations += 1
                    time_elapsed = time.time() - start_time
                output = self.agent.return_stopped_response(
                    self.early_stopping_method, intermediate_steps, **inputs
                )
                return await self._areturn(
                    output, intermediate_steps, run_manager=run_manager
                )
        except (TimeoutError, asyncio.TimeoutError):
            # stop early when interrupted by the async timeout
            output = self.agent.return_stopped_response(
                self.early_stopping_method, intermediate_steps, **inputs
            )
            return await self._areturn(
                output, intermediate_steps, run_manager=run_manager
            )

    def _find_mcp_tool(self, agent_action: MCPToolAction) -> Optional[BaseTool]:
        """按 tool 名 + server_name 找 MCP 工具，两个字段必须同时匹配。"""
        for tool in self.mcp_tools:
            if tool.name == agent_action.tool and tool.server_name == agent_action.server_name:
                return tool
        return None

    def _missing_tool_reason(
        self, agent_action: AgentAction, name_to_tool_map: Dict[str, BaseTool]
    ) -> Optional[str]:
        """工具名不在本轮可用清单里时给出可读原因，否则返回 ``None``。

        这类失败模型修不了：观察结果只有一句 "xxx is not a valid tool"，它看不懂，
        只会把同一个调用原样再发一遍。实测一轮里连发 15 次（正好撞到 langchain 的
        ``max_iterations``），每轮一次模型调用，界面上只剩一串"好的，我再尝试一次"，
        而工具从头到尾没执行过（日志里 15 条 agent_action、0 条 tool_start/tool_end）。
        """
        if isinstance(agent_action, MCPToolAction):
            if self._find_mcp_tool(agent_action) is None:
                return (
                    f"MCP 工具「{agent_action.tool}」"
                    f"（服务「{agent_action.server_name}」）本轮没有加载成功，已停止重试"
                )
            return None

        if agent_action.tool in name_to_tool_map:
            return None
        available = "、".join(name_to_tool_map) or "无"
        return (
            f"工具「{agent_action.tool}」本轮不可用"
            f"（本轮可用工具：{available}），已停止重试"
        )

    def _stop_on_missing_tool(
        self,
        next_step_output: List[Tuple[AgentAction, Any]],
        name_to_tool_map: Dict[str, BaseTool],
    ) -> Optional[AgentFinish]:
        """本轮里有任何一次调用指向不存在的工具时，直接收尾。

        找不到工具的那条分支不会触发 tool_start/tool_end，所以前端看不到失败，
        模型也收不到"这次没执行"的有效信号；把它认出来并收尾，比让模型重试到
        步数上限（最后回答是 langchain 的 "Agent stopped due to max iterations."）
        更省模型调用，也更说得清。
        """
        for action, _ in next_step_output:
            reason = self._missing_tool_reason(action, name_to_tool_map)
            if reason:
                logger.warning(reason)
                return AgentFinish(return_values={"output": reason}, log=reason)
        return None

    def _perform_agent_action(
        self,
        name_to_tool_map: Dict[str, BaseTool],
        color_mapping: Dict[str, str],
        agent_action: AgentAction,
        run_manager: Optional[CallbackManagerForChainRun] = None,
    ) -> AgentStep:
        if run_manager:
            run_manager.on_agent_action(agent_action, color="green")
        
        if isinstance(agent_action, MCPToolAction): 
            tool_run_kwargs = self.agent.tool_run_logging_kwargs()
            # Find the MCP tool by name and server_name from self.mcp_tools
            mcp_tool = self._find_mcp_tool(agent_action)
            
            if mcp_tool:
                try:
                    observation = mcp_tool.run(
                        agent_action.tool_input,
                        verbose=self.verbose,
                        color="blue",
                        callbacks=run_manager.get_child() if run_manager else None,
                        **tool_run_kwargs,
                    )
                except NotImplementedError:
                    # MCPStructuredTool 只有协程实现（_arun），LangChain 的同步
                    # run() 对它一律抛 NotImplementedError。产品的两条聊天路径
                    # （chat / agent_run）都走 ainvoke → _aperform_agent_action，
                    # 落到这里说明执行器被当同步驱动了；把原因写成可行动的观察，
                    # 而不是让整轮崩在一句 "does not support sync invocation" 上。
                    observation = (
                        f"MCP tool '{agent_action.tool}' from server "
                        f"'{agent_action.server_name}' only supports async invocation; "
                        f"this agent was driven synchronously, so it cannot be run here."
                    )
            else:
                observation = self._missing_tool_reason(
                    agent_action, name_to_tool_map
                ) or f"MCP tool '{agent_action.tool}' is not available"

        # Otherwise we lookup the tool
        elif agent_action.tool in name_to_tool_map:
            tool = name_to_tool_map[agent_action.tool]
            return_direct = tool.return_direct
            color = color_mapping[agent_action.tool]
            tool_run_kwargs = self.agent.tool_run_logging_kwargs()
            if return_direct:
                tool_run_kwargs["llm_prefix"] = ""
            # We then call the tool on the tool input to get an observation
            # TODO: platform adapter tool for all  tools,
            #       view tools binding langchain_chatchat/agents/platform_tools/base.py:188
            if agent_action.tool in AdapterAllToolStructType.__members__.values():
                observation = tool.run(
                    {
                        "agent_action": agent_action,
                    },
                    verbose=self.verbose,
                    color="red",
                    callbacks=run_manager.get_child() if run_manager else None,
                    **tool_run_kwargs,
                )
            else:
                observation = tool.run(
                    agent_action.tool_input,
                    verbose=self.verbose,
                    color=color,
                    callbacks=run_manager.get_child() if run_manager else None,
                    **tool_run_kwargs,
                )
        else:
            observation = self._missing_tool_reason(
                agent_action, name_to_tool_map
            ) or f"Tool '{agent_action.tool}' is not available"
        return AgentStep(action=agent_action, observation=observation)

    def _consume_next_step(
            self, values: NextStepOutput
    ) -> Union[AgentFinish, List[Tuple[AgentAction, str]]]:
        if isinstance(values[-1], AgentFinish):
            return values[-1]
        else:
            return [
                (a.action, a.observation) for a in values if isinstance(a, AgentStep)
            ]

    async def _aperform_agent_action(
        self,
        name_to_tool_map: Dict[str, BaseTool],
        color_mapping: Dict[str, str],
        agent_action: AgentAction,
        run_manager: Optional[AsyncCallbackManagerForChainRun] = None,
    ) -> AgentStep:
        if run_manager:
            await run_manager.on_agent_action(
                agent_action, verbose=self.verbose, color="green"
            )
        if isinstance(agent_action, MCPToolAction): 
            tool_run_kwargs = self.agent.tool_run_logging_kwargs()
            # Find the MCP tool by name and server_name from self.mcp_tools
            mcp_tool = self._find_mcp_tool(agent_action)
            
            if mcp_tool:
                observation = await mcp_tool.arun(
                    agent_action.tool_input,
                    verbose=self.verbose,
                    color="blue",
                    callbacks=run_manager.get_child() if run_manager else None,
                    **tool_run_kwargs,
                )
            else:
                observation = self._missing_tool_reason(
                    agent_action, name_to_tool_map
                ) or f"MCP tool '{agent_action.tool}' is not available"

        # Otherwise we lookup the tool
        elif agent_action.tool in name_to_tool_map:
            tool = name_to_tool_map[agent_action.tool]
            return_direct = tool.return_direct
            color = color_mapping[agent_action.tool]
            tool_run_kwargs = self.agent.tool_run_logging_kwargs()
            if return_direct:
                tool_run_kwargs["llm_prefix"] = ""
            # We then call the tool on the tool input to get an observation
            # TODO: platform adapter tool for all  tools,
            #       view tools binding
            #       langchain_chatchat.agents.platform_tools.base.PlatformToolsRunnable.paser_all_tools
            if agent_action.tool in AdapterAllToolStructType.__members__.values():
                observation = await tool.arun(
                    {
                        "agent_action": agent_action,
                    },
                    verbose=self.verbose,
                    color="red",
                    callbacks=run_manager.get_child() if run_manager else None,
                    **tool_run_kwargs,
                )
            else:
                observation = await tool.arun(
                    agent_action.tool_input,
                    verbose=self.verbose,
                    color=color,
                    callbacks=run_manager.get_child() if run_manager else None,
                    **tool_run_kwargs,
                )
        else:
            observation = self._missing_tool_reason(
                agent_action, name_to_tool_map
            ) or f"Tool '{agent_action.tool}' is not available"
        return AgentStep(action=agent_action, observation=observation)
