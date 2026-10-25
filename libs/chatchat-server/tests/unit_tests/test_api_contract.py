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
