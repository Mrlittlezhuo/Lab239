import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Brain } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { toast } from "sonner";

import { listUsedPreferences, setPreferencesEnabled } from "@/api/memory";
import { categoryLabel } from "@/components/memory/categories";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { useChatPrefs } from "@/stores/chatPrefs";

/**
 * 聊天/知识库对话顶部的"已使用个人偏好"入口。
 *
 * 列表直接来自服务端 `GET /memories/preferences`——那就是本轮真正会注入的
 * 同一份内容，界面不会和实际注入不一致。
 */
export function MemoryUsageBadge({ conversationKey }: { conversationKey: string }) {
  const [open, setOpen] = useState(false);
  const prefs = useChatPrefs();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const offForThisChat = prefs.memoryOff[conversationKey] === true;

  const { data } = useQuery({
    queryKey: ["memory-preferences"],
    queryFn: () => listUsedPreferences(),
  });

  const masterMutation = useMutation({
    mutationFn: setPreferencesEnabled,
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["memory-preferences"] }),
    onError: (err) => toast.error(err instanceof Error ? err.message : "开关更新失败"),
  });

  const masterOn = data?.enabled ?? true;
  const used = data?.preferences ?? [];

  const label = !masterOn
    ? "个人记忆已关闭"
    : offForThisChat
      ? "本次不使用个人偏好"
      : used.length > 0
        ? "已使用个人偏好"
        : "无个人偏好";

  return (
    <>
      <div className="flex items-center gap-2">
        <Badge variant={masterOn && !offForThisChat ? "secondary" : "outline"}>
          <Brain className="mr-1 h-3 w-3" />
          {label}
        </Badge>
        <Button variant="outline" size="sm" onClick={() => setOpen(true)}>
          记忆
        </Button>
      </div>

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="max-h-[85vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>本次使用的个人偏好</DialogTitle>
          </DialogHeader>

          <div className="space-y-3">
            <div className="flex items-center justify-between">
              <div>
                <Label>个人记忆</Label>
                <p className="text-xs text-muted-foreground">
                  关闭后你的全部个人偏好都不再用于新对话；已保存的内容不会被删除。
                </p>
              </div>
              <Switch
                aria-label="个人记忆"
                checked={masterOn}
                onCheckedChange={(v) => masterMutation.mutate(v === true)}
              />
            </div>

            {used.length > 0 ? (
              <ul className="space-y-1 rounded-md border p-3 text-sm">
                {used.map((m) => (
                  <li key={m.id}>
                    <span className="text-muted-foreground">
                      {categoryLabel(m.category)}：
                    </span>
                    {m.content}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="rounded-md border p-3 text-sm text-muted-foreground">
                {masterOn
                  ? "本次没有可用的个人偏好。到“我的记忆”页新增后，新会话会自动使用。"
                  : "个人记忆已关闭，本次不会使用任何个人偏好。"}
              </p>
            )}

            {used.length > 0 && data && (
              <p className="text-xs text-muted-foreground">
                每次回答最多使用 {data.max_count} 条，优先使用回答语言、输出风格和与
                当前问题相关的背景；个人偏好只影响回答方式，不会改变知识库的检索范围、
                引用和文档原文。
              </p>
            )}

            <div className="flex items-center justify-between">
              <div>
                <Label>本次不使用个人偏好</Label>
                <p className="text-xs text-muted-foreground">
                  只作用于当前会话，不删除已保存的偏好；新开的会话仍会使用。
                </p>
              </div>
              <Switch
                aria-label="本次不使用个人偏好"
                checked={offForThisChat}
                onCheckedChange={(v) =>
                  prefs.setMemoryOff(conversationKey, v === true)
                }
              />
            </div>

            <Button
              variant="outline"
              className="w-full"
              onClick={() => {
                setOpen(false);
                navigate("/memory");
              }}
            >
              管理我的记忆
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
}
