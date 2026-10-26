"""API 契约链离线测试（不导入 FastAPI app）。

钉住三件事：

1. `contracts/openapi.json` 存在、可解析，且关键 schema 还在（防止重新导出时
   意外丢掉路由/模型）；
2. `frontend/src/api/schema.d.ts` 与契约一致（防止有人手改生成物，或改了契约
   忘了重新生成类型）；
3. 前端确实有模块在消费生成类型（防止这条链退化成没人用的死代码）。

这里刻意不导入 `create_app()`：真实 app 导入要 9 秒以上且带进程级副作用，
现有单测也一致地避开它。「契约 vs 当前 app」的比对由
`python tools/export_openapi.py --check` 负责，不进单测会话。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
CONTRACT_PATH = REPO_ROOT / "contracts" / "openapi.json"
TYPES_PATH = REPO_ROOT / "frontend" / "src" / "api" / "schema.d.ts"
GENERATOR_PATH = REPO_ROOT / "tools" / "gen_api_types.py"

# SSE 帧契约链：contracts/sse-frames.json → tools/gen_sse_types.py
# → frontend/src/api/sse-frames.ts。这条链独立于 OpenAPI，因为 kb_chat/file_chat
# 的帧负载来自 OpenAIBaseOutput(extra="allow")，字段在 OpenAPI 里没有 schema。
SSE_CONTRACT_PATH = REPO_ROOT / "contracts" / "sse-frames.json"
SSE_TYPES_PATH = REPO_ROOT / "frontend" / "src" / "api" / "sse-frames.ts"
SSE_GENERATOR_PATH = REPO_ROOT / "tools" / "gen_sse_types.py"
# 必须消费 SSE 生成类型的前端文件；缺一个说明这条链接线又退回手写副本。
SSE_CONSUMERS = [
    "frontend/src/lib/sse/types.ts",
    "frontend/src/lib/sse/classify.ts",
    "frontend/src/components/chat/MemorySnapshotBadge.tsx",
]

# 前端已经在用的契约模型。删掉这些模型说明接口契约被破坏，必须显式改测试。
REQUIRED_SCHEMAS = [
    "ConversationOut",
    "ServerModelsOut",
    "PlatformModelsOut",
    "KBDefaultsOut",
    "MessageOut",
    "ConversationMessagesOut",
    "ConversationDeletedOut",
]

# 这些端点的 200 响应必须在契约里有具体模型。写成 `-> dict` 时 FastAPI 推不出
# response_model，契约里该响应就是空的，前端只能手抄类型 —— 后端改字段编译期
# 不会报错，只在运行时拿到 undefined。这里钉住，防止注解被改回去。
TYPED_RESPONSES = [
    ("get", "/conversations/{conversation_id}/messages"),
    ("delete", "/conversations/{conversation_id}"),
    ("get", "/server/models"),
]


def _load_generator():
    spec = importlib.util.spec_from_file_location("gen_api_types", GENERATOR_PATH)
    assert spec and spec.loader, f"生成器缺失: {GENERATOR_PATH}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def contract() -> dict:
    assert CONTRACT_PATH.exists(), (
        f"缺少契约文件 {CONTRACT_PATH}，请运行 python tools/export_openapi.py"
    )
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


def test_contract_is_parseable(contract: dict):
    assert contract.get("openapi", "").startswith("3."), "契约不是 OpenAPI 3.x"
    assert len(contract.get("paths", {})) > 100, "契约路由数异常偏低，疑似导出被截断"
    schemas = contract.get("components", {}).get("schemas", {})
    for name in REQUIRED_SCHEMAS:
        assert name in schemas, f"契约缺少前端依赖的模型: {name}"


def test_generated_types_match_contract(contract: dict):
    assert TYPES_PATH.exists(), (
        f"缺少 {TYPES_PATH}，请运行 python tools/gen_api_types.py"
    )
    gen = _load_generator()
    expected = gen.render(contract)
    actual = TYPES_PATH.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert actual == expected, (
        "frontend/src/api/schema.d.ts 与 contracts/openapi.json 不一致。"
        "生成物不要手改，请运行 python tools/gen_api_types.py 后一起提交。"
    )


def test_frontend_actually_consumes_generated_types():
    api_dir = REPO_ROOT / "frontend" / "src" / "api"
    consumers = [
        path
        for path in api_dir.glob("*.ts")
        if path.name != "schema.d.ts"
        and 'from "@/api/schema"' in path.read_text(encoding="utf-8")
    ]
    assert consumers, (
        "没有任何 api 模块引用 @/api/schema —— 契约链已退化为死代码"
    )


@pytest.mark.parametrize("method,path", TYPED_RESPONSES)
def test_endpoint_response_has_a_declared_model(contract: dict, method, path):
    op = contract.get("paths", {}).get(path, {}).get(method)
    assert op, f"契约里缺少 {method.upper()} {path}"
    schema = (
        op.get("responses", {})
        .get("200", {})
        .get("content", {})
        .get("application/json", {})
        .get("schema")
    )
    has_model = bool(schema) and ("$ref" in schema or schema.get("type") == "array")
    assert has_model, (
        f"{method.upper()} {path} 的 200 响应没有声明模型，退化成 dict 了：{schema!r}。"
        "这会让前端只能手抄类型，后端改字段时编译期不会报错。"
    )


def _load_sse_generator():
    spec = importlib.util.spec_from_file_location(
        "gen_sse_types", SSE_GENERATOR_PATH
    )
    assert spec and spec.loader, f"SSE 生成器缺失: {SSE_GENERATOR_PATH}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_sse_generated_types_match_contract():
    """SSE 生成物不得手改：必须等于 tools/gen_sse_types.py 对契约的渲染结果。"""
    assert SSE_CONTRACT_PATH.exists(), (
        f"缺少 {SSE_CONTRACT_PATH}；它被 .gitignore 的全局 *.json 影响，"
        "需要显式放行才能入库"
    )
    assert SSE_TYPES_PATH.exists(), (
        f"缺少 {SSE_TYPES_PATH}，请运行 python tools/gen_sse_types.py"
    )
    gen = _load_sse_generator()
    contract = json.loads(SSE_CONTRACT_PATH.read_text(encoding="utf-8"))
    expected = gen.render(contract)
    actual = SSE_TYPES_PATH.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert actual == expected, (
        "frontend/src/api/sse-frames.ts 与 contracts/sse-frames.json 不一致。"
        "生成物不要手改，请运行 python tools/gen_sse_types.py 后一起提交。"
    )


def test_frontend_actually_consumes_sse_frame_types():
    """防止 SSE 契约链接线退化成没人用的死代码（前端又回去手写枚举副本）。"""
    for rel in SSE_CONSUMERS:
        path = REPO_ROOT / rel
        assert path.exists(), f"前端文件缺失: {rel}"
        assert 'from "@/api/sse-frames"' in path.read_text(encoding="utf-8"), (
            f"{rel} 不再引用 @/api/sse-frames —— 它可能又手写了一份帧枚举副本，"
            "这样后端改状态机时前端不会有任何编译期信号"
        )


def _response_schema_names(contract: dict, prefix: str) -> set[str]:
    """取某类路径下所有 200 响应引用到的 schema 名。

    跟 $ref、数组元素、anyOf/oneOf/allOf 分支（`Optional[XxxOut]` 就是 anyOf），
    并递归进入被引用的模型，否则只覆盖到最外层会漏掉一大半。
    """
    schemas = contract.get("components", {}).get("schemas", {})
    names: set[str] = set()

    def walk(schema: dict):
        if not isinstance(schema, dict):
            return
        if "$ref" in schema:
            name = schema["$ref"].rsplit("/", 1)[-1]
            if name in names:
                return
            names.add(name)
            walk(schemas.get(name, {}))
            return
        if "items" in schema:
            walk(schema["items"])
        for key in ("anyOf", "oneOf", "allOf"):
            for branch in schema.get(key, []):
                walk(branch)
        for prop in schema.get("properties", {}).values():
            walk(prop)

    for path, item in contract["paths"].items():
        if not path.startswith(prefix):
            continue
        for operation in item.values():
            if not isinstance(operation, dict):
                continue
            content = operation.get("responses", {}).get("200", {}).get("content", {})
            for media in content.values():
                walk(media.get("schema", {}))
    return names


def test_all_responses_declare_every_field_required(contract: dict):
    """所有接口的响应模型必须把全部字段标成 required（不限 supervision）。

    FastAPI 序列化响应模型时会写出所有字段（本仓库没有用 response_model_exclude_unset /
    exclude_none 的路由），所以契约里把有默认值的字段标成可选，就比运行时更松：
    前端由契约生成的类型会多出 `| undefined`，后端删字段时编译期不会响。
    `chatchat.server.response_model.ResponseModel` 基类负责补全 required；谁把基类
    改回 BaseModel，或者新加一个没继承它的响应模型，这里会红。

    2026-10-26 从 /supervision 扩到全部路径：会话、记忆、会话上下文、服务器、
    认证/登录、MCP 状态，以及两个旧 chatchat 响应信封（BaseResponse/ListResponse）
    都补上基类后，67 个响应模型零例外。
    """
    schemas = contract["components"]["schemas"]
    checked, problems = 0, []
    for name in sorted(_response_schema_names(contract, "")):
        schema = schemas.get(name)
        if not isinstance(schema, dict) or "properties" not in schema:
            continue
        checked += 1
        missing = set(schema["properties"]) - set(schema.get("required", []))
        if missing:
            problems.append(f"{name}: 字段 {sorted(missing)} 没进 required")
    assert checked >= 60, f"只检查到 {checked} 个响应模型，疑似路径或引用取错"
    assert not problems, (
        "响应契约比运行时更松（让前端类型被削弱）。"
        "请让这些模型继承 chatchat.server.response_model.ResponseModel：\n  "
        + "\n  ".join(problems)
    )


def test_supervision_responses_are_not_untyped(contract: dict):
    """supervision 的 200 响应必须在契约里有类型。

    路由返回裸 dict（不写 response_model）时，FastAPI 生成的响应是空 object，
    契约里看不出任何字段，前端只能手抄类型 —— 后端改字段时编译期不会响。
    二进制下载用 response_class=FileResponse + binary schema，也算有类型。
    """

    def typed(schema) -> bool:
        if not isinstance(schema, dict):
            return False
        if "$ref" in schema:
            return True
        if schema.get("type") in {"string", "number", "integer", "boolean"}:
            return True
        if schema.get("properties"):
            return True
        if schema.get("type") == "array":
            return typed(schema.get("items"))
        for key in ("anyOf", "oneOf", "allOf"):
            if key in schema and any(typed(branch) for branch in schema[key]):
                return True
        return False

    checked, untyped = 0, []
    for path, item in contract["paths"].items():
        if not path.startswith("/supervision"):
            continue
        for method, operation in item.items():
            if not isinstance(operation, dict):
                continue
            content = operation.get("responses", {}).get("200", {}).get("content")
            if not content:
                continue
            checked += 1
            for media_type, media in content.items():
                if not typed(media.get("schema")):
                    untyped.append(f"{method.upper()} {path}  ({media_type})")
    assert checked >= 100, f"只检查到 {checked} 个端点，疑似路径或响应取错"
    assert not untyped, (
        "这些 supervision 端点的 200 响应在契约里没有类型（多半是路由返回裸 dict）。"
        "前端只能手抄类型，后端改字段时编译期不会报错：\n  "
        + "\n  ".join(sorted(untyped))
    )


# 棘轮基线，2026-10-26 实测。这些字段声明成 List[dict] / Dict[str, Any]，契约里
# 元素是 additionalProperties 的空壳，生成的 TS 是 `{}`：前端接上契约等于把有形状的
# 东西变成没形状，只能继续手抄。修一处就把这个数调小一处，新增一处这道门直接红。
# 确实要保留开放结构时（内容来自模型输出或用户自己写的 YAML，写死会拒绝合法数据），
# 在提交说明里写清理由再调基数。
UNTYPED_NESTED_BASELINE = 49


def test_supervision_untyped_nested_fields_do_not_grow(contract: dict):
    """supervision 响应模型里的无类型嵌套字段只准减少、不准增加（棘轮）。

    无类型 = 对象只有 additionalProperties，或数组元素是这种空壳对象。
    `Optional[Dict[str, Any]]`（anyOf: null + 空壳）同样算无类型。
    """

    schemas = contract["components"]["schemas"]

    def typed(schema) -> bool:
        if not isinstance(schema, dict):
            return False
        if "$ref" in schema:
            return True
        if schema.get("type") in {"string", "number", "integer", "boolean"}:
            return True
        if schema.get("properties"):
            return True
        if schema.get("type") == "array":
            return typed(schema.get("items"))
        for key in ("anyOf", "oneOf", "allOf"):
            if key in schema and any(typed(branch) for branch in schema[key]):
                return True
        return False

    untyped: list[str] = []
    seen: set[str] = set()

    def walk_model(name: str) -> None:
        if name in seen or name not in schemas:
            return
        seen.add(name)
        for field, prop in schemas[name].get("properties", {}).items():
            if not typed(prop):
                untyped.append(f"{name}.{field}")
            target = prop
            while isinstance(target, dict) and "$ref" in target:
                target = schemas.get(target["$ref"].rsplit("/", 1)[-1], {})
            if not isinstance(target, dict):
                continue
            items = target.get("items", {})
            if target.get("type") == "array" and isinstance(items, dict) and "$ref" in items:
                walk_model(items["$ref"].rsplit("/", 1)[-1])
            elif "$ref" in target:
                walk_model(target["$ref"].rsplit("/", 1)[-1])

    names = _response_schema_names(contract, "/supervision")
    for name in sorted(names):
        walk_model(name)

    assert len(seen) >= 40, f"只覆盖到 {len(seen)} 个响应模型，疑似遍历漏了"
    assert len(untyped) <= UNTYPED_NESTED_BASELINE, (
        f"无类型嵌套字段从 {UNTYPED_NESTED_BASELINE} 涨到 {len(untyped)} 个。"
        "新加的 List[dict] / Dict[str, Any] 会让前端拿到空壳类型，请声明成模型；"
        "确实要保留开放结构就在提交说明里写明理由，并把基线常量调大："
        + "\n  ".join(sorted(set(untyped)))
    )
