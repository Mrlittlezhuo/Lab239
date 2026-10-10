import { useEffect, useRef } from "react";

import { MessageBubble } from "@/components/chat/MessageBubble";
import type { UiMessage } from "@/hooks/useChatStream";

export function MessageList({
  messages,
  onFeedback,
}: {
  messages: UiMessage[];
  onFeedback: (key: string, score: number) => void;
}) {
  const bottomRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages]);

  return (
    <div className="space-y-3">
      {messages.map((m) => (
        <MessageBubble key={m.key} message={m} onFeedback={onFeedback} />
      ))}
      <div ref={bottomRef} />
    </div>
  );
}
