/**
 * 由 tools/gen_sse_types.py 从 contracts/sse-frames.json 生成，请勿手改。
 * 改帧字段请先改契约（后端 test_sse_frame_vocabulary_matches_contract 会
 * 用真实流式运行核对键集），再重新生成本文件。
 *
 * 覆盖范围：/chat/kb_chat 与 /chat/file_chat 的帧。smart_conversation 与
 * agent_run 链路的帧尚未纳入，见契约 open_gaps。
 */

/** SSE 帧种类，判别依据是契约 transport.discriminator。 */
export type SseFrameName = "rag_content" | "memory_usage";

/** 来自契约 frames[rag_content].semantics.retrieval_status。 */
export type RetrievalStatus = "pending" | "matched" | "empty" | "error";
export const RETRIEVAL_STATUS: readonly RetrievalStatus[] = ["pending", "matched", "empty", "error"] as const;

/** 来自契约 frames[memory_usage].semantics.generation_status。 */
export type GenerationStatus = "prepared" | "queued" | "started" | "partial" | "cancelled" | "failed" | "completed";
export const GENERATION_STATUS: readonly GenerationStatus[] = ["prepared", "queued", "started", "partial", "cancelled", "failed", "completed"] as const;

/** 来自契约 frames[memory_usage].semantics.incomplete_statuses。 */
export type IncompleteStatuses = "partial" | "cancelled" | "failed";
export const INCOMPLETE_STATUSES: readonly GenerationStatus[] = ["partial", "cancelled", "failed"] as const;

/** 每一帧都带的公共键（含未声明值类型的 extra 字段）。 */
export const SSE_COMMON_KEYS = ["choices", "created", "id", "is_ref", "message_id", "message_type", "model", "object", "status"] as const;

/** 每种帧除公共键外额外带的键。 */
export const SSE_FRAME_EXTRA_KEYS = {
  "rag_content": ["conversation_id", "docs", "references", "retrieval_status"] as const,
  "memory_usage": ["class_name", "generation_status", "memory_usage_manifest"] as const,
} as const;

/** 契约声明的全部帧键（公共 + 额外），用于运行时核对未知键。 */
export const SSE_KNOWN_KEYS: readonly string[] = [
  ...SSE_COMMON_KEYS,
  ...SSE_FRAME_EXTRA_KEYS["rag_content"],
  ...SSE_FRAME_EXTRA_KEYS["memory_usage"],
];
