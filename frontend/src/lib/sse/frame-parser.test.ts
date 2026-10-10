import { describe, expect, it } from "vitest";

import { SseFrameParser } from "@/lib/sse/frame-parser";

function encodeChunks(text: string): Uint8Array[] {
  return [new TextEncoder().encode(text)];
}

function run(text: string): string[] {
  const p = new SseFrameParser();
  const frames = encodeChunks(text).flatMap((c) => p.push(c));
  return [...frames, ...p.end()];
}

describe("SseFrameParser", () => {
  it("基本帧", () => {
    expect(run("data: hello\n\ndata: world\n\n")).toEqual(["hello", "world"]);
  });

  it("跨 chunk 半帧续传", () => {
    const p = new SseFrameParser();
    const enc = new TextEncoder();
    let frames = p.push(enc.encode("data: hel"));
    expect(frames).toEqual([]);
    frames = p.push(enc.encode("lo\n"));
    expect(frames).toEqual([]);
    frames = p.push(enc.encode("\ndata: x\n\n"));
    expect(frames).toEqual(["hello", "x"]);
    expect(p.end()).toEqual([]);
  });

  it("多字节字符跨 chunk 不被截断", () => {
    const p = new SseFrameParser();
    const full = new TextEncoder().encode("data: 你好\n\n");
    // “你”的 UTF-8 编码中间切开
    const cut = full.indexOf(0xe4) + 1;
    const frames = [...p.push(full.slice(0, cut)), ...p.push(full.slice(cut)), ...p.end()];
    expect(frames).toEqual(["你好"]);
  });

  it("CRLF 与尾随 \\r 半个换行", () => {
    const p = new SseFrameParser();
    const enc = new TextEncoder();
    let frames = p.push(enc.encode("data: a\r"));
    expect(frames).toEqual([]);
    frames = p.push(enc.encode("\n\r\n"));
    expect(frames).toEqual(["a"]);
    expect(p.end()).toEqual([]);
  });

  it("忽略 ping 注释行与 event/id 字段", () => {
    expect(
      run(": ping - 2026-10-10 00:00:00.000000+00:00\n\nevent: message\nid: 1\ndata: ok\n\n"),
    ).toEqual(["ok"]);
  });

  it("多行 data 用换行合并", () => {
    expect(run("data: line1\ndata: line2\n\n")).toEqual(["line1\nline2"]);
  });

  it("end() 冲刷无空行结尾的尾帧", () => {
    expect(run("data: tail")).toEqual(["tail"]);
  });

  it("纯注释帧不产出", () => {
    expect(run(": ping\n\n")).toEqual([]);
  });

  it("逐字节推 1000 帧不丢帧", () => {
    const p = new SseFrameParser();
    const enc = new TextEncoder();
    const payload = "data: {\"i\":1}\n\n";
    const bytes = enc.encode(payload.repeat(1000));
    const frames: string[] = [];
    for (let i = 0; i < bytes.length; i++) frames.push(...p.push(bytes.subarray(i, i + 1)));
    frames.push(...p.end());
    expect(frames).toHaveLength(1000);
    expect(frames[0]).toBe('{"i":1}');
  });
});
