# -*- coding: utf-8 -*-
"""pydantic v1 风格 API 的兼容层（基于 pydantic v2 实现）。

任务 013（LangChain 1.x / LangGraph 迁移基线）升级后，项目代码统一运行在
pydantic v2 之上。本模块按任务文档 §8.4 作为“小型集中式兼容适配器”，为仍按
v1 API 编写的既有业务代码提供等价语义，避免大面积改写：

- ``BaseModel`` / ``SecretStr`` / ``FieldInfo`` / ``create_model`` 直接提供
  pydantic v2 的实现；
- ``Field`` 在 v2 ``Field`` 基础上额外接受 v1 风格 ``required=`` 关键字参数；
- ``root_validator(pre=True)`` 映射为 ``model_validator(mode='before')``；
- ``root_validator()``（post）通过 ``model_validator(mode='wrap')`` 模拟 v1
  “接收/返回已校验字段 dict”的语义；写回时仅更新模型已定义的字段，未定义
  字段丢弃（与 v1 ``extra != allow`` 行为一致）；
- ``Extra`` 仅为 v1 ``class Config`` 写法保留的兼容符号；
- ``model_schema`` 通过 ``pydantic.model_json_schema`` 提供（与 v1
  ``pydantic.schema.model_schema`` 对应）；
- ``typing`` 为标准库 ``typing`` 模块引用。

注意：v2 ``BaseModel`` 会忽略类内 ``class Config`` 写法。个别类在 v1 环境
依赖 ``allow_population_by_field_name=True`` 的，需要在本任务中显式改为
``model_config = ConfigDict(populate_by_name=True)``（见 zhipuai /
localai_embeddings）。
"""
import typing  # noqa: F401  (兼容 ``from chatchat.server.pydantic_v1 import typing``)
from typing import Any, Callable

from pydantic import (
    BaseModel,  # noqa: F401  (v2 原生)
    Field as _V2Field,
    SecretStr,  # noqa: F401  (v2 原生，提供 get_secret_value)
    create_model,  # noqa: F401  (v2 原生，接受 v2 FieldInfo)
    model_validator,
)
from pydantic.fields import FieldInfo  # noqa: F401  (v2 原生)
from pydantic.json_schema import model_json_schema

__all__ = [
    "BaseModel",
    "Extra",
    "Field",
    "FieldInfo",
    "SecretStr",
    "create_model",
    "model_schema",
    "root_validator",
    "typing",
]


class Extra:
    """v1 ``Extra`` 的兼容符号（仅用于 v1 风格 ``class Config``）。"""

    allow = "allow"
    ignore = "ignore"
    forbid = "forbid"


def Field(default: Any = ..., **kwargs: Any) -> "FieldInfo":
    """pydantic v2 ``Field`` 的 v1 兼容包装。

    额外接受 v1 风格的 ``required=`` 关键字：
    ``Field(None, required=False, ...)`` 等价于 v2 的 ``Field(default=None, ...)``；
    ``Field(..., required=True)`` 等价于 v2 的必填字段 ``Field(...)``。

    额外接受 v1 风格的 ``choices=`` 关键字：v1 中该参数会并入字段的 JSON
    Schema；v2 ``Field`` 对未知关键字只告警。这里显式合并进
    ``json_schema_extra``，保持 schema 输出与 v1 一致且无弃用告警。
    """
    required = kwargs.pop("required", None)
    if required is False and default is ...:
        default = None
    choices = kwargs.pop("choices", None)
    if choices is not None:
        merged = dict(kwargs.get("json_schema_extra") or {})
        merged.setdefault("choices", list(choices))
        kwargs["json_schema_extra"] = merged
    return _V2Field(default, **kwargs)


def model_schema(*args: Any, **kwargs: Any) -> Any:
    """v1 ``pydantic.schema.model_schema`` 的兼容入口。"""
    return model_json_schema(*args, **kwargs)


def root_validator(
    pre: bool = False,
    *,
    allow_reuse: bool = False,  # noqa: ARG001  (v1 关键字，仅接受不生效)
    skip_on_failure: bool = False,  # noqa: ARG001  (v1 关键字，仅接受不生效)
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """v1 ``root_validator`` 的 v2 兼容入口。

    - ``pre=True``：函数接收原始输入（dict），等价 ``mode='before'``；
    - ``pre=False``：先完成字段校验得到模型实例，再把实例字段值转成 dict
      调用原函数（签名 ``(cls, values)``），将返回 dict 中属于已定义字段的
      值写回实例，模拟 v1 post 阶段“dict 进、dict 出”的语义。
    """
    if pre:

        def decorator_pre(func: Callable[..., Any]) -> Callable[..., Any]:
            @model_validator(mode="before")
            @classmethod
            def _before(cls: Any, values: Any) -> Any:
                return func(cls, values)

            return _before

        return decorator_pre

    def decorator_post(func: Callable[..., Any]) -> Callable[..., Any]:
        @model_validator(mode="wrap")
        @classmethod
        def _wrap(cls: Any, values: Any, handler: Callable[..., Any], info: Any = None) -> Any:
            model = handler(values)
            values_dict = dict(model.__dict__)
            result = func(cls, values_dict)
            if isinstance(result, dict):
                defined = set(cls.model_fields)
                for key, value in result.items():
                    if key in defined:
                        setattr(model, key, value)
            return model

        return _wrap

    return decorator_post
