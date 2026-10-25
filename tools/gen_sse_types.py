#!/usr/bin/env python3
"""从 contracts/sse-frames.json 生成前端可消费的 SSE 帧类型。

用法：
    python tools/gen_sse_types.py            # 写入 frontend/src/api/sse-frames.ts
    python tools/gen_sse_types.py --check    # 只校验，不一致退出码 1（给 CI 用）

为什么需要它：/chat/kb_chat 与 /chat/file_chat 的每帧负载由 OpenAIChatOutput 产生，
而 OpenAIBaseOutput 配了 extra="allow"、model_dump 把 model_extra 摊平进负载，
所以这些字段在 OpenAPI 里没有 schema，gen_api_types.py 覆盖不到。SSE 帧的规格
只存在于 contracts/sse-frames.json（键集由后端测试从真实流里采集并钉住），
本工具把其中的枚举与键集变成前端类型，避免前端再手写副本。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "contracts" / "sse-frames.json"
OUTPUT = ROOT / "frontend" / "src" / "api" / "sse-frames.ts"

# semantics 里不是枚举的说明性字段
_NON_ENUM_KEYS = {"note", "authoritative_source", "discriminator"}


def pascal(key: str) -> str:
    parts = [p for p in key.split("_") if p]
    return "".join(p[:1].upper() + p[1:] for p in parts)


def upper(key: str) -> str:
    return "_".join(p for p in key.split("_") if p).upper()


def render(contract: dict) -> str:
    frames = contract["frames"]
    lines: list[str] = [
        "/**",
        " * 由 tools/gen_sse_types.py 从 contracts/sse-frames.json 生成，请勿手改。",
        " * 改帧字段请先改契约（后端 test_sse_frame_vocabulary_matches_contract 会",
        " * 用真实流式运行核对键集），再重新生成本文件。",
        " *",
        " * 覆盖范围：/chat/kb_chat 与 /chat/file_chat 的帧。smart_conversation 与",
        " * agent_run 链路的帧尚未纳入，见契约 open_gaps。",
        " */",
        "",
    ]

    frame_names = [f["name"] for f in frames]
    lines.append("/** SSE 帧种类，判别依据是契约 transport.discriminator。 */")
    lines.append(
        "export type SseFrameName = "
        + " | ".join(f'"{n}"' for n in frame_names)
        + ";"
    )
    lines.append("")

    # 每个帧的 semantics 里的字符串数组都生成「类型 + 常量」两份
    seen_enums: set[str] = set()
    enums: dict[str, tuple[str, list[str]]] = {}
    for frame in frames:
        semantics = frame.get("semantics", {})
        for key, values in semantics.items():
            if key in _NON_ENUM_KEYS or not isinstance(values, list):
                continue
            if not all(isinstance(v, str) for v in values):
                continue
            if key in seen_enums:
                continue
            seen_enums.add(key)
            enums[key] = (pascal(key), values, frame["name"])

    def const_element_type(key: str) -> str:
        """取值是另一个枚举真子集时，常量按超集标注。

        否则 `INCOMPLETE_STATUSES.includes(generation_status)` 会因为数组元素类型
        比实参窄而编译失败；窄类型仍然单独导出，需要严格性的人可以用它。
        """
        values = enums[key][1]
        for other_key, (_, other_values, _) in enums.items():
            if other_key != key and set(values) < set(other_values):
                return enums[other_key][0]
        return enums[key][0]

    for key, (type_name, values, frame_name) in enums.items():
        lines.append(f"/** 来自契约 frames[{frame_name}].semantics.{key}。 */")
        lines.append(
            f"export type {type_name} = "
            + " | ".join(f'"{v}"' for v in values)
            + ";"
        )
        lines.append(
            f"export const {upper(key)}: readonly {const_element_type(key)}[] = "
            + "[" + ", ".join(f'"{v}"' for v in values) + "] as const;"
        )
        lines.append("")

    common = contract["common_keys"]
    lines.append("/** 每一帧都带的公共键（含未声明值类型的 extra 字段）。 */")
    lines.append(
        "export const SSE_COMMON_KEYS = ["
        + ", ".join(f'"{k}"' for k in common)
        + "] as const;"
    )
    lines.append("")
    lines.append("/** 每种帧除公共键外额外带的键。 */")
    lines.append("export const SSE_FRAME_EXTRA_KEYS = {")
    for frame in frames:
        keys = frame["additional_keys"]
        lines.append(
            f'  "{frame["name"]}": ['
            + ", ".join(f'"{k}"' for k in keys)
            + "] as const,"
        )
    lines.append("} as const;")
    lines.append("")
    lines.append("/** 契约声明的全部帧键（公共 + 额外），用于运行时核对未知键。 */")
    lines.append("export const SSE_KNOWN_KEYS: readonly string[] = [")
    lines.append("  ...SSE_COMMON_KEYS,")
    for frame in frames:
        lines.append(f"  ...SSE_FRAME_EXTRA_KEYS[\"{frame['name']}\"],")
    lines.append("];")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="不写文件；不一致则退出码 1")
    args = parser.parse_args()

    if not CONTRACT.exists():
        print(f"缺少 SSE 帧契约：{CONTRACT}", file=sys.stderr)
        return 1

    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    rendered = render(contract)

    if args.check:
        if not OUTPUT.exists():
            print(f"SSE 生成类型缺失：{OUTPUT}", file=sys.stderr)
            return 1
        current = OUTPUT.read_text(encoding="utf-8")
        if current != rendered:
            print(
                f"SSE TYPES DRIFT: {OUTPUT} 与 contracts/sse-frames.json 不一致，"
                "请运行 python tools/gen_sse_types.py",
                file=sys.stderr,
            )
            return 1
        print(
            f"SSE TYPES OK: {OUTPUT} 与契约一致 | frames={len(contract['frames'])}"
            f" common_keys={len(contract['common_keys'])}"
        )
        return 0

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(rendered, encoding="utf-8")
    print(
        f"WROTE {OUTPUT} | frames={len(contract['frames'])}"
        f" common_keys={len(contract['common_keys'])}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
