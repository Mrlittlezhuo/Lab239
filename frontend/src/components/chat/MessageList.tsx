import { useEffect, useRef } from "react";

import { MessageBubble } from "@/components/chat/MessageBubble";
import type { UiMessage } from "@/hooks/useChatStream";

export function MessageList({
  messages,
  onFeedback,
  conversationId,
}: {
  messages: UiMessage[];
  onFeedback: (key: string, score: number) => void;
  /** 确认卡片保存到"仅本会话"时需要它；未建立会话时传 undefined */
  conversationId?: string;
}) {
  const bottomRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages]);

  return (
    <div className="space-y-3">
      {messages.map((m) => (
        <MessageBubble
          key={m.key}
          message={m}
          onFeedback={onFeedback}
          conversationId={conversationId}
        />
      ))}
      <div ref={bottomRef} />
    </div>
  );
}
