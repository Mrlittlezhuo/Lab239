#!/usr/bin/env python3
"""导出后端 OpenAPI 契约到 `contracts/openapi.json`。

后端 FastAPI 在 `create_app()` 里已经描述了全部接口的请求/响应模型
（`api_schemas.py` + 各 `*_routes.py` 的 `response_model`）。这份脚本把那
份描述固化成一个可提交、可 diff、可被前端消费的契约文件，用来消灭
「后端改字段、前端手写 interface 静默失配」这一类问题。

用法
----
    python tools/export_openapi.py            # 生成/覆盖 contracts/openapi.json
    python tools/export_openapi.py --check    # 只校验，不一致退出码 1（给 CI 用）

数据目录说明
------------
`create_app()` 会挂载 `StaticFiles`，要求 `MEDIA_PATH` / `IMG_DIR` 已存在；
正常启动时这些目录由 `chatchat cli` 的 `make_dirs()` 创建。为了不读取也不
写入真实的 `chatchat-study-data`，这里把 `CHATCHAT_ROOT` 指向一个专用 scratch
目录，只用于满足挂载点存在性检查。可用 `CHATCHAT_OPENAPI_ROOT` 覆盖位置。

已知边界
--------
OpenAPI 只覆盖 JSON 请求/响应。**SSE 流式接口的帧结构不在 OpenAPI 里**
（FastAPI 对 `StreamingResponse` 只能给出空响应模式），那部分契约仍然手写
在 `frontend/src/lib/sse/types.ts`，见 `contracts/README.md`。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONTRACT_PATH = REPO_ROOT / "contracts" / "openapi.json"
DEFAULT_SCRATCH_ROOT = Path(tempfile.gettempdir()) / "chatchat_openapi_root"


def scratch_root() -> Path:
    """契约导出专用的 CHATCHAT_ROOT，绝不指向真实运行数据目录。"""
    env = os.environ.get("CHATCHAT_OPENAPI_ROOT")
    if env:
        return Path(env).resolve()

    root = DEFAULT_SCRATCH_ROOT
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".write_probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return root
    except OSError:
        # 系统临时目录不可写（例如受限沙箱）：退回仓库内 .tmp/，该目录已 gitignore。
        return (REPO_ROOT / ".tmp" / "openapi_contract_root").resolve()


def build_spec() -> dict:
    """导入真实 app 并返回 OpenAPI 文档。必须在导入 chatchat 之前设好 CHATCHAT_ROOT。"""
    root = scratch_root()
    root.mkdir(parents=True, exist_ok=True)
    os.environ["CHATCHAT_ROOT"] = str(root)

    from chatchat.settings import Settings

    # 只作用于 scratch 根，用于满足 create_app 的 StaticFiles 目录存在性检查。
    Settings.basic_settings.make_dirs()

    from chatchat.server.api_server.server_app import app

    return app.openapi()


def render(spec: dict) -> str:
    """稳定序列化：排序键 + 固定缩进 + LF，保证 diff 只反映真实契约变化。"""
    return json.dumps(spec, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def summarize(spec: dict) -> str:
    paths = spec.get("paths", {})
    operations = sum(1 for item in paths.values() for _ in item)
    schemas = spec.get("components", {}).get("schemas", {})
    return (
        f"openapi {spec.get('openapi', '?')} | "
        f"title={spec.get('info', {}).get('title', '?')} | "
        f"version={spec.get('info', {}).get('version', '?')} | "
        f"paths={len(paths)} operations={operations} schemas={len(schemas)}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="不写文件；若已提交契约与当前 app 不一致则退出码 1",
    )
    parser.add_argument(
        "--out",
        default=str(CONTRACT_PATH),
        help=f"输出路径，默认 {CONTRACT_PATH}",
    )
    args = parser.parse_args()
    out_path = Path(args.out).resolve()

    spec = build_spec()
    rendered = render(spec)

    if args.check:
        if not out_path.exists():
            print(f"CONTRACT MISSING: {out_path} 不存在，请先运行 export_openapi.py", file=sys.stderr)
            return 1
        current = out_path.read_text(encoding="utf-8")
        if current.replace("\r\n", "\n") != rendered:
            print(
                "CONTRACT DRIFT: 后端接口描述已变化，但 contracts/openapi.json 未更新。\n"
                f"  运行: python tools/export_openapi.py 然后把 {out_path.name} 一起提交。\n"
                f"  当前 app: {summarize(spec)}",
                file=sys.stderr,
            )
            return 1
        print(f"CONTRACT OK: {out_path} 与当前 app 一致 | {summarize(spec)}")
        return 0

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(rendered)
    print(f"WROTE {out_path} | {summarize(spec)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
