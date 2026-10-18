from langchain_classic.chains import LLMChain
from langchain_community.utilities import SQLDatabase
from langchain_core.prompts.prompt import PromptTemplate
from langchain_experimental.sql import SQLDatabaseChain, SQLDatabaseSequentialChain
from sqlalchemy import event
from sqlalchemy.exc import OperationalError

from chatchat.server.agent.tools_access import is_configured
from chatchat.server.pydantic_v1 import Field
from chatchat.server.utils import get_tool_config

from .tools_registry import regist_tool

from langchain_chatchat.agent_toolkits.all_tools.tool import (
    BaseToolOutput,
)
from chatchat.server.model_capacity import CONSUMER_AGENT_TOOL

READ_ONLY_PROMPT_TEMPLATE = """You are a MySQL expert. The database is currently in read-only mode. 
Given an input question, determine if the related SQL can be executed in read-only mode.
If the SQL can be executed normally, return Answer:'SQL can be executed normally'.
If the SQL cannot be executed normally, return Answer: 'SQL cannot be executed normally'.
Use the following format:

Answer: Final answer here

Question: {query}
"""


# 定义一个拦截器函数来检查SQL语句，以支持read-only,可修改下面的write_operations，以匹配你使用的数据库写操作关键字
def intercept_sql(conn, cursor, statement, parameters, context, executemany):
    # List of SQL keywords that indicate a write operation
    write_operations = (
        "insert",
        "update",
        "delete",
        "create",
        "drop",
        "alter",
        "truncate",
        "rename",
    )
    # Check if the statement starts with any of the write operation keywords
    if any(statement.strip().lower().startswith(op) for op in write_operations):
        raise OperationalError(
            "Database is read-only. Write operations are not allowed.",
            params=None,
            orig=None,
        )


def _run_chain(
    db,
    llm,
    query: str,
    table_names: list,
    top_k: int,
    return_intermediate_steps: bool,
    read_only: bool,
):
    """跑一次 text2sql 的链。返回 str 表示提前得出的结论（比如只读拒绝）。

    单独拆出来是为了让调用方能整体 try/except：链里任何一步（大模型报错、
    SQL 语法错、表不存在）以前都会直接穿透成接口 500，用户看不到原因。
    """
    if read_only:
        # 在read_only下，先让大模型判断只读模式是否能满足需求，避免后续执行过程报错，返回友好提示。
        READ_ONLY_PROMPT = PromptTemplate(
            input_variables=["query"],
            template=READ_ONLY_PROMPT_TEMPLATE,
        )
        read_only_chain = LLMChain(
            prompt=READ_ONLY_PROMPT,
            llm=llm,
        )
        read_only_result = read_only_chain.invoke(query)
        if "SQL cannot be executed normally" in read_only_result["text"]:
            return "当前数据库为只读状态，无法满足您的需求！"

        # 当然大模型不能保证完全判断准确，为防止大模型判断有误，再从拦截器层面拒绝写操作
        event.listen(db._engine, "before_cursor_execute", intercept_sql)

    # 如果不指定table_names，优先走SQLDatabaseSequentialChain，这个链会先预测需要哪些表，然后再将相关表输入SQLDatabaseChain
    # 这是因为如果不指定table_names，直接走SQLDatabaseChain，Langchain会将全量表结构传递给大模型，可能会因token太长从而引发错误，也浪费资源
    # 如果指定了table_names，直接走SQLDatabaseChain，将特定表结构传递给大模型进行判断
    if len(table_names) > 0:
        db_chain = SQLDatabaseChain.from_llm(
            llm,
            db,
            verbose=True,
            top_k=top_k,
            return_intermediate_steps=return_intermediate_steps,
        )
        return db_chain.invoke({"query": query, "table_names_to_use": table_names})

    # 先预测会使用哪些表，然后再将问题和预测的表给大模型
    db_chain = SQLDatabaseSequentialChain.from_llm(
        llm,
        db,
        verbose=True,
        top_k=top_k,
        return_intermediate_steps=return_intermediate_steps,
    )
    return db_chain.invoke(query)


