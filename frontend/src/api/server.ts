import { getJson } from "@/api/http";

export interface PlatformModels {
  name: string;
  llm_models: string[];
  embed_models: string[];
  image2text_models: string[];
}

export interface LlmModelParams {
  model: string;
  temperature: number;
  max_tokens: number;
  history_len: number;
  prompt_name: string;
  callbacks: boolean;
}

export interface KbDefaults {
  chunk_size: number;
  chunk_overlap: number;
  vector_search_top_k: number;
  score_threshold: number;
  default_knowledge_base: string;
  default_vs_type: string;
  default_search_engine: string;
}

export interface ServerModels {
  platforms: PlatformModels[];
  llm_models: string[];
  embedding_models: string[];
  image2text_models: string[];
  default_llm: string;
  default_embedding: string;
  history_len: number;
  temperature: number;
  max_tokens: number | null;
  support_agent_models: string[];
  llm_model_config: Record<string, LlmModelParams>;
  search_engines: string[];
  vs_types: string[];
  kb_defaults: KbDefaults;
}

export function getServerModels(): Promise<ServerModels> {
  return getJson<ServerModels>("/server/models");
}
