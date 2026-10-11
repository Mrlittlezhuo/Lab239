import logging
import requests
from requests.auth import HTTPBasicAuth
from urllib.parse import parse_qs
from typing import Optional

from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_core.runnables import RunnablePassthrough

from chatchat.server.agent.tools_access import is_configured
from chatchat.server.pydantic_v1 import Field
from chatchat.server.utils import get_tool_config, get_ChatOpenAI
# from chatchat.server.api_server.chat_routes import global_model_name
# from chatchat.configs import (
#     MAX_TOKENS,
# )

from .tools_registry import regist_tool

from langchain_chatchat.agent_toolkits.all_tools.tool import (
    BaseToolOutput,
)
logger = logging.getLogger()

TIMEOUT = 30

# Prompt for the prom_chain
PROMETHEUS_PROMPT_TEMPLATE = """ You are an expert in Prometheus, a powerful time-series monitoring service. 
Your main task is to translate users' specific requirements into PromQL queries. 
This includes understanding their monitoring needs, the specific metrics they are interested in, 
the time period for which they want the data, and any specific conditions or thresholds they want to apply. 
Your goal is to provide the most accurate and efficient PromQL query based on the given information.

Please return the PromQL in a format that can be used with the HTTP API, such as:

'query?query=up&time=2015-07-01T20:10:51.781Z' for instant data queries.
'query_range?query=up&start=2015-07-01T20:10:30.781Z&end=2015-07-01T20:11:00.781Z&step=15s' for range data queries.

I will automatically fill in the Prometheus IP and port. Please provide the query according to the example, 
and no other content is needed.

Question: {query} 
"""


def execute_promql_request(url: str, params: dict, auth: Optional[HTTPBasicAuth], promql: str) -> str:
    try:
        response = requests.get(url, params=params, auth=auth, timeout=TIMEOUT)
    except requests.exceptions.RequestException as e:
        return f"PromQL: {promql} 的错误: {type(e).__name__}: {e}\n（请确认 {url} 可访问）"

    if response.status_code == 200:
        try:
            return f"PromQL: {promql} 的查询结果: {response.json()}\n"
        except ValueError:
            return f"PromQL: {promql} 的错误: 响应不是 JSON：{response.text[:200]}\n"
    else:
        return f"PromQL: {promql} 的错误: HTTP {response.status_code} {response.text[:200]}\n"


def query_prometheus(query: str, config: dict) -> str:
    prometheus_endpoint = config["prometheus_endpoint"]
    username = config["username"]
    password = config["password"]

    if not is_configured(prometheus_endpoint):
        return (
            "未配置 Prometheus 地址：请在 tool_settings.yaml 的 "
            "text2promql.prometheus_endpoint 填入真实地址（当前是空值或占位符）"
        )

    llm = get_ChatOpenAI(
        # model_name=global_model_name,
        # temperature=0.1,
        # streaming=True,
        # local_wrap=True,
        # verbose=True,
        # max_tokens=MAX_TOKENS,
    )
    if llm is None:
        return (
            "无法初始化对话模型：text2promql 使用默认对话模型，"
            "请确认 model_settings.yaml 里的默认模型已配置且可用"
        )

    prometheus_prompt = ChatPromptTemplate.from_template(PROMETHEUS_PROMPT_TEMPLATE)
    output_parser = StrOutputParser()

    prometheus_chain = (
        {"query": RunnablePassthrough()}
        | prometheus_prompt
        | llm
        | output_parser
    )

    # 根据用户名和密码是否存在来设置 auth 参数
    auth = HTTPBasicAuth(username, password) if username and password else None

    try:
        promql = prometheus_chain.invoke(query)
    except Exception as exc:
        # 大模型侧失败（模型没配、超时、限流）以前会直接穿透成接口 500
        return f"生成 PromQL 失败：{type(exc).__name__}: {exc}\n"
    logger.info(f"PromQL: {promql}")

    # debug
    # promql = 'query?query=kube_pod_status_phase{namespace="default"}'

    # 从 promql 字符串中提取 query_type 和 参数
    try:
        query_type, query_params = promql.split('?')
    except ValueError as e:
        logger.error(f"Promql split error: {e}")
        content = f"PromQL: {promql} 的错误: {e}\n"
        return content

    prometheus_url = f"{prometheus_endpoint}/api/v1/{query_type}"

    # params 的格式如下:
    """
    {
    "query": "kube_pod_status_phase{namespace=\"default\"}"
    }
    """
    params = {k: v[0] for k, v in parse_qs(query_params).items()}

    # 向 Prometheus 发起 HTTP 请求
    content = execute_promql_request(prometheus_url, params, auth, promql)

    logger.info(content)
    return content


@regist_tool(title="Prometheus对话")
def text2promql(
        query: str = Field(
            description="Tool for querying a Prometheus server, No need for PromQL statements, "
                        "just input the natural language that you want to chat with prometheus"
        )
):
    """Use this tool to chat with prometheus, Input natural language,
    then it will convert it into PromQL and execute it in the prometheus, then return the execution result."""
    tool_config = get_tool_config("text2promql")
    return BaseToolOutput(query_prometheus(query=query, config=tool_config))
