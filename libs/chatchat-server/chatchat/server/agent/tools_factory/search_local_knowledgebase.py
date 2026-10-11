from urllib.parse import urlencode

from chatchat.settings import Settings
from chatchat.server.agent.tools_factory.tools_registry import (
    regist_tool,
    format_context,
)

from langchain_chatchat.agent_toolkits.all_tools.tool import (
    BaseToolOutput,
)
from chatchat.server.knowledge_base.kb_api import list_kbs
from chatchat.server.knowledge_base.kb_doc_api import search_docs
from chatchat.server.pydantic_v1 import Field
from chatchat.server.utils import get_tool_config

template = (
    "Use local knowledgebase from one or more of these:\n{KB_info}\n to get information，Only local data on "
    "this knowledge use this tool. The 'database' should be one of the above [{key}]."
)
KB_info_str = "\n".join([f"{key}: {value}" for key, value in Settings.kb_settings.KB_INFO.items()])
template_knowledge = template.format(KB_info=KB_info_str, key="samples")


def search_knowledgebase(query: str, database: str, config: dict):
    docs = search_docs(
        query=query,
        knowledge_base_name=database,
        top_k=config["top_k"],
        score_threshold=config["score_threshold"],
        file_name="",
        metadata={},
    )
    return {"knowledge_base": database, "docs": docs}


@regist_tool(description=template_knowledge, title="本地知识库")
def search_local_knowledgebase(
    # 这里以前写 choices=[kb.kb_name for kb in list_kbs().data]：装饰器参数在导入期求值，
    # 于是 import tools_factory 就要查一次数据库（get_tool() 每次调用都 reload 这个包），
    # 数据库不可用时整个工具注册表都加载不出来。可选知识库清单由
    # utils.update_search_local_knowledgebase_tool() 在运行期注入 schema，本来就是它负责刷新。
    database: str = Field(
        description="Database for Knowledge Search",
    ),
    query: str = Field(description="Query for Knowledge Search"),
):
    """"""
    available = [kb.kb_name for kb in list_kbs().data]
    if database not in available:
        # 模型（或用户）给了一个不存在的知识库名时，不该让检索层报一句看不懂的错
        names = "、".join(available) or "无（还没有创建任何知识库）"
        return BaseToolOutput({
            "error": f"知识库「{database}」不存在。当前可用知识库：{names}"
        })

    tool_config = get_tool_config("search_local_knowledgebase")
    try:
        ret = search_knowledgebase(query=query, database=database, config=tool_config)
    except Exception as exc:
        # 注意不能配 format=format_context：它会把 {"error": ...} 吞成
        # "没有找到相关文档,请更换关键词重试"
        return BaseToolOutput({
            "error": f"检索知识库「{database}」失败：{type(exc).__name__}: {exc}"
        })
    return BaseToolOutput(ret, format=format_context)
