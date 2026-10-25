import { deleteJson, getJson, patchJson, postJson } from "@/api/http";
import type { components } from "@/api/schema";

/**
 * 会话。字段定义来自后端契约（`ConversationOut`），不再手写：后端增删改字段时
 * 编译期就会报错，而不是等到运行时才发现。契约链见 contracts/README.md。
 *
 * 语义备注：
 * - `temporary`：临时对话，不创建、不保留会话私有上下文（第二期设计方案 §4.4）。
 * - `project_id`：会话所在项目空间，只有该项目的范围记忆能在本会话召回（第三期 §4.4）。
 */
export type Conversation = components["schemas"]["ConversationOut"];

/**
 * 会话历史消息。字段来自后端契约里的 `MessageOut`（`conversation_routes.py`
 * 本来就有这个 pydantic 模型，只是端点原先标注 `-> dict`，没进契约）。
 *
 * 注意可选性：生成器把「有默认值的字段」标成 `?`，所以这里比原先手抄的
 * `create_time: string | null` 更宽（`string | null | undefined`）。实际运行时
 * FastAPI 一定会把这些字段序列化出来（它们有默认值），所以这个 `?` 是契约生成器
 * 对响应模型的保守近似，不是后端真的会省略字段。
 */
export type MessageRecord = components["schemas"]["MessageOut"];

/** `GET /conversations/{id}/messages` 的响应包装，同样来自契约。 */
export type ConversationMessages =
  components["schemas"]["ConversationMessagesOut"];

/**
 * 会话列表。chatType 决定只看哪一类会话：智能聊天是 agent_chat，
 * 知识库对话是 kb_chat —— 不传就混在一起（知识库对话页会看到智能聊天的会话）。
 */
export function listConversations(chatType?: string): Promise<Conversation[]> {
  return getJson<Conversation[]>("/conversations", {
    query: chatType ? { chat_type: chatType } : undefined,
  });
}

export function createConversation(
  name = "",
  chatType = "agent_chat",
  temporary = false,
  projectId?: string | null,
): Promise<Conversation> {
  return postJson<Conversation>("/conversations", {
    name,
    chat_type: chatType,
    temporary,
    project_id: projectId || null,
  });
}

/**
 * 改会话所在的项目空间（第三期 §4.4）：传 null 表示回到"无项目"的普通会话。
 * 服务端只接受当前用户自己的项目，别人的项目返回 404。
 */
export function setConversationProject(
  id: string,
  projectId: string | null,
): Promise<Conversation> {
  return patchJson<Conversation>(`/conversations/${id}/project`, {
    project_id: projectId || null,
  });
}

/** 单个会话详情（聊天页需要知道会话挂在哪个项目空间下）。 */
export function getConversation(id: string): Promise<Conversation> {
  return getJson<Conversation>(`/conversations/${id}`);
}

export function renameConversation(
  id: string,
  name: string,
): Promise<Conversation> {
  return patchJson<Conversation>(`/conversations/${id}`, { name });
}

export function deleteConversation(id: string): Promise<unknown> {
  return deleteJson(`/conversations/${id}`);
}

export function listMessages(
  conversationId: string,
): Promise<ConversationMessages> {
  return getJson(`/conversations/${conversationId}/messages`);
}
