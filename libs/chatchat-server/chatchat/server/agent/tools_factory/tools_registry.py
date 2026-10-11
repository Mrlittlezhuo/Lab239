from __future__ import annotations

import inspect
import json
import re
from typing import Any, Callable, Dict, Optional, Type, Union, List

from langchain_classic.agents import tool
from langchain_core.tools import BaseTool
from pydantic import ConfigDict
from pydantic.fields import FieldInfo

from chatchat.server.pydantic_v1 import BaseModel, Extra, create_model
from langchain_chatchat.utils import PYDANTIC_V2

__all__ = ["regist_tool", "BaseToolOutput", "format_context"]


_TOOLS_REGISTRY = {}


# patch BaseTool to support extra fields e.g. a title
if PYDANTIC_V2:
    # pydantic v2: 通过覆盖基类 model_config 使后续创建的 tool 子类
    # 允许未声明的构造参数（等价于 v1 的 BaseTool.Config.extra = Extra.allow）
    _base_tool_config = dict(BaseTool.model_config)
    _base_tool_config["extra"] = "allow"
    BaseTool.model_config = ConfigDict(**_base_tool_config)
else:
    BaseTool.Config.extra = Extra.allow

# 说明：v1 时代为绕过 langchain#15855 曾在此 monkeypatch
# ``BaseTool._parse_input`` / ``BaseTool._to_args_and_kwargs``（2 参签名）。
# 迁移到 langchain-core v2 后，核心实现的 ``run``/``ainvoke`` 以 3 参
# （``tool_input, tool_call_id``）调用这两个方法，且原生实现已覆盖
# pydantic Field 工具参数与 ``InjectedToolCallId`` 的处理（项目工具未使用
# 后两者），因此移除该补丁，改用 v2 原生实现。


def _func_fieldinfo_has_schema_extra(func: Callable) -> bool:
    """函数签名上是否存在携带 json_schema_extra（如 v1 风格 ``choices``）的
    FieldInfo。v2 的 tool() 推断 schema 时仅透传 description，会丢弃这些信息。
    """
    try:
        sig = inspect.signature(func)
    except (TypeError, ValueError):
        return False
    for param in sig.parameters.values():
        if isinstance(param.default, FieldInfo) and param.default.json_schema_extra:
            return True
    return False


def _build_function_args_schema(func: Callable, title: str) -> Type[BaseModel]:
    """按 v1 语义从函数签名重建 args_schema。

    v1 的 ``tool`` 装饰器会把签名上的 ``FieldInfo`` 原样保留（含
    ``json_schema_extra`` 中的 ``choices`` 等，WebUI 依赖它渲染参数下拉框）；
    v2 的 ``tool`` 只透传 ``description``。此函数仅在检测到
    ``json_schema_extra`` 时由 ``regist_tool`` 调用，以 ``create_model``
    还原 v1 行为，其余工具仍走 v2 原生推断路径。
    """
    import typing as _typing

    sig = inspect.signature(func)
    hints = _typing.get_type_hints(func)
    fields: Dict[str, Any] = {}
    for name, param in sig.parameters.items():
        if name not in hints:
            continue
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        if isinstance(param.default, FieldInfo):
            fields[name] = (hints[name], param.default)
        elif param.default is inspect.Parameter.empty:
            fields[name] = (hints[name], ...)
        else:
            fields[name] = (hints[name], param.default)
    return create_model(title, **fields)


def regist_tool(
    *args: Any,
    title: str = "",
    description: str = "",
    return_direct: bool = False,
    args_schema: Optional[Type[BaseModel]] = None,
    infer_schema: bool = True,
) -> Union[Callable, BaseTool]:
    """
    wrapper of langchain tool decorator
    add tool to regstiry automatically
    """

    def _parse_tool(t: BaseTool):
        nonlocal description, title

        _TOOLS_REGISTRY[t.name] = t

        # change default description
        if not description:
            if t.func is not None:
                description = t.func.__doc__
            elif t.coroutine is not None:
                description = t.coroutine.__doc__
        t.description = " ".join(re.split(r"\n+\s*", description))
        # set a default title for human
        if not title:
            title = "".join([x.capitalize() for x in t.name.split("_")])
        # pydantic v2 禁止给模型实例赋值未声明字段；
        # 用 object.__setattr__ 保持 v1 时代 extra 属性（title）的语义
        object.__setattr__(t, "title", title)

    def wrapper(def_func: Callable) -> BaseTool:
        _args_schema = args_schema
        if (
            _args_schema is None
            and infer_schema
            and _func_fieldinfo_has_schema_extra(def_func)
        ):
            try:
                _args_schema = _build_function_args_schema(
                    def_func, def_func.__name__
                )
            except Exception:
                # 回退到 v2 原生推断路径
                _args_schema = None
        partial_ = tool(
            *args,
            # v2 的 tool() 要求显式 description 或函数 docstring；
            # v1 时 description 依赖 _parse_tool 事后覆盖，这里显式透传
            description=description or None,
            return_direct=return_direct,
            args_schema=_args_schema,
            infer_schema=infer_schema,
        )
        t = partial_(def_func)
        _parse_tool(t)
        return t

    if len(args) == 0:
        return wrapper
    else:
        t = tool(
            *args,
            description=description or None,
            return_direct=return_direct,
            args_schema=args_schema,
            infer_schema=infer_schema,
        )
        _parse_tool(t)
        return t


def _doc_page_content(doc: Any) -> str:
    """取出一条结果的正文。

    `docs` 里三种形状都出现过：`search_internet` 放的是 langchain 的 `Document`，
    `url_reader` 放的是裸 dict，`search_local_knowledgebase` 放的是 `DocumentWithVSId`。
    旧写法 `DocumentWithVSId.model_validate(doc)` 在 pydantic v2 下只认 dict 或
    `DocumentWithVSId` 本身，父类 `Document` 实例直接被拒
    （`Input should be a valid dictionary or instance of DocumentWithVSId`）——
    联网搜索明明搜到了内容，工具却整步 failed。这里本来也只需要正文。
    """
    if isinstance(doc, dict):
        return doc.get("page_content") or ""
    return getattr(doc, "page_content", None) or ""


def format_context(self: BaseToolOutput) -> str:
    '''
    将包含知识库输出的ToolOutput格式化为 LLM 需要的字符串
    '''
    context = ""
    docs = self.data.get("docs") or []
    source_documents = []

    for doc in docs:
        content = _doc_page_content(doc)
        if content.strip():
            source_documents.append(content)

    if len(source_documents) == 0:
        context = "没有找到相关文档,请更换关键词重试"
    else:
        for doc in source_documents:
            context += doc + "\n\n"

    return context
