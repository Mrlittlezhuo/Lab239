/**
 * SSE 帧切分：跨 chunk 续帧、多字节安全、CRLF 归一、忽略注释行（ping）、
 * 多行 data 合并。纯逻辑，无 fetch 依赖，可单测。
 */
export class SseFrameParser {
  private decoder = new TextDecoder("utf-8");
  private buffer = "";
  /** 暂存可能是半个 CRLF 的尾随 \r */
  private pendingCr = false;

  /** 推入一段字节流，返回本次凑齐的帧 data 载荷列表。 */
  push(chunk: Uint8Array): string[] {
    return this.consume(this.decoder.decode(chunk, { stream: true }));
  }

  /** 流结束：冲刷解码器与缓冲区中最后一个（可能没有空行结尾的）帧。 */
  end(): string[] {
    const frames = this.consume(this.decoder.decode(), true);
    this.decoder = new TextDecoder("utf-8");
    return frames;
  }

  private consume(text: string, final = false): string[] {
    let text2 = text;
    let frames: string[] = [];
    // 归一化换行：\r\n → \n，孤立 \r → \n；尾随 \r 可能是半个 \r\n，扣住
    if (this.pendingCr) {
      text2 = "\r" + text2;
      this.pendingCr = false;
    }
    if (!final && text2.endsWith("\r")) {
      this.pendingCr = true;
      text2 = text2.slice(0, -1);
    }
    text2 = text2.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
    this.buffer += text2;

    for (;;) {
      const sep = this.buffer.indexOf("\n\n");
      if (sep === -1) break;
      const frame = this.buffer.slice(0, sep);
      this.buffer = this.buffer.slice(sep + 2);
      const payload = parseFrame(frame);
      if (payload !== null) frames.push(payload);
    }

    if (final && this.buffer.length > 0) {
      const payload = parseFrame(this.buffer);
      this.buffer = "";
      if (payload !== null) frames.push(payload);
    }
    return frames;
  }
}

/** 单帧文本 → data 载荷（多行 data 用 \n 连接）；无 data 返回 null。 */
function parseFrame(frame: string): string | null {
  const dataLines: string[] = [];
  for (const line of frame.split("\n")) {
    if (line === "") continue;
    if (line.startsWith(":")) continue; // 注释/ping
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    if (field !== "data") continue; // event:/id:/retry: 服务端未使用
    let value = colon === -1 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    dataLines.push(value);
  }
  if (dataLines.length === 0) return null;
  return dataLines.join("\n");
}
