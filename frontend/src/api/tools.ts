import { getJson } from "@/api/http";
import { unwrapEnvelope, type Envelope } from "@/lib/errors";

export interface ToolInfo {
  name: string;
  description?: string;
  args?: Record<string, unknown>; // JSON Schema 风格参数定义
  return_direct?: boolean;
  [key: string]: unknown;
}

/** GET /tools → {code,msg,data:{tools:{name:info}}} → name→info 映射。 */
export async function listTools(): Promise<Record<string, ToolInfo>> {
  const body = await getJson<Envelope<{ tools: Record<string, ToolInfo> }>>(
    "/tools",
  );
  return unwrapEnvelope(body).tools;
}
