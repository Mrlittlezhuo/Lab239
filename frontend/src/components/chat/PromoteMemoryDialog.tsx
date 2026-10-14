import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import { promoteMemory, type MemoryKind, type MemoryScopeKind } from "@/api/memory";
import type { PromotionCandidate } from "@/api/sessionContext";
import { listProjects } from "@/api/research";
import { MEMORY_KINDS, kindLabel } from "@/components/memory/categories";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";

const NO_PROJECT = "__none__";

function messageOf(err: unknown, fallback: string): string {
  return err instanceof Error ? err.message : fallback;
}

/**
 * 把会话里的已确认事项保存为跨会话记忆（第三期设计方案 §4.3）。
 *
 * 范围、类别与有效期都由用户选定：系统只提出候选，绝不自动升级——
 * 点「取消」不会发出任何请求，也不会写入记忆（P3-13）。
 * 同一范围同一主题上已有的旧决策会被服务端标记为"已被替代"，
 * 记录保留、修订链完整（P3-07）。
 */
export function PromoteMemoryDialog({
  open,
  onOpenChange,
  content,
  conversationId,
  candidate,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  content: string;
  conversationId?: string;
  /** 服务端提出的候选（带推荐类别/范围）；手动保存时不传 */
  candidate?: PromotionCandidate | null;
}) {
  const queryClient = useQueryClient();
  const [kind, setKind] = useState<MemoryKind>("decision");
  const [scope, setScope] = useState<MemoryScopeKind>("personal");
  const [projectId, setProjectId] = useState<string>(NO_PROJECT);
  const [expiresOn, setExpiresOn] = useState(false);
  const [expiresDate, setExpiresDate] = useState("");

  const { data: projects = [] } = useQuery({
    queryKey: ["research-projects"],
    queryFn: () => listProjects(),
    enabled: open,
  });

  // 每次打开都按候选的推荐值重置：推荐只是默认值，用户可以改
  useEffect(() => {
    if (!open) return;
    setKind(candidate?.recommended_kind ?? "decision");
    setScope(candidate?.recommended_scope ?? "personal");
    setProjectId(candidate?.project_id ?? NO_PROJECT);
    setExpiresOn(false);
    setExpiresDate("");
  }, [open, candidate]);

  const projectMissing = scope === "project" && projectId === NO_PROJECT;

  const save = useMutation({
    mutationFn: () => {
      if (projectMissing) throw new Error("项目范围记忆必须先选择项目");
      return promoteMemory({
        content,
        kind,
        scope,
        project_id: scope === "project" ? projectId : null,
        expires_at: expiresOn ? expiresDate : null,
        conversation_id: conversationId ?? null,
      });
    },
    onSuccess: (res) => {
      queryClient.invalidateQueries({ queryKey: ["session-context"] });
      queryClient.invalidateQueries({ queryKey: ["memories"] });
      queryClient.invalidateQueries({ queryKey: ["memory-recall"] });
      toast.success(
        res.superseded_ids.length > 0
          ? "已保存为跨会话记忆；同主题的旧决策已标记为已被替代"
          : "已保存为跨会话记忆；新会话里遇到相关问题会参考它",
      );
      onOpenChange(false);
    },
    onError: (err) => toast.error(messageOf(err, "保存失败")),
  });

  const kindHint = MEMORY_KINDS.find((k) => k.value === kind)?.hint;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>保存为跨会话记忆</DialogTitle>
        </DialogHeader>

        <p className="whitespace-pre-wrap rounded-md border p-3 text-sm">
          {content}
        </p>
        {candidate?.reason && (
          <p className="text-xs text-muted-foreground">
            系统建议：{candidate.reason}
          </p>
        )}

        <div className="space-y-3">
          <div className="space-y-1">
            <Label>范围</Label>
            <Select
              value={scope}
              onValueChange={(v) => setScope(v as MemoryScopeKind)}
            >
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="personal">个人（我的所有会话）</SelectItem>
                <SelectItem value="project">仅某个项目内</SelectItem>
              </SelectContent>
            </Select>
            <p className="text-xs text-muted-foreground">
              项目范围的记忆只在该项目的会话里、或在问题里明确提到该项目时才被参考。
            </p>
          </div>

          {scope === "project" && (
            <div className="space-y-1">
              <Label>项目</Label>
              <Select value={projectId} onValueChange={setProjectId}>
                <SelectTrigger>
                  <SelectValue placeholder="选择项目" />
                </SelectTrigger>
                <SelectContent>
                  {projects.length === 0 && (
                    <SelectItem value={NO_PROJECT}>还没有可用项目</SelectItem>
                  )}
                  {projects.map((p) => (
                    <SelectItem key={p.project_id} value={p.project_id}>
                      {p.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              {projectMissing && (
                <p className="text-xs text-destructive">请先选择项目。</p>
              )}
            </div>
          )}

          <div className="space-y-1">
            <Label>类别</Label>
            <Select value={kind} onValueChange={(v) => setKind(v as MemoryKind)}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {MEMORY_KINDS.filter((k) => k.value !== "fact").map((k) => (
                  <SelectItem key={k.value} value={k.value}>
                    {k.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            {kindHint && <p className="text-xs text-muted-foreground">{kindHint}</p>}
          </div>

          <div className="flex items-center justify-between">
            <div>
              <Label>有效期</Label>
              <p className="text-xs text-muted-foreground">
                到期后不再自动参考，记录仍保留。
              </p>
            </div>
            <Switch
              aria-label="设置有效期"
              checked={expiresOn}
              onCheckedChange={(v) => setExpiresOn(v === true)}
            />
          </div>
          {expiresOn && (
            <Input
              type="date"
              aria-label="有效期截止日期"
              value={expiresDate}
              onChange={(e) => setExpiresDate(e.target.value)}
            />
          )}

          <p className="text-xs text-muted-foreground">
            保存后类别为「{kindLabel(kind)}」、范围为
            {scope === "project" ? "项目内" : "个人"}。
            同主题上已有的旧决策会被标记为已被替代，历史内容仍可在「我的记忆」里查看。
          </p>

          <div className="flex gap-2">
            <Button
              variant="outline"
              className="flex-1"
              onClick={() => onOpenChange(false)}
            >
              取消
            </Button>
            <Button
              className="flex-1"
              disabled={save.isPending || projectMissing}
              onClick={() => save.mutate()}
            >
              确认保存
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
