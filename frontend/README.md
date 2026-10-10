# Chatchat 前端（React SPA）

替换旧 Streamlit webui 的单页应用。所有数据都走后端 FastAPI HTTP/SSE 接口，
构建产物由后端同源托管，因此默认不需要额外的 Web 服务器，也不涉及 CORS。

## 技术栈

- React 18 + Vite 5 + TypeScript（strict）
- Tailwind CSS 3 + vendored shadcn/ui 组件（`src/components/ui`，已提交进仓库，
  不依赖 shadcn CLI 再拉取）
- react-router 6（history 模式）、zustand（auth / 聊天偏好 / 主题，localStorage 持久化）、
  @tanstack/react-query 5（服务端状态）
- SSE 自行解析（`src/lib/sse`，fetch + ReadableStream，不用第三方 SSE 库）
- vitest + Testing Library

明确不使用：axios、openai JS SDK（会藏掉必须自行分类的 SSE 帧）、
rehype-raw（模型输出渲染 HTML 会造成 XSS）。

## 本地开发

```bash
cd frontend
npm install --registry=https://registry.npmmirror.com   # 直连可去掉 --registry
npm run dev
```

- 后端默认在 `127.0.0.1:7861` 运行（`chatchat start -a`），Vite 已把
  `/auth`、`/chat`、`/knowledge_base` 等 API 前缀代理过去，且不缓冲 SSE。
  换地址用 `VITE_PROXY_TARGET=http://127.0.0.1:8000 npm run dev`。
- 登录账号即后端用户（首次部署用 CLI 创建的管理员）。

## 构建与部署

```bash
npm run build      # 产物在 frontend/dist
```

后端启动时若找到 `frontend/dist/index.html`，会把整个 SPA 挂到 `/`：

- 浏览器访问 API 地址（默认 `http://127.0.0.1:7861/`）即是 Web UI；
- `/docs`、`/redoc`、`/openapi.json`、`/media`、`/img` 行为不变；
- 没有构建产物时保持旧行为：`/` 重定向到 `/docs`。

相关后端环境变量：

| 变量 | 作用 | 默认 |
|---|---|---|
| `CHATCHAT_WEB_DIST` | SPA 构建目录（需含 `index.html`） | 仓库 `frontend/dist` |
| `CHATCHAT_AUTH_TTL_MINUTES` | 登录令牌有效期（分钟，clamp 5–10080） | `720` |
| `CHATCHAT_ENABLE_STREAMLIT_WEBUI` | 设为 `1` 时 `chatchat start -a` 附带启动旧 Streamlit | 关闭 |

前端侧配置：

| 变量 | 作用 |
|---|---|
| `VITE_API_BASE_URL` | 构建时 API 地址；留空表示同源（同源部署推荐留空） |
| `VITE_PROXY_TARGET` | 仅 `npm run dev` 的代理目标 |
| localStorage `chatchat.api_base` | 运行时覆盖 API 地址（Tauri 壳或跨机部署用），优先级最高 |

## 测试

```bash
npm run typecheck   # tsc --noEmit
npm run test        # vitest
```

测试重点覆盖 SSE 帧解析（半帧、多字节截断、CRLF、ping、尾帧冲刷）、
SSE 事件分类（token / 工具 / agent / 引用 / 进度 / 错误 / 畸形）、
auth store、http 层的 Bearer 与 `{code,msg,data}` 信封处理。

## 目录约定

```
src/
  api/        每个后端路由组一个 client，统一走 api/http.ts
  components/ ui/（shadcn）与 common/、chat/ 等业务组件
  hooks/      useChatStream、useRagStream 等流式状态机
  layouts/    AppLayout（侧栏 + 主题切换 + 错误边界）
  lib/        config、errors、sse/（frame-parser、classify、stream、applyEvent）
  pages/      路由页面
  routes/     guards.tsx（RequireAuth/RequireAdmin）、router.tsx
  stores/     auth、chatPrefs、theme（zustand persist）
```

## 与后端契约的几个坑

- 聊天接口的扩展字段（`conversation_id`、`auto_agent`、`agent_run`、`use_mcp`、
  `chat_model_config` 等）放在请求体**顶层**，不是嵌套 `extra_body`。
- 图片解析走 `POST /v1/chat/completions`（代理路由原样透传），`messages[].content`
  用 OpenAI 多模态数组（`text` + `image_url` 的 data URL），并需在设置里选一个
  image2text 视觉模型。`/chat/chat/completions` 收到多模态 content 会直接 400：
  它的 content 是字符串语义（会话标题、落库 `query`、prompt 模板）。
  代价是 v1 直通不落库：这一轮没有会话历史，也不能点赞/点踩。
- 知识库接口即使 HTTP 200 也可能带 `{code:404,...}` 信封错误，必须按 `code` 判错
  （`lib/errors.ts` 的 `unwrapEnvelope` / `checkEnvelope`）。
- `delete_knowledge_base` 的 body 是裸 JSON 字符串；`upload_docs`/`update_docs` 的
  `docs` 字段是 JSON 字符串；临时文件上传靠 `prev_id` 续传。
- 引用列表里的 `url` 指向需要 Bearer 的下载接口，点击时拦截成带鉴权的 blob 下载。
- SSE 帧分类顺序敏感：agent 事件（`status: null`）必须先于普通 token 判定，
  见 `src/lib/sse/classify.ts`。

## 旧 Streamlit 界面

`libs/chatchat-server/chatchat/webui_pages` 与 `webui.py` 暂时保留（代码未删除），
但默认不再随 `chatchat start -a` 启动；需要时用
`CHATCHAT_ENABLE_STREAMLIT_WEBUI=1` 或显式 `chatchat start -w`。
