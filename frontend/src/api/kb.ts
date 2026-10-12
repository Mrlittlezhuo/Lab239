import { getJson, postJson, request } from "@/api/http";
import { apiUrl } from "@/lib/config";
import {
  checkEnvelope,
  unwrapEnvelope,
  type Envelope,
} from "@/lib/errors";
import { getAuthToken } from "@/stores/auth";

export interface KbSummary {
  id?: number;
  kb_name: string;
  vs_type?: string;
  embed_model?: string;
  kb_info?: string;
  file_count?: number;
  [key: string]: unknown;
}

export interface KbFileDetail {
  file_name: string;
  file_id?: string;
  kb_name?: string;
  file_ext?: string;
  file_mtime?: string;
  file_size?: number;
  [key: string]: unknown;
}

export interface KbSearchDoc {
  id: string;
  page_content: string;
  metadata: Record<string, unknown>;
  score?: number;
}

export async function listKnowledgeBases(): Promise<KbSummary[]> {
  const body = await getJson<Envelope<KbSummary[]>>(
    "/knowledge_base/list_knowledge_bases",
  );
  return unwrapEnvelope(body);
}

export async function createKnowledgeBase(input: {
  knowledge_base_name: string;
  vector_store_type: string;
  kb_info: string;
  embed_model: string;
}): Promise<Envelope> {
  const body = await postJson<Envelope>("/knowledge_base/create_knowledge_base", input);
  return checkEnvelope(body);
}

/**
 * 服务端签名是单个 Body str，请求体是裸 JSON 字符串 `"知识库名"`。
 *
 * 这里只能传原始字符串，不能先 JSON.stringify：http.ts 的 request 会对
 * 非 FormData 的 body 编码一次，先编码就变成 `"\"知识库名\""`，服务端校验
 * 通过后拿到的是带引号的名字，查库必然落空，界面只看到
 * 「未找到知识库 "知识库名"」（HTTP 200 + code=404）。
 */
export async function deleteKnowledgeBase(name: string): Promise<Envelope> {
  const body = await request<Envelope>("/knowledge_base/delete_knowledge_base", {
    method: "POST",
    body: name,
  });
  return checkEnvelope(body);
}

export async function listFiles(kbName: string): Promise<KbFileDetail[]> {
  const body = await getJson<Envelope<KbFileDetail[]>>("/knowledge_base/list_files", {
    query: { knowledge_base_name: kbName },
  });
  return unwrapEnvelope(body);
}

/** search_docs 无信封，直接返回数组。 */
export function searchDocs(input: {
  query?: string;
  knowledge_base_name: string;
  top_k?: number;
  score_threshold?: number;
  file_name?: string;
  metadata?: Record<string, unknown>;
}): Promise<KbSearchDoc[]> {
  return postJson<KbSearchDoc[]>("/knowledge_base/search_docs", input);
}

export async function uploadDocs(input: {
  knowledge_base_name: string;
  files: File[];
  override?: boolean;
  toVectorStore?: boolean;
  chunkSize?: number;
  chunkOverlap?: number;
  zhTitleEnhance?: boolean;
}): Promise<Envelope> {
  const form = new FormData();
  for (const f of input.files) form.append("files", f);
  form.append("knowledge_base_name", input.knowledge_base_name);
  form.append("override", String(input.override ?? false));
  form.append("to_vector_store", String(input.toVectorStore ?? true));
  if (input.chunkSize !== undefined) form.append("chunk_size", String(input.chunkSize));
  if (input.chunkOverlap !== undefined)
    form.append("chunk_overlap", String(input.chunkOverlap));
  if (input.zhTitleEnhance !== undefined)
    form.append("zh_title_enhance", String(input.zhTitleEnhance));
  form.append("docs", "");
  const body = await request<Envelope>("/knowledge_base/upload_docs", {
    method: "POST",
    body: form,
  });
  return checkEnvelope(body);
}

export async function deleteDocs(input: {
  knowledge_base_name: string;
  file_names: string[];
  deleteContent?: boolean;
}): Promise<Envelope> {
  // 逐个列字段，不 spread input：input 里是 camelCase（deleteContent），
  // spread 会把 camelCase 一起发给服务端。
  const body = await postJson<Envelope>("/knowledge_base/delete_docs", {
    knowledge_base_name: input.knowledge_base_name,
    file_names: input.file_names,
    delete_content: input.deleteContent ?? false,
    not_refresh_vs_cache: false,
  });
  return checkEnvelope(body);
}

export async function updateKbInfo(
  kbName: string,
  kbInfo: string,
): Promise<Envelope> {
  const body = await postJson<Envelope>("/knowledge_base/update_info", {
    knowledge_base_name: kbName,
    kb_info: kbInfo,
  });
  return checkEnvelope(body);
}

/**
 * 重新向量化文件。docs 是 JSON 字符串（服务端约定）：
 * {file_name: {docs: [{page_content, metadata}], texts?: [...]}}
 */
export async function updateDocs(input: {
  knowledge_base_name: string;
  file_names: string[];
  docs?: unknown;
  chunkSize?: number;
  chunkOverlap?: number;
  zhTitleEnhance?: boolean;
}): Promise<Envelope> {
  const body = await postJson<Envelope>("/knowledge_base/update_docs", {
    knowledge_base_name: input.knowledge_base_name,
    file_names: input.file_names,
    chunk_size: input.chunkSize,
    chunk_overlap: input.chunkOverlap,
    zh_title_enhance: input.zhTitleEnhance,
    override_custom_docs: true,
    docs: input.docs === undefined ? "" : JSON.stringify(input.docs),
    not_refresh_vs_cache: false,
  });
  return checkEnvelope(body);
}

/** 临时文件上传（文件对话）。返回 knowledge_base_id 供 temp_kb 模式使用。 */
export async function uploadTempDocs(input: {
  files: File[];
  prevId?: string | null;
  chunkSize?: number;
  chunkOverlap?: number;
}): Promise<{ knowledge_base_id: string; file_counts: Record<string, number> }> {
  const form = new FormData();
  for (const f of input.files) form.append("files", f);
  if (input.prevId) form.append("prev_id", input.prevId);
  if (input.chunkSize !== undefined) form.append("chunk_size", String(input.chunkSize));
  if (input.chunkOverlap !== undefined)
    form.append("chunk_overlap", String(input.chunkOverlap));
  const body = await request<
    Envelope<{ knowledge_base_id: string; file_counts: Record<string, number> }>
  >("/knowledge_base/upload_temp_docs", { method: "POST", body: form });
  return unwrapEnvelope(body);
}

/** download_doc 需要 Bearer，返回同源 URL 仅供 fetch-blob 使用。 */
export function downloadDocUrl(kbName: string, fileName: string): string {
  const params = new URLSearchParams({
    knowledge_base_name: kbName,
    file_name: fileName,
  });
  return apiUrl(`/knowledge_base/download_doc?${params.toString()}`);
}

export async function fetchDocBlob(
  kbName: string,
  fileName: string,
): Promise<Blob> {
  const res = await fetch(downloadDocUrl(kbName, fileName), {
    headers: { Authorization: `Bearer ${getAuthToken() ?? ""}` },
    credentials: "omit",
  });
  if (!res.ok) throw new Error(`下载失败 HTTP ${res.status}`);
  return res.blob();
}
