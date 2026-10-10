import type { UiMessage } from "@/hooks/useChatStream";

/** 会话消息导出为 Markdown 文本。 */
export function messagesToMarkdown(
  messages: UiMessage[],
  title = "聊天记录",
): string {
  const lines: string[] = [`# ${title}`, ""];
  for (const m of messages) {
    if (m.role === "user") {
      lines.push(`## 用户`, "", m.content, "");
    } else {
      lines.push(`## 助手`, "");
      if (m.route?.executionModeLabel) {
        lines.push(`> 路由：${m.route.executionModeLabel}`, "");
      }
      for (const tc of m.toolCalls) {
        lines.push(`- 工具 \`${tc.name ?? "?"}\`：${tc.done ? "完成" : "执行中"}`);
      }
      if (m.toolCalls.length > 0) lines.push("");
      lines.push(m.content, "");
      if (m.references && m.references.length > 0) {
        lines.push("### 参考资料", "");
        for (const r of m.references) {
          lines.push(`- ${r.file_name ?? r.knowledge_base ?? "引用"}（${r.score ?? "-"}）`);
        }
        lines.push("");
      }
    }
  }
  return lines.join("\n");
}
