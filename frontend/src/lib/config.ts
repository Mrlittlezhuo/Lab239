/**
 * API base 解析：localStorage 覆盖（Tauri/调试用）> VITE_API_BASE_URL > 同源。
 * 返回空字符串表示同源相对路径。
 */
const STORAGE_KEY = "chatchat.api_base";

function stripTrailingSlash(url: string): string {
  return url.replace(/\/+$/, "");
}

export function getApiBase(): string {
  try {
    const override = localStorage.getItem(STORAGE_KEY);
    if (override) return stripTrailingSlash(override);
  } catch {
    // localStorage 不可用时退回环境变量/同源
  }
  const env = import.meta.env.VITE_API_BASE_URL;
  if (env) return stripTrailingSlash(env);
  return "";
}

export function setApiBase(url: string | null): void {
  try {
    if (url) localStorage.setItem(STORAGE_KEY, stripTrailingSlash(url));
    else localStorage.removeItem(STORAGE_KEY);
  } catch {
    // 忽略存储失败
  }
}

export function apiUrl(path: string): string {
  const normalized = path.startsWith("/") ? path : `/${path}`;
  return `${getApiBase()}${normalized}`;
}
