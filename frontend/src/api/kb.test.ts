/**
 * 知识库接口的请求体形状（钉住契约，全部离线）。
 *
 * 出问题的现场：删除「无线通信」时界面提示「未找到知识库 无线通信」。
 * 后端 delete_kb 的签名是单个 `Body(...)` 标量，FastAPI 因此要求请求体是
 * **裸 JSON 字符串** `"无线通信"`；而 kb.ts 之前先 JSON.stringify 了一次，
 * http.ts 又编码一次，落到服务端的名字是 `"无线通信"`（含引号），
 * 查库落空 → HTTP 200 + code=404。
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  createKnowledgeBase,
  deleteDocs,
  deleteKnowledgeBase,
  updateDocs,
  uploadDocs,
} from "@/api/kb";

type FetchFn = (url: string, init: RequestInit) => Promise<Response>;

function envelope(code: number, msg = "ok", data: unknown = {}) {
  return new Response(JSON.stringify({ code, msg, data }), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

function mockFetch(impl: FetchFn = async () => envelope(200)) {
  const fn = vi.fn(impl);
  vi.stubGlobal("fetch", fn);
  return fn;
}

function lastCall(fn: ReturnType<typeof mockFetch>) {
  const [url, init] = fn.mock.calls[0];
  return { url, init };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("deleteKnowledgeBase", () => {
  it("请求体是裸 JSON 字符串，不被二次编码", async () => {
    const fetchMock = mockFetch();

    await deleteKnowledgeBase("无线通信");

    const { url, init } = lastCall(fetchMock);
    expect(url).toBe("/knowledge_base/delete_knowledge_base");
    expect(init.method).toBe("POST");
    expect(init.body).toBe('"无线通信"');
    expect((init.headers as Headers).get("Content-Type")).toBe(
      "application/json",
    );
  });

  it("名字里的引号只出现一层（回归：双编码会让服务端拿到带引号的名字）", async () => {
    const fetchMock = mockFetch();

    await deleteKnowledgeBase("other");

    const body = lastCall(fetchMock).init.body as string;
    expect(JSON.parse(body)).toBe("other");
  });

  it("code=404 时把服务端 msg 原样抛出（界面直接显示）", async () => {
    mockFetch(async () => envelope(404, '未找到知识库 "无线通信"'));

    await expect(deleteKnowledgeBase("无线通信")).rejects.toThrow(
      '未找到知识库 "无线通信"',
    );
  });
});

describe("知识库写操作请求体", () => {
  it("create 是对象体，字段名与服务端一致", async () => {
    const fetchMock = mockFetch();

    await createKnowledgeBase({
      knowledge_base_name: "kb1",
      vector_store_type: "faiss",
      kb_info: "简介",
      embed_model: "bge-m3",
    });

    const body = JSON.parse(lastCall(fetchMock).init.body as string);
    expect(body).toEqual({
      knowledge_base_name: "kb1",
      vector_store_type: "faiss",
      kb_info: "简介",
      embed_model: "bge-m3",
    });
  });

  it("upload_docs 走 multipart，不做 JSON 编码也不设 Content-Type", async () => {
    const fetchMock = mockFetch();
    const file = new File(["内容"], "a.txt", { type: "text/plain" });

    await uploadDocs({ knowledge_base_name: "kb1", files: [file] });

    const { url, init } = lastCall(fetchMock);
    expect(url).toBe("/knowledge_base/upload_docs");
    expect(init.body).toBeInstanceOf(FormData);
    const form = init.body as FormData;
    expect(form.get("knowledge_base_name")).toBe("kb1");
    expect(form.get("to_vector_store")).toBe("true");
    expect((init.headers as Headers).get("Content-Type")).toBeNull();
  });

  it("update_docs 的 docs 是对象里的 JSON 字符串（外层仍是一次编码）", async () => {
    const fetchMock = mockFetch();

    await updateDocs({
      knowledge_base_name: "kb1",
      file_names: ["a.txt"],
      docs: { "a.txt": { docs: [{ page_content: "x", metadata: {} }] } },
    });

    const body = JSON.parse(lastCall(fetchMock).init.body as string);
    expect(body.file_names).toEqual(["a.txt"]);
    expect(body.override_custom_docs).toBe(true);
    expect(typeof body.docs).toBe("string");
    expect(JSON.parse(body.docs)["a.txt"].docs[0].page_content).toBe("x");
  });

  it("delete_docs 带 delete_content / not_refresh_vs_cache", async () => {
    const fetchMock = mockFetch();

    await deleteDocs({
      knowledge_base_name: "kb1",
      file_names: ["a.txt"],
      deleteContent: true,
    });

    const body = JSON.parse(lastCall(fetchMock).init.body as string);
    expect(body).toEqual({
      knowledge_base_name: "kb1",
      file_names: ["a.txt"],
      delete_content: true,
      not_refresh_vs_cache: false,
    });
  });
});
