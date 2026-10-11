import { afterEach, describe, expect, it, vi } from "vitest";

import { listTools } from "@/api/tools";
import { ApiError } from "@/lib/errors";
import { clearAuth } from "@/stores/auth";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

describe("listTools", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    clearAuth();
    localStorage.clear();
  });

  it("data 本身就是 name→info 映射（后端没有再套一层 tools）", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse(200, {
          code: 200,
          msg: "success",
          data: {
            calculate: { name: "calculate", description: "计算", args: {} },
            search_internet: { name: "search_internet", description: "搜索" },
          },
        }),
      ),
    );

    const tools = await listTools();

    expect(Object.keys(tools).sort()).toEqual(["calculate", "search_internet"]);
    expect(tools.calculate.description).toBe("计算");
  });

  it("信封错误抛 ApiError，不静默返回空列表", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse(200, { code: 401, msg: "unauthorized" })),
    );

    await expect(listTools()).rejects.toBeInstanceOf(ApiError);
  });

  it("data 缺失时回落成空映射", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse(200, { code: 200, msg: "success" })),
    );

    expect(await listTools()).toEqual({});
  });
});
