import { describe, expect, it } from "vitest";

import { formatEnv, parseEnvLines } from "@/lib/mcpEnv";

describe("parseEnvLines", () => {
  it("按行解析 KEY=VALUE", () => {
    expect(parseEnvLines("API_KEY=sk-abc\nDEBUG=1").env).toEqual({
      API_KEY: "sk-abc",
      DEBUG: "1",
    });
  });

  it("只按第一个 = 切分，值里可以含 = 和空格", () => {
    expect(parseEnvLines("CHROME_ARGS=--user-data-dir=/tmp/p --headless").env).toEqual({
      CHROME_ARGS: "--user-data-dir=/tmp/p --headless",
    });
    expect(parseEnvLines("DSN=user=pwd=pw").env).toEqual({ DSN: "user=pwd=pw" });
  });

  it("忽略空行与行首尾空白，兼容 CRLF", () => {
    expect(parseEnvLines("  A=1  \r\n\r\n  B=2\r\n").env).toEqual({ A: "1", B: "2" });
  });

  it("没有 = 或变量名不合法的行进 invalid", () => {
    const parsed = parseEnvLines("API_KEY=ok\n没有等号\n9BAD=x\n=空名");
    expect(parsed.env).toEqual({ API_KEY: "ok" });
    expect(parsed.invalid).toEqual(["没有等号", "9BAD=x", "=空名"]);
  });

  it("空输入得到空对象", () => {
    expect(parseEnvLines("")).toEqual({ env: {}, invalid: [] });
  });
});

describe("formatEnv", () => {
  it("对象还原成按行文本", () => {
    expect(formatEnv({ API_KEY: "sk-abc", DEBUG: "1" })).toBe("API_KEY=sk-abc\nDEBUG=1");
  });

  it("缺字段时不炸", () => {
    expect(formatEnv(undefined)).toBe("");
    expect(formatEnv(null)).toBe("");
  });
});
