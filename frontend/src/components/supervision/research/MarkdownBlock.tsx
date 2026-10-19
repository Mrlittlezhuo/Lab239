import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

/**
 * 讨论消息的 Markdown 渲染。
 *
 * 只用 `react-markdown` + `remark-gfm`（聊天页同一套），不引入 `rehype-raw`：
 * 模型或成员写的原始 HTML 一律按文本转义，不会被当成 HTML 注入页面。
 */
export function MarkdownBlock({ text, className }: { text: string; className?: string }) {
  if (!text.trim()) {
    return <p className="text-sm text-muted-foreground">（空）</p>;
  }
  return (
    <div className={className ?? "markdown-body"}>
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{text}</ReactMarkdown>
    </div>
  );
}
