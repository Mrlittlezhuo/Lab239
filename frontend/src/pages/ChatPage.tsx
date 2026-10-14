import { useEffect } from "react";
import { Download } from "lucide-react";
import { useNavigate, useParams } from "react-router-dom";
import { toast } from "sonner";

import { listMessages } from "@/api/conversations";
import { ChatSettings } from "@/components/chat/ChatSettings";
import { Composer } from "@/components/chat/Composer";
import { ConversationSidebar } from "@/components/chat/ConversationSidebar";
import { MemoryUsageBadge } from "@/components/chat/MemoryUsageBadge";
import { MessageList } from "@/components/chat/MessageList";
import { ProjectSelect } from "@/components/chat/ProjectSelect";
import { RecalledMemoriesPanel } from "@/components/chat/RecalledMemoriesPanel";
import { SessionContextBadge } from "@/components/chat/SessionContextBadge";
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
    onConversationId: (id) => {
      // 新会话拿到真实 id 后，把会话级开关（本次不使用个人偏好、临时对话）
      // 带到真实 id 上，否则首轮之后这些开关就失效了
      prefs.mergeMemoryOff("new", id);
      prefs.mergeTemporary("new", id);
      // 项目选择已经随首轮请求写进会话，本地这条"新会话"默认值就此了结：
      // 再开新会话时回到"不选项目"，保证普通聊天默认不带项目记忆（§4.4）
      prefs.clearProjectChoice("new");
      navigate(`/chat/${id}`, { replace: true });
    },
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
  // 召回与注入都以本轮用户问题为准，因此取最近一条用户消息
  const lastQuery =
    [...chat.messages].reverse().find((m) => m.role === "user")?.content ?? "";

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
            <ProjectSelect
              conversationId={conversationId}
              conversationKey={draftKey}
            />
            <SessionContextBadge
              conversationId={conversationId}
              conversationKey={draftKey}
            />
            <MemoryUsageBadge conversationKey={draftKey} />
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
            conversationId={conversationId}
            onFeedback={(key, score) => chat.patchMessage(key, { feedbackScore: score })}
          />
          {/* 回答下方列出本轮真正注入的历史记忆（第三期 §4.2、§7） */}
          <RecalledMemoriesPanel
            query={lastQuery}
            conversationId={conversationId}
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
              useMemory: !prefs.memoryOff[draftKey],
              temporary: prefs.temporary[draftKey] === true,
              // 只在会话还没建立时决定项目空间；已有会话以服务端记录为准
              projectId: prefs.projectChoice[draftKey],
            });
          }}
          onStop={chat.stop}
          streaming={chat.streaming}
        />
      </div>
    </div>
  );
}
