import type { DialogueMessages, ResearchDialogue } from "@/api/supervision";
import { formatTime } from "@/components/supervision/research/briefModel";

/**
 * 项目内研究讨论消息的纯逻辑。
 *
 * 后端一条 `message` 行同时存了成员发言（`query`）与 AI 回复（`response`），
 * 回复是异步写入的：模型未接入或失败时 `response` 为空、`meta_data.pending_reply`
 * 为真。界面必须把这两种情况分开，不能把空回复显示成「AI 说了空话」。
 */

export type DialogueMessageRow = DialogueMessages["messages"][number];

export interface DialogueTurn {
  key: string;
  message_id: string;
  /** member = 当前成员自己的发言；assistant = AI 回复 */
  role: "member" | "assistant";
  text: string;
  time: string;
  pendingReply: boolean;
  truncated: boolean;
  modelName: string;
  contextInjected: boolean | null;
}

function metaOf(row: DialogueMessageRow): Record<string, unknown> {
  return (row.metadata ?? {}) as Record<string, unknown>;
}

function metaBool(value: unknown): boolean | null {
  return typeof value === "boolean" ? value : null;
}

/** 一条消息行拆成「我的发言」+「AI 回复」两轮；空的一侧不产生轮次。 */
export function dialogueTurns(messages: DialogueMessageRow[]): DialogueTurn[] {
  const turns: DialogueTurn[] = [];
  for (const row of messages ?? []) {
    const meta = metaOf(row);
    const query = (row.query ?? "").trim();
    const response = row.response ?? "";
    if (query) {
      turns.push({
        key: `${row.message_id}-q`,
        message_id: row.message_id,
        role: "member",
        text: query,
        time: formatTime(row.create_time),
        pendingReply: false,
        truncated: false,
        modelName: "",
        contextInjected: null,
      });
    }
    if (response.trim()) {
      turns.push({
        key: `${row.message_id}-a`,
        message_id: row.message_id,
        role: "assistant",
        text: response,
        time: formatTime(String(meta.reply_time ?? "") || (row.create_time ?? "")),
        pendingReply: false,
        truncated: metaBool(meta.truncated_response) === true,
        modelName: typeof meta.model_name === "string" ? meta.model_name : "",
        contextInjected: metaBool(meta.project_context_injected),
      });
    } else if (isPendingReply(row)) {
      turns.push({
        key: `${row.message_id}-pending`,
        message_id: row.message_id,
        role: "assistant",
        text: "",
        time: "",
        pendingReply: true,
        truncated: false,
        modelName: "",
        contextInjected: null,
      });
    }
  }
  return turns;
}

/** 提取研究定义时可选的消息范围；只按成员发言计数（AI 回复不是线索来源）。 */
export function selectableMessageIds(messages: DialogueMessageRow[]): string[] {
  const ids: string[] = [];
  for (const row of messages ?? []) {
    if ((row.query ?? "").trim() && !ids.includes(row.message_id)) ids.push(row.message_id);
  }
  return ids;
}

export function messageExcerpt(row: DialogueMessageRow, max = 60): string {
  const text = (row.query ?? "").replace(/\s+/g, " ").trim();
  return text.length > max ? `${text.slice(0, max)}…` : text;
}

export function dialogueLabel(dialogue: ResearchDialogue): string {
  return `${dialogue.code} · ${dialogue.message_count} 条消息`;
}

/** 后端 `DialogueMessageBody.text` 的上限（与 message.query 列宽一致）。 */
export const MAX_MESSAGE_CHARS = 4096;

/**
 * 发送前的本地校验说明：后端对空正文与超长正文一律 422，
 * 这里只是提前给出可读提示，不改变后端语义。
 */
export function messageLengthIssue(text: string): string | null {
  const trimmed = text.trim();
  if (!trimmed) return "先写下这轮讨论的问题或回答再发送。";
  if (trimmed.length > MAX_MESSAGE_CHARS) {
    return `单条消息最多 ${MAX_MESSAGE_CHARS} 字，现在有 ${trimmed.length} 字，请拆分后重新发送。`;
  }
  return null;
}

/** 这条发言是否还在等 AI 回复（`response` 空 + `pending_reply`）。 */
export function isPendingReply(row: DialogueMessageRow): boolean {
  if ((row.response ?? "").trim()) return false;
  if (!(row.query ?? "").trim()) return false;
  return true;
}

/** 后端 `DialogueBody.title` 的上限（max_length=50）。 */
export const DIALOGUE_TITLE_MAX = 50;

/**
 * 进入项目时自动建会话用的初始标题：`{项目名} 研究讨论`，按后端上限截断。
 * 它只是初始值——后端 `create_dialogue` 幂等（已有则复用），学生与导师
 * 在同一项目里各自一条、互不可见。
 */
export function defaultDialogueTitle(projectName: string): string {
  const name = (projectName ?? "").trim() || "本项目";
  return `${name} 研究讨论`.slice(0, DIALOGUE_TITLE_MAX);
}

/**
 * 会话列表按 `update_time` 倒序返回（后端排序），这里只做「最近讨论时间」文案，
 * 不重新排序、不自己判断哪条是主会话。
 */
export function dialogueUpdatedText(dialogue: ResearchDialogue): string {
  const updated = formatTime(dialogue.update_time) || formatTime(dialogue.create_time);
  return updated ? `更新于 ${updated}` : "尚无更新";
}
