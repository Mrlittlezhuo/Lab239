#!/usr/bin/env python3
"""从 `contracts/openapi.json` 生成前端可消费的 TS 类型 `frontend/src/api/schema.d.ts`。

为什么不用 openapi-typescript
-----------------------------
契约本来就是 Python 生成的，类型生成放在同一侧：CI 上只依赖 Python，不需要额外
Node 工具链。生成的形状与 openapi-typescript 一致（`components["schemas"]["Name"]`），
将来若要换成官方生成器，调用方代码不用改。

用法
----
    python tools/gen_api_types.py            # 生成/覆盖
    python tools/gen_api_types.py --check    # 只校验，不一致退出码 1（给 CI 用）
    python tools/gen_api_types.py --report-gaps   # 打印没有响应模型的接口清单
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONTRACT_PATH = REPO_ROOT / "contracts" / "openapi.json"
OUTPUT_PATH = REPO_ROOT / "frontend" / "src" / "api" / "schema.d.ts"

PRIMITIVES = {
    "string": "string",
    "number": "number",
    "integer": "number",
    "boolean": "boolean",
    "null": "null",
}


def needs_parens(expr: str) -> bool:
    """联合/交叉类型嵌进数组或对象时要加括号。"""
    return "|" in expr or "&" in expr


class Generator:
    """递归地把 JSON Schema 转成 TS 类型文本，`indent` 控制缩进层级。"""

    def __init__(self) -> None:
        self.local_names: set[str] = set()

    def ts(self, schema, indent: int) -> str:
        pad = "  " * indent
        if schema is None or schema is True or not isinstance(schema, dict):
            return "unknown"
        if schema is False:
            return "never"

        if "$ref" in schema:
            return f'components["schemas"][{json.dumps(schema["$ref"].split("/")[-1])}]'

        if "const" in schema:
            return json.dumps(schema["const"])

        if "enum" in schema:
            values = schema["enum"]
            if not values:
                return "never"
            return " | ".join("null" if v is None else json.dumps(v) for v in values)

        for key in ("allOf", "anyOf", "oneOf"):
            if key in schema:
                members = [self.ts(m, indent) for m in schema[key]]
                if not members:
                    return "unknown"
                joiner = " & " if key == "allOf" else " | "
                if len(members) == 1:
                    return members[0]
                return joiner.join(members)

        types = schema.get("type")
        if isinstance(types, list):
            return " | ".join(self.ts({**schema, "type": t}, indent) for t in types)

        if types == "array":
            item = self.ts(schema.get("items"), indent)
            return f"Array<{item}>" if needs_parens(item) else f"{item}[]"

        if types == "object" or "properties" in schema or "additionalProperties" in schema:
            return self.object_type(schema, indent)

        if types in PRIMITIVES:
            return PRIMITIVES[types]

        return "unknown"

    def object_type(self, schema, indent: int) -> str:
        pad = "  " * indent
        inner_pad = "  " * (indent + 1)
        props = schema.get("properties") or {}
        required = set(schema.get("required") or [])

        entries: list[str] = []
        for name, sub in props.items():
            key = name if name.isidentifier() else json.dumps(name)
            optional = "" if name in required else "?"
            entries.append(f"{inner_pad}{key}{optional}: {self.ts(sub, indent + 1)};")

        extra = schema.get("additionalProperties")
        if extra is True:
            entries.append(f"{inner_pad}[key: string]: unknown;")
        elif isinstance(extra, dict):
            entries.append(f"{inner_pad}[key: string]: {self.ts(extra, indent + 1)};")

        if not entries:
            return "{ [key: string]: unknown }"
        return "{\n" + "\n".join(entries) + "\n" + pad + "}"


HEADER = """/**
 * 自动生成 —— 不要手工编辑。
 *
 * 来源: contracts/openapi.json（`python tools/export_openapi.py` 从 FastAPI 导出）
 * 生成: python tools/gen_api_types.py
 *
 * 用法:
 *     import type {{ components }} from "@/api/schema";
 *     export type Conversation = components["schemas"]["ConversationOut"];
 *
 * 已知边界: SSE 流式接口的帧结构不在 OpenAPI 里，见 contracts/README.md。
 */

export interface components {
  schemas: {
"""

FOOTER = """  };
}

export type Schema<N extends keyof components["schemas"]> = components["schemas"][N];
"""


def render(spec: dict) -> str:
    schemas = spec.get("components", {}).get("schemas", {}) or {}
    gen = Generator()
    lines = [HEADER.rstrip("\n")]
    for name in sorted(schemas):
        key = name if name.isidentifier() else json.dumps(name)
        body = gen.ts(schemas[name], 2)
        if body.startswith("{"):
            lines.append(f"    {key}: {body};")
        else:
            lines.append(f"    {key}: {body};")
    lines.append(FOOTER.rstrip("\n"))
    return "\n".join(lines) + "\n"


def missing_response_models(spec: dict) -> list[str]:
    """列出没有可引用响应模型的接口 —— 这些接口无法被契约保护。"""
    gaps: list[str] = []
    for path, item in sorted(spec.get("paths", {}).items()):
        for method, op in item.items():
            resp = (op.get("responses") or {}).get("200") or {}
            content = resp.get("content") or {}
            typed = any(
                sch and ("$ref" in sch or sch.get("properties") or sch.get("items"))
                for sch in (media.get("schema") for media in content.values())
            )
            if not typed:
                gaps.append(f"{method.upper()} {path}")
    return gaps


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="不写文件；不一致则退出码 1")
    parser.add_argument("--contract", default=str(CONTRACT_PATH))
    parser.add_argument("--out", default=str(OUTPUT_PATH))
    parser.add_argument("--stdout", action="store_true", help="输出到标准输出（沙箱受限时代替直接写文件）")
    parser.add_argument("--report-gaps", action="store_true", help="打印缺少响应模型的接口清单")
    args = parser.parse_args()

    contract_path = Path(args.contract).resolve()
    out_path = Path(args.out).resolve()

    if not contract_path.exists():
        print(f"CONTRACT MISSING: {contract_path} 不存在，先运行 tools/export_openapi.py", file=sys.stderr)
        return 1

    spec = json.loads(contract_path.read_text(encoding="utf-8"))
    rendered = render(spec)

    if args.report_gaps:
        gaps = missing_response_models(spec)
        print(f"缺少可引用响应模型的接口: {len(gaps)}", file=sys.stderr)
        for gap in gaps:
            print(f"  {gap}", file=sys.stderr)

    if args.stdout:
        sys.stdout.write(rendered)
        return 0

    if args.check:
        if not out_path.exists():
            print(f"TYPES MISSING: {out_path} 不存在，先运行 gen_api_types.py", file=sys.stderr)
            return 1
        current = out_path.read_text(encoding="utf-8")
        if current.replace("\r\n", "\n") != rendered:
            print(
                f"TYPES DRIFT: {out_path.name} 与 contracts/openapi.json 不一致，"
                "请运行 python tools/gen_api_types.py 并提交。",
                file=sys.stderr,
            )
            return 1
        print(f"TYPES OK: {out_path} 与契约一致")
        return 0

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(rendered)
    schemas = spec.get("components", {}).get("schemas", {}) or {}
    print(f"WROTE {out_path} | schemas={len(schemas)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
