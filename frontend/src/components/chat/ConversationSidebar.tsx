import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { MessageSquare, Pencil, Plus, Trash2 } from "lucide-react";
import { toast } from "sonner";

import {
  createConversation,
  deleteConversation,
  listConversations,
  renameConversation,
} from "@/api/conversations";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";

export function ConversationSidebar({
  activeId,
  onSelect,
  chatType = "agent_chat",
}: {
  activeId?: string;
  onSelect: (id: string | null) => void;
  chatType?: string;
}) {
  const queryClient = useQueryClient();
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editingName, setEditingName] = useState("");

  const { data: conversations = [] } = useQuery({
    queryKey: ["conversations"],
    queryFn: listConversations,
  });

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: ["conversations"] });

  const createMutation = useMutation({
    mutationFn: () => createConversation("", chatType),
    onSuccess: (conv) => {
      invalidate();
      onSelect(conv.id);
    },
    onError: (err) => toast.error(err instanceof Error ? err.message : "创建会话失败"),
  });

  const renameMutation = useMutation({
    mutationFn: ({ id, name }: { id: string; name: string }) =>
      renameConversation(id, name),
    onSuccess: invalidate,
    onError: (err) => toast.error(err instanceof Error ? err.message : "重命名失败"),
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => deleteConversation(id),
    onSuccess: (_data, id) => {
      invalidate();
      if (activeId === id) onSelect(null);
    },
    onError: (err) => toast.error(err instanceof Error ? err.message : "删除失败"),
  });

  return (
    <div className="flex h-full w-60 shrink-0 flex-col border-r">
      <div className="p-2">
        <Button
          size="sm"
          className="w-full"
          onClick={() => createMutation.mutate()}
          disabled={createMutation.isPending}
        >
          <Plus className="h-4 w-4" />
          新建会话
        </Button>
      </div>
      <div className="flex-1 space-y-1 overflow-y-auto px-2 pb-2">
        {conversations.map((conv) => (
          <div
            key={conv.id}
            className={cn(
              "group flex items-center gap-1 rounded-md px-2 py-1.5 text-sm hover:bg-accent cursor-pointer",
              activeId === conv.id && "bg-accent",
            )}
            onClick={() => onSelect(conv.id)}
          >
            <MessageSquare className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
            {editingId === conv.id ? (
              <Input
                value={editingName}
                onChange={(e) => setEditingName(e.target.value)}
                className="h-7 flex-1 text-sm"
                autoFocus
                onClick={(e) => e.stopPropagation()}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    renameMutation.mutate({ id: conv.id, name: editingName });
                    setEditingId(null);
                  }
                  if (e.key === "Escape") setEditingId(null);
                }}
                onBlur={() => {
                  renameMutation.mutate({ id: conv.id, name: editingName });
                  setEditingId(null);
                }}
              />
            ) : (
              <span className="min-w-0 flex-1 truncate">
                {conv.name || "未命名会话"}
              </span>
            )}
            <button
              type="button"
              className="hidden text-muted-foreground hover:text-foreground group-hover:block"
              onClick={(e) => {
                e.stopPropagation();
                setEditingId(conv.id);
                setEditingName(conv.name ?? "");
              }}
              title="重命名"
            >
              <Pencil className="h-3.5 w-3.5" />
            </button>
            <button
              type="button"
              className="hidden text-muted-foreground hover:text-destructive group-hover:block"
              onClick={(e) => {
                e.stopPropagation();
                deleteMutation.mutate(conv.id);
              }}
              title="删除"
            >
              <Trash2 className="h-3.5 w-3.5" />
            </button>
          </div>
        ))}
        {conversations.length === 0 && (
          <div className="px-2 py-4 text-center text-xs text-muted-foreground">
            暂无会话，点击上方新建
          </div>
        )}
      </div>
    </div>
  );
}
