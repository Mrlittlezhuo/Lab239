---
name: api-contract-first
description: 本项目的 API 契约先行流程。新功能开发时先定业务对象与状态取值，再写响应模型与桩路由并导出契约、生成前端类型，让前端不等后端、后端实现受契约约束。含三条命令、四道门、可照抄骨架与已知坑。仅当用户显式调用时加载，不自动触发。
disable-model-invocation: true
---

# API 契约先行（本项目专用）

## 顺序

```
功能设计 → 业务对象与状态取值 → API 契约骨架（先写） → 前端与后端并行 → 测试贯穿
```

契约不是后端的产物，是**先写的约定**；后端是"实现契约"。前端页面设计完，契约的一半已经出来了：
每个页面要显示什么、能点什么操作，就是接口的返回字段与操作。

## 五条硬事实（先记住，否则会踩）

1. **契约必须来自代码，不能手写。** `export_openapi.py --check` 会拿提交的契约与当前 app 导出的比对，手写的 JSON 直接让门红。
2. **只写模型、没有路由引用它，模型不会进契约。** OpenAPI 只收录被接口引用到的 schema。骨架阶段必须连路由一起写。
3. **契约链三段，都在仓库里：**
   `FastAPI app → tools/export_openapi.py → contracts/openapi.json → tools/gen_api_types.py → frontend/src/api/schema.d.ts`
   SSE 流式帧不在 OpenAPI 里，走独立一段：`contracts/sse-frames.json → tools/gen_sse_types.py → frontend/src/api/sse-frames.ts`
4. **响应模型必须继承 `ResponseModel`**（定义在 `libs/chatchat-server/chatchat/server/api_server/api_schemas.py`）。它把契约里所有字段标成 required。原因：pydantic 把"有默认值"标成"可能没有"，但 FastAPI 序列化响应时一定写出全部字段；标松了，后端删字段前端编译不会报错。**只改导出的 schema 元数据，零运行时行为变化。** 不要给请求模型用它。
5. **状态取值一处定义。** 数据库迁移里有 `CHECK (status IN (...))`，响应模型就必须写 `Literal[...]`，两边同一组值。查 CHECK：在 `libs/chatchat-server/chatchat/server/db/migrate/migrations.py` 里搜 `CHECK (`。没有 CHECK 的字段**不要凭猜收窄**，保持宽类型。

## 五步流程

**1. 定业务对象与状态**：对象有哪些、状态取值集合、状态之间怎么流转。取值集合现在就要定死。

**2. 页面设计 → 接口清单**：列表 / 详情 / 每个按钮对应的操作。

**3. 写契约骨架**（后端实现先不写）。在 `libs/chatchat-server/chatchat/server/api_server/` 下建模块路由：

```python
from typing import List, Literal, Optional
from pydantic import Field

class DefenseOut(ResponseModel):
    defense_id: str
    code: str
    # 与迁移里的 CHECK 同一组值，一处定义
    status: Literal["scheduled", "in_progress", "finished", "cancelled"]
    scheduled_at: Optional[str] = None      # 真的可能没有才写 Optional
    conclusions: List[ConclusionOut] = Field(default_factory=list)

@defense_router.get("/defenses/{defense_id}", response_model=DefenseOut)
async def get_defense(defense_id: str):
    # 桩：返回假数据让前端开工。后端实现时只替换函数体，模型不动。
    return DefenseOut(defense_id=defense_id, code="DEF-001", status="scheduled")
```

骨架规则：
- **嵌套字段也要声明模型**，不要写 `List[dict]` / `Dict[str, Any]`。那样契约里元素是空壳，生成的 TS 是 `{}`，前端接上等于丢信息。确实要保留开放结构（内容来自模型输出或用户自己写的 YAML）时，在提交说明里写理由。
- **桩返回的假数据必须满足模型**（pydantic 会校验）——写不出来就说明契约形状有问题，这是免费的自检。
- **二进制下载**要 `response_class=FileResponse` 并显式 `responses={200: {"content": {"application/octet-stream": {"schema": {"type":"string","format":"binary"}}}}}`，否则契约里是个空 object。
- **Python 关键字字段名**（`from`/`to`）写成 `from_value` + `Field(..., alias="from")` + `model_config = ConfigDict(populate_by_name=True)`。

