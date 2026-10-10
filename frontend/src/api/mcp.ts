import { deleteJson, getJson, postJson, putJson } from "@/api/http";

export interface McpConnection {
  id: string;
  server_name: string;
  args: string[];
  env: Record<string, string>;
  cwd?: string | null;
  transport: string; // stdio | sse
  timeout: number;
  enabled: boolean;
  description?: string | null;
  config: Record<string, unknown>;
  created_at?: string;
  updated_at?: string;
}

export interface McpStatusResponse {
  success: boolean;
  message: string;
  connection_id?: string | null;
}

export interface McpProfile {
  timeout: number;
  working_dir: string;
  env_vars: Record<string, string>;
  update_time: string;
}

const BASE = "/api/v1/mcp_connections";

export function listMcpConnections(): Promise<McpConnection[]> {
  return getJson<{ connections: McpConnection[]; total: number }>(`${BASE}/`).then(
    (r) => r.connections,
  );
}

export function createMcpConnection(input: {
  server_name: string;
  args: string[];
  env?: Record<string, string>;
  cwd?: string | null;
  transport?: string;
  timeout?: number;
  enabled?: boolean;
  description?: string | null;
}): Promise<McpConnection> {
  return postJson<McpConnection>(`${BASE}/`, input);
}

export function updateMcpConnection(
  id: string,
  patch: Partial<Omit<McpConnection, "id">>,
): Promise<McpStatusResponse> {
  return putJson<McpStatusResponse>(`${BASE}/${id}`, patch);
}

export function deleteMcpConnection(id: string): Promise<McpStatusResponse> {
  return deleteJson<McpStatusResponse>(`${BASE}/${id}`);
}

export function enableMcpConnection(id: string): Promise<McpStatusResponse> {
  return postJson<McpStatusResponse>(`${BASE}/${id}/enable`);
}

export function disableMcpConnection(id: string): Promise<McpStatusResponse> {
  return postJson<McpStatusResponse>(`${BASE}/${id}/disable`);
}

export function getMcpProfile(): Promise<McpProfile> {
  return getJson<McpProfile>(`${BASE}/profile`);
}

export function updateMcpProfile(input: {
  timeout: number;
  working_dir: string;
  env_vars: Record<string, string>;
}): Promise<McpProfile> {
  return putJson<McpProfile>(`${BASE}/profile`, input);
}