def query_database(query: str, config: dict):
    model_name= config["model_name"]
    top_k = config["top_k"]
    return_intermediate_steps = config["return_intermediate_steps"]
    sqlalchemy_connect_str = config["sqlalchemy_connect_str"]
    read_only = config["read_only"]

    # 配置里留着的是 `mysql+pymysql://用户名:密码@主机地址/数据库名称` 这样的中文占位符，
    # 原样拿去连库，SQLAlchemy 会报一个看不懂的编码/主机解析错误。
    if not is_configured(sqlalchemy_connect_str):
        return (
            "未配置数据库连接串：请在 tool_settings.yaml 的 "
            "text2sql.sqlalchemy_connect_str 填入真实连接串（当前是空值或占位符）"
        )

    try:
        db = SQLDatabase.from_uri(sqlalchemy_connect_str)
    except Exception as exc:
        # 连不上、缺驱动、表不存在都在这里，必须带出真实原因，
        # 否则接口层只剩一句 failed to call tool 'text2sql'
        return (
            f"连接数据库失败：{type(exc).__name__}: {exc}\n"
            "请确认 text2sql.sqlalchemy_connect_str 正确、数据库可访问、"
            "且已安装对应驱动（如 pymysql、psycopg2）"
        )

    from chatchat.server.utils import get_ChatOpenAI

    llm = get_ChatOpenAI(
        consumer=CONSUMER_AGENT_TOOL,
        model_name=model_name,
        temperature=0.1,
        streaming=True,
        local_wrap=True,
        verbose=True,
    )
    if llm is None:
        return (
            f"无法初始化对话模型「{model_name}」：text2sql 使用自己在配置里指定的模型，"
            "与前端选择的模型无关；请确认该模型已在 model_settings.yaml 中配置且可用"
        )
    table_names = config["table_names"]
    table_comments = config["table_comments"]
    result = None

    # 如果发现大模型判断用什么表出现问题，尝试给langchain提供额外的表说明，辅助大模型更好的判断应该使用哪些表，尤其是SQLDatabaseSequentialChain模式下,是根据表名做的预测，很容易误判
    # 由于langchain固定了输入参数，所以只能通过query传递额外的表说明
    if table_comments:
        TABLE_COMMNET_PROMPT = (
            "\n\nI will provide some special notes for a few tables:\n\n"
        )
        table_comments_str = "\n".join([f"{k}:{v}" for k, v in table_comments.items()])
        query = query + TABLE_COMMNET_PROMPT + table_comments_str + "\n\n"

    try:
        result = _run_chain(
            db=db,
            llm=llm,
            query=query,
            table_names=table_names,
            top_k=top_k,
            return_intermediate_steps=return_intermediate_steps,
            read_only=read_only,
        )
    except Exception as exc:
        return f"执行数据库查询失败：{type(exc).__name__}: {exc}"

    if isinstance(result, str):
        return result
    if not isinstance(result, dict) or result.get("result") is None:
        return f"数据库查询没有返回结果，原始响应：{result!r}"

    context = f"""查询结果:{result['result']}\n\n"""

    intermediate_steps = result.get("intermediate_steps")
    # 如果存在intermediate_steps，且这个数组的长度大于2，则保留最后两个元素，因为前面几个步骤存在示例数据，容易引起误解
    if intermediate_steps:
        if len(intermediate_steps) > 2:
            sql_detail = intermediate_steps[-2:-1][0]["input"]
            # sql_detail截取从SQLQuery到Answer:之间的内容
            sql_detail = sql_detail[
                sql_detail.find("SQLQuery:") + 9 : sql_detail.find("Answer:")
            ]
            context = context + "执行的sql:'" + sql_detail + "'\n\n"
    return context


@regist_tool(title="数据库对话")
def text2sql(
    query: str = Field(
        description="No need for SQL statements,just input the natural language that you want to chat with database"
    ),
):
    """Use this tool to chat with  database,Input natural language, then it will convert it into SQL and execute it in the database, then return the execution result."""
    tool_config = get_tool_config("text2sql")
    return BaseToolOutput(query_database(query=query, config=tool_config))
