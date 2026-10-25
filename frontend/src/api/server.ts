import { getJson } from "@/api/http";
import type { components } from "@/api/schema";

/**
 * 模型清单接口的类型全部来自后端契约（`GET /server/models` 的
 * `response_model=ServerModelsOut`），不再手写。契约链见 contracts/README.md。
 *
 * 备注：`llm_model_config` 的内层刻意保持宽松（`Record<string, unknown>`）。
 * 后端 `LLM_MODEL_CONFIG` 是 `Dict[str, Dict]`，内层键由用户 yaml 决定，
 * 收紧成固定字段会把用户配置的额外键从响应里过滤掉。当前实际出现的键是：
 * `model`、`temperature`、`max_tokens`、`history_len`、`prompt_name`、`callbacks`。
 */
export type PlatformModels = components["schemas"]["PlatformModelsOut"];
export type KbDefaults = components["schemas"]["KBDefaultsOut"];
export type ServerModels = components["schemas"]["ServerModelsOut"];

export function getServerModels(): Promise<ServerModels> {
  return getJson<ServerModels>("/server/models");
}
