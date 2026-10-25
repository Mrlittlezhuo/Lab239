# API 契约链

后端 FastAPI 已经完整描述了所有接口的请求/响应模型（`api_schemas.py` + 各
`*_routes.py` 的 `response_model`）。这条链把那份描述固化成**可提交、可 diff、
可被前端编译期消费**的产物，用来消灭「后端改字段、前端手写 interface 静默失配」。

## 链路

```
FastAPI app ──export_openapi.py──▶ contracts/openapi.json ──gen_api_types.py──▶ frontend/src/api/schema.d.ts
                                                                                        │
                                                                        前端 api 模块 import type
                                                                                        │
                                                                        tsc 编译期报错（漂移即现形）
```

## 三条命令

```bash
# 1. 从当前后端导出契约（需要项目 Conda 环境，导入 app 约 9 秒）
python tools/export_openapi.py

# 2. 由契约生成前端类型
python tools/gen_api_types.py

# 3. 防漂移校验（给 CI 用，不一致退出码 1）
python tools/export_openapi.py --check     # 契约 vs 当前 app
python tools/gen_api_types.py --check      # 类型 vs 契约
```

两个 `--check` 都不写文件，只做比对。`libs/chatchat-server/tests/unit_tests/test_api_contract.py`
把第 2 组校验纳入了 pytest（不导入 app，纯文件比对）。

## 前端怎么用

```ts
import type { components } from "@/api/schema";

export type Conversation = components["schemas"]["ConversationOut"];
```

已接线的示范：`frontend/src/api/conversations.ts` 的 `Conversation`。

**实测有效性**：把契约里 `ConversationOut.temporary` 改名成 `is_temporary` 后，
`npm run typecheck` 立刻在 `src/components/chat/ConversationSidebar.tsx:126` 报
`TS2551: Property 'temporary' does not exist ... Did you mean 'is_temporary'?`。
这正是手写 interface 一直在藏的那类问题。

## 为什么不用 openapi-typescript

契约本来就是 Python 生成的，类型生成放在同一侧：CI 上只依赖 Python，不需要额外
Node 工具链。生成的形状与 openapi-typescript 一致（`components["schemas"]["Name"]`），
将来换成官方生成器，调用方代码不用改。

## 已知边界（重要）

### 1. SSE 流式接口的帧结构不在 OpenAPI 里

FastAPI 对 `StreamingResponse` 只能给出空响应模式。`POST /chat/kb_chat`、
`POST /chat/file_chat`、`POST /chat/chat/completions` 这类接口的**帧协议**仍然
手写在 `frontend/src/lib/sse/types.ts` + `frame-parser.ts`，不受本契约保护。
要覆盖它需要单独定义帧 schema（建议 `contracts/sse-frames.schema.json`）。

### 2. 98 个接口没有可引用的响应模型

这些接口返回的是无类型 `object`，**契约保护不到**。补齐办法是在路由上声明
`response_model=`。按模块分布：

| 模块 | 数量 |
|---|---|
| `research` | 39 |
| `v1` | 15 |
| `memories` | 10 |
| `conversations` | 7 |
| `supervision` | 6 |
| `server` | 5 |
| `knowledge_base` | 5 |
| `agent-runs` | 4 |
| `chat` | 4 |
| `admin` / `other` / `auth` | 各 1 |

完整清单：`python tools/gen_api_types.py --report-gaps`。

对比：258 个 operation 里 257 个有 200 响应声明，但其中 98 个的声明是空
`object`。优先补前端正在调用的那批（`GET /conversations/{id}/messages`、
`GET /server/models`、`GET /memories/*`）。

### 3. `.gitignore` 里有全局 `*.json`

仓库根 `.gitignore` 第 2 行是 `*.json`，所以契约文件靠
`!contracts/openapi.json` 显式放行。新增其它 `.json` 产物时要记得同样处理。

## 生成物说明

| 文件 | 大小 | 说明 |
|---|---|---|
| `contracts/openapi.json` | ~685 KB | 键已排序、LF、UTF-8，diff 只反映真实契约变化 |
| `frontend/src/api/schema.d.ts` | ~53 KB | 182 个 schema；**不要手改**，改了会被 `--check` 和 pytest 拦下 |