**4. 导出契约、生成类型**（仓库根，用项目 Conda 环境的 python）：

```bash
python tools/export_openapi.py      # 契约 vs 当前 app，写 contracts/openapi.json
python tools/gen_api_types.py       # 契约 → frontend/src/api/schema.d.ts
python tools/gen_sse_types.py       # 仅当改了 SSE 帧
```

前端用法：

```ts
import type { components } from "@/api/schema";
export type Defense = components["schemas"]["DefenseOut"];
```

**5. 前后端并行**：前端用桩开工，后端把实现填进路由，**模型不动**。改字段就是改契约，必须重跑第 4 步，门会响。

## 四道门（改完必须全绿）

```bash
# 契约与类型一致性（不写文件，只比对）
python tools/export_openapi.py --check    # 期望 CONTRACT OK
python tools/gen_api_types.py --check     # 期望 TYPES OK
python tools/gen_sse_types.py --check     # 期望 SSE TYPES OK（改了 SSE 才需要）

# 契约门测试（必填性 / 无类型响应 / 无类型嵌套棘轮）
# 注意：pytest 前 CHATCHAT_ROOT 指向的目录必须先建数据目录，否则会假失败
$env:CHATCHAT_ROOT="<仓库根>\.tmp\pytest_root_d"
python -c "from chatchat.settings import Settings; Settings.basic_settings.make_dirs()"
python -m pytest libs/chatchat-server/tests/unit_tests/test_api_contract.py -q --rootdir libs/chatchat-server

# 前端
cd frontend && npm run typecheck && npx vitest run && npm run build
```

**棘轮门**：`test_api_contract.py` 里 `UNTYPED_NESTED_BASELINE = 49` 是"无类型嵌套字段"的历史欠账数。修一处就把常量调小一处；新增一处门直接红。已实测它会咬人（注入一个 `List[dict]` → 报"从 49 涨到 50"、exit 1）。

**门必须能红**：新加的门要主动注入一次漂移验证它会失败，再撤掉。空过的门没有价值。

## 已知坑

- **不要用 PowerShell `Get-Content`/`Set-Content` 改含中文的源文件**——会把中文打成乱码并吞掉引号，造成语法错误。用 Python 显式 UTF-8 读写，且读时 `newline=None`、写时 `newline="\n"`（`git checkout` 在 autocrlf 下给的是 CRLF，锚定行尾的正则会失配）。
- `make test` 的 `--disable-socket` 与 starlette `TestClient` 冲突，会整批假红；直接跑 `python -m pytest`。
- 生成的 `schema.d.ts` 不要手改，它是产物。
- 前端手抄类型接契约前，先逐字段核对：字段名与 required 相同**不够**，嵌套元素类型和枚举取值也要一致。接错了比不接更糟（把有形状的变成 `{}`）。

## 存量债（做之前先知道）

- `ResponseModel` 与 `Literal` 纪律目前**只覆盖 supervision 模块**。会话、记忆、服务器、消息历史还没迁。
- 无类型嵌套字段还剩 49 处，大头在文献模块（`SnapshotOut` 13、`RunOut` 9、`ConclusionOut` 5、`ResultOut` 5）。
- 完整缺口清单：`python tools/gen_api_types.py --report-gaps`（实测输出：全 API 还有 93 个接口没有可引用的响应模型，清单打到 stderr）。

## 交付

按 AGENTS.md 的 Git 交付：定向验证通过 → 只暂存本次任务文件 → 立即提交并推送。契约文件与生成的类型必须和后端改动**在同一个提交里**，否则门会红。
