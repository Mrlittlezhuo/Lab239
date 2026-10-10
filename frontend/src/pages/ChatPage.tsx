import { useEffect } from "react";
import { Download } from "lucide-react";
import { useNavigate, useParams } from "react-router-dom";
import { toast } from "sonner";

import { listMessages } from "@/api/conversations";
import { ChatSettings } from "@/components/chat/ChatSettings";
import { Composer } from "@/components/chat/Composer";
import { ConversationSidebar } from "@/components/chat/ConversationSidebar";
import { MessageList } from "@/components/chat/MessageList";
import { Button } from "@/components/ui/button";
import {
  historyToUiMessages,
  useChatStream,
} from "@/hooks/useChatStream";
import { downloadText } from "@/lib/download";
import { messagesToMarkdown } from "@/lib/exportMarkdown";
import { useChatPrefs } from "@/stores/chatPrefs";

export function ChatPage() {
  const { conversationId } = useParams<{ conversationId?: string }>();
  const navigate = useNavigate();
  const prefs = useChatPrefs();
  const chat = useChatStream({
    conversationId,
    onConversationId: (id) => navigate(`/chat/${id}`, { replace: true }),
  });

  useEffect(() => {
    if (!conversationId) {
      chat.reset();
      return;
    }
    let cancelled = false;
    listMessages(conversationId)
      .then((res) => {
        if (!cancelled) chat.setInitialMessages(historyToUiMessages(res.messages));
      })
      .catch((err) => {
        if (!cancelled)
          toast.error(err instanceof Error ? err.message : "加载历史失败");
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conversationId]);

  const draftKey = conversationId ?? "new";
  const draft = prefs.drafts[draftKey] ?? "";

  function handleExport() {
    if (chat.messages.length === 0) {
      toast.info("当前没有消息可导出");
      return;
    }
    downloadText(messagesToMarkdown(chat.messages), "chat-export.md");
  }

  return (
    <div className="flex h-full">
      <ConversationSidebar
        activeId={conversationId}
        onSelect={(id) => navigate(id ? `/chat/${id}` : "/chat")}
      />
      <div className="flex min-w-0 flex-1 flex-col">
        <div className="flex items-center justify-between border-b pb-3">
          <h1 className="text-lg font-semibold">智能聊天</h1>
          <div className="flex items-center gap-2">
            <Button variant="outline" size="sm" onClick={handleExport} title="导出 Markdown">
              <Download className="h-4 w-4" />
              导出
            </Button>
            <ChatSettings />
          </div>
        </div>
        <div className="flex-1 overflow-y-auto py-4">
          <MessageList
            messages={chat.messages}
            onFeedback={(key, score) => chat.patchMessage(key, { feedbackScore: score })}
          />
        </div>
        <Composer
          value={draft}
          onChange={(v) => prefs.setDraft(draftKey, v)}
          onSend={(text, imageDataUrl) => {
            prefs.clearDraft(draftKey);
            void chat.send(text, {
              model: prefs.model,
              temperature: prefs.temperature,
              toolMode: prefs.toolMode,
              selectedTools: prefs.selectedTools,
              useMcp: prefs.useMcp,
              imageDataUrl,
              visionModel: prefs.visionModel,
            });
          }}
          onStop={chat.stop}
          streaming={chat.streaming}
        />
      </div>
    </div>
  );
}
