import { getJson, patchJson, postJson } from "@/api/http";
import type { AuthUser } from "@/stores/auth";

export type UserOut = AuthUser;

export function listUsers(): Promise<UserOut[]> {
  return getJson<UserOut[]>("/admin/users");
}

export function createUser(input: {
  username: string;
  display_name?: string;
  password: string;
  role: "admin" | "user";
}): Promise<UserOut> {
  return postJson<UserOut>("/admin/users", input);
}

export function setUserStatus(
  userId: string,
  status: "active" | "disabled",
): Promise<UserOut> {
  return patchJson<UserOut>(`/admin/users/${userId}/status`, { status });
}

export function resetUserPassword(userId: string, password: string): Promise<UserOut> {
  return postJson<UserOut>(`/admin/users/${userId}/reset-password`, { password });
}
