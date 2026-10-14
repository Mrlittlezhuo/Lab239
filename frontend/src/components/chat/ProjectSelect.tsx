import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import { getConversation, setConversationProject } from "@/api/conversations";
import { listProjects } from "@/api/research";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useChatPrefs } from "@/stores/chatPrefs";

/** Radix Select 不接受空串作为 item value，用哨兵表示"不选项目"。 */
const NO_PROJECT = "__none__";

function messageOf(err: unknown, fallback: string): string {
  return err instanceof Error ? err.message : fallback;
}

/**
 * 会话的项目空间选择（第三期设计方案 §4.4）。
 *
 * 项目范围的记忆只在选定该项目的会话里、或在问题里明确提到该项目时才被
 * 参考，因此普通智能聊天默认不选项目就不会带进项目记忆。
 *
 * 会话还没建立时选择存在本地偏好里，随第一条消息一起提交；会话已建立时
 * 直接改服务端记录（`PATCH /conversations/{id}/project`），刷新后仍然有效。
 */
export function ProjectSelect({
  conversationId,
  conversationKey,
}: {
  conversationId?: string;
  /** 偏好存储键（"new" / "rag:new" / 会话 id） */
  conversationKey: string;
}) {
  const prefs = useChatPrefs();
  const queryClient = useQueryClient();

  const { data: projects = [] } = useQuery({
    queryKey: ["research-projects"],
    queryFn: () => listProjects(),
  });

  const { data: conversation } = useQuery({
    queryKey: ["conversation", conversationId],
    queryFn: () => getConversation(conversationId as string),
    enabled: !!conversationId,
  });

  const current = conversationId
    ? conversation?.project_id ?? null
    : prefs.projectChoice[conversationKey] ?? null;

  const mutation = useMutation({
    mutationFn: (projectId: string | null) =>
      setConversationProject(conversationId as string, projectId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["conversation", conversationId] });
      queryClient.invalidateQueries({ queryKey: ["conversations"] });
      queryClient.invalidateQueries({ queryKey: ["session-context", conversationId] });
      queryClient.invalidateQueries({ queryKey: ["memory-recall"] });
      toast.success("已切换本会话的项目空间；项目内记忆按新设置生效");
    },
    onError: (err) => toast.error(messageOf(err, "切换项目失败")),
  });

  function choose(value: string) {
    const projectId = value === NO_PROJECT ? null : value;
    if (conversationId) {
      mutation.mutate(projectId);
    } else if (projectId) {
      prefs.setProjectChoice(conversationKey, projectId);
    } else {
      prefs.clearProjectChoice(conversationKey);
    }
  }

  return (
    <div className="flex items-center gap-1">
      <Label className="text-xs text-muted-foreground">项目</Label>
      <Select value={current ?? NO_PROJECT} onValueChange={choose}>
        <SelectTrigger className="w-40">
          <SelectValue placeholder="不选项目" />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={NO_PROJECT}>不选项目</SelectItem>
          {projects.length === 0 && (
            <SelectItem value="__empty__">还没有项目</SelectItem>
          )}
          {projects.map((p) => (
            <SelectItem key={p.project_id} value={p.project_id}>
              {p.name}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      <span className="text-xs text-muted-foreground">
        {conversationId
          ? "项目内记忆只在本会话参考"
          : "新建会话时带上该项目"}
      </span>
    </div>
  );
}
