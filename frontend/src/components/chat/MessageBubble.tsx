import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { FeedbackBar } from "@/components/chat/FeedbackBar";
import { MemoryProposalCard } from "@/components/chat/MemoryProposalCard";
import {
  MemorySnapshotBadge,
  isIncompleteUsage,
} from "@/components/chat/MemorySnapshotBadge";
import { ReferenceList } from "@/components/chat/ReferenceList";
import { RouteBadge } from "@/components/chat/RouteBadge";
import { ToolCallCard } from "@/components/chat/ToolCallCard";
import { AgentRunPanel } from "@/components/chat/AgentRunPanel";
import { cn } from "@/lib/utils";
import type { UiMessage } from "@/hooks/useChatStream";

export function MessageBubble({
  message,
  onFeedback,
  conversationId,
}: {
  message: UiMessage;
  onFeedback: (key: string, score: number) => void;
  conversationId?: string;
}) {
  const isUser = message.role === "user";
  return (
    <div
      className={cn(
        "rounded-lg border p-3",
        isUser ? "bg-muted/60" : "bg-background",
      )}
    >
      {isUser ? (
        <>
          {message.imageDataUrl && (
            <img
              src={message.imageDataUrl}
              alt="附件"
              className="mb-2 max-h-48 rounded-md border"
            />
          )}
          <div className="whitespace-pre-wrap">{message.content}</div>
        </>
      ) : (
        <>
          <RouteBadge message={message} />
          {message.agent && <AgentRunPanel agent={message.agent} />}
          {message.toolCalls.map((call, i) => (
            <ToolCallCard key={i} call={call} />
          ))}
          <div className="markdown-body">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>
              {message.content}
            </ReactMarkdown>
          </div>
          {message.streaming && message.content === "" && (
            <div className="text-sm text-muted-foreground">思考中…</div>
          )}
          {message.error && (
            <div className="mt-2 rounded-md border border-destructive/40 bg-destructive/10 p-2 text-sm text-destructive">
              {message.error}
            </div>
          )}
          {isIncompleteUsage(message.memoryUsage) && (
            // 停止/失败/中断的回答：已有内容照常显示，但要说清它没说完，
            // 否则下一轮模型和用户都会把它当成已完成的结论（第四期 §11.3）。
            <div className="mt-2 rounded-md border border-amber-500/40 bg-amber-500/10 p-2 text-sm">
              这条回答未完成（已停止或失败），下面是已经生成的部分；你可以直接说
              “继续”。
            </div>
          )}
          {message.references && message.references.length > 0 && (
            <ReferenceList references={message.references} />
          )}
          {message.memoryUsage && !message.streaming && (
            <MemorySnapshotBadge usage={message.memoryUsage} />
          )}
          {message.note && (
            <div className="mt-2 text-xs text-muted-foreground">
              {message.note}
            </div>
          )}
          {!message.streaming &&
            (message.proposals ?? []).map((proposal, i) => (
              <MemoryProposalCard
                key={`${message.key}-p${i}`}
                proposal={proposal}
                conversationId={conversationId}
              />
            ))}
          {!message.streaming && (
            <FeedbackBar
              messageId={message.serverMessageId}
              score={message.feedbackScore}
              onScored={(score) => onFeedback(message.key, score)}
            />
          )}
        </>
      )}
    </div>
  );
}
