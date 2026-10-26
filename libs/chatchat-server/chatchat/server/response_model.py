"""响应模型基类。

为什么单独一个模块：`api_schemas.py` 自己 import 了 `chatchat.server.utils`，而 `server/utils.py`、
`types/server/response/base.py` 属于更底层的模块，
所以 `server/utils.py`、`auth/schemas.py` 这类文件不能反过来从 `api_schemas`
取基类（会成环）。本模块只依赖 pydantic，任何层的响应模型都能引用它。
`api_schemas.py` 会 re-export 这个名字，已有的 `from ...api_schemas import
ResponseModel` 写法继续有效。
"""

from chatchat.server.pydantic_v2 import BaseModel


class ResponseModel(BaseModel):
    """响应模型基类：把 JSON Schema 的 `required` 补全为全部字段。

    为什么需要：pydantic 把「有默认值」的字段标成非 required。这对**请求**是对的
    （调用方可以不传），对**响应**是错的 —— FastAPI 序列化响应模型时会写出全部
    字段，缺省的用默认值填上（只有路由显式 `response_model_exclude_unset` /
    `exclude_none` 才会真的省略，本仓库没有这种路由）。

    后果不是文档瑕疵而是真实的类型漏洞：契约里把字段标成可选，前端由契约生成的
    类型就比运行时实际更松（`string` 变成 `string | null | undefined`）。把手抄
    类型换成这种生成类型，编译一定过，但前端拿到的保证被悄悄削弱了。

    这里只改**导出的 schema 元数据**，不改运行时校验与序列化行为：响应校验和
    字段输出与之前完全一致。
    """

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema, handler):
        json_schema = handler(core_schema)
        properties = json_schema.get("properties")
        if properties:
            json_schema["required"] = sorted(properties)
        return json_schema
