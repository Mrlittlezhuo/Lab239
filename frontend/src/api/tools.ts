import { getJson } from "@/api/http";
import { unwrapEnvelope, type Envelope } from "@/lib/errors";

export interface ToolInfo {
  name: string;
  /** 中文名，来自 @regist_tool(title=...)；text2images 的那个 title 是一大段说明 */
  title?: string;
  description?: string;
  args?: Record<string, unknown>; // JSON Schema 风格参数定义
  return_direct?: boolean;
  [key: string]: unknown;
}

/** GET /tools → {code,msg,data:{name:info}} → name→info 映射。
 *
 * 注意信封里 `data` 本身就是 name→info 映射，没有再套一层 `tools`
 * （见 `libs/chatchat-server/chatchat/server/api_server/tool_routes.py`）。
 * 之前按 `data.tools` 取，拿到 undefined，工具勾选列表永远是空的。
 */
export async function listTools(): Promise<Record<string, ToolInfo>> {
  const body = await getJson<Envelope<Record<string, ToolInfo>>>("/tools");
  return unwrapEnvelope(body) ?? {};
}
