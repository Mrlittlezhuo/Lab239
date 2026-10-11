/**
 * MCP 连接的环境变量：界面上按行输入 `KEY=VALUE`，存进库里是对象。
 *
 * 需要它是因为 stdio 类 MCP server 的凭据只能靠环境变量传（API key、profile
 * 路径等），以前页面上根本没有这个入口，只能把连接删了用命令行手插库。
 */

export interface ParsedEnv {
  env: Record<string, string>;
  /** 被丢弃的行，原样返回，便于界面上指名道姓地提示 */
  invalid: string[];
}

/**
 * 只按第一个 `=` 切分（值里可以含 `=`），空行忽略。
 *
 * 变量名不合法（不是 `[A-Za-z_][A-Za-z0-9_]*`）的行会被丢进 `invalid`：
 * 名字写错的 env 传下去是静默无效，与其让用户以为配好了，不如先拒掉。
 */
export function parseEnvLines(text: string): ParsedEnv {
  const env: Record<string, string> = {};
  const invalid: string[] = [];
  for (const raw of (text || "").split(/\r?\n/)) {
    const line = raw.trim();
    if (!line) continue;
    const at = line.indexOf("=");
    const key = at > 0 ? line.slice(0, at).trim() : "";
    const value = at > 0 ? line.slice(at + 1).trim() : "";
    if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(key)) {
      invalid.push(line);
      continue;
    }
    env[key] = value;
  }
  return { env, invalid };
}

/** 把库里的对象还原成按行文本，用于回填表单。 */
export function formatEnv(env?: Record<string, string> | null): string {
  return Object.entries(env ?? {})
    .map(([k, v]) => `${k}=${v}`)
    .join("\n");
}
