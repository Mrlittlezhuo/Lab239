import { useEffect, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { toast } from "sonner";

import {
  createMemory,
  updateMemory,
  type Memory,
  type MemoryCategory,
  type MemoryKind,
  type MemoryScopeKind,
} from "@/api/memory";
import { listProjects } from "@/api/research";
import {
  MEMORY_CATEGORIES,
  MEMORY_KINDS,
  MEMORY_SAVE_NOTICE,
  MEMORY_SENSITIVE_NOTICE,
  kindLabel,
  toDateString,
} from "@/components/memory/categories";
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
import { Textarea } from "@/components/ui/textarea";

/** Radix Select 不接受空串作为 item value，用哨兵表示"不作为个人偏好"。 */
const NO_CATEGORY = "__none__";
/** 同上：项目范围但还没选项目。 */
const NO_PROJECT = "__none__";

export interface MemoryEditorDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** null = 新增；否则编辑该条记忆 */
  memory: Memory | null;
  onSaved: () => void;
}

export function MemoryEditorDialog({
  open,
  onOpenChange,
  memory,
  onSaved,
}: MemoryEditorDialogProps) {
  const [content, setContent] = useState("");
  const [category, setCategory] = useState<string>(MEMORY_CATEGORIES[0].value);
  const [kind, setKind] = useState<MemoryKind>("fact");
  const [scope, setScope] = useState<MemoryScopeKind>("personal");
  const [projectId, setProjectId] = useState<string>(NO_PROJECT);
  const [expiresOn, setExpiresOn] = useState(false);
  const [expiresDate, setExpiresDate] = useState("");
  const [enabled, setEnabled] = useState(true);

  const { data: projects = [] } = useQuery({
    queryKey: ["research-projects"],
    queryFn: () => listProjects(),
    enabled: open,
  });

  // 打开时按目标记忆重置表单（同一组件复用于新增与编辑）
  useEffect(() => {
    if (!open) return;
    setContent(memory?.content ?? "");
    setCategory(memory?.category ?? NO_CATEGORY);
    setKind(memory?.kind ?? "fact");
    setScope(memory?.scope ?? "personal");
    setProjectId(memory?.project_id ?? NO_PROJECT);
    setExpiresOn(!!memory?.expires_at);
    setExpiresDate(toDateString(memory?.expires_at));
    setEnabled(memory?.enabled ?? true);
  }, [open, memory]);

  const projectMissing = scope === "project" && projectId === NO_PROJECT;

  const save = useMutation({
    mutationFn: async () => {
      const text = content.trim();
      if (!text) throw new Error("内容不能为空");
      if (projectMissing) throw new Error("项目范围记忆必须先选择项目");
      const nextCategory =
        category === NO_CATEGORY ? null : (category as MemoryCategory);
      const expiresAt = expiresOn ? expiresDate : "";
      if (memory) {
        // 类别/有效期必须显式传空串才能清除（服务端把 undefined 当作"不改动"）
        await updateMemory(memory.id, {
          content: text,
          category: nextCategory ?? "",
          enabled,
          kind,
          scope,
          project_id: scope === "project" ? projectId : "",
          expires_at: expiresAt,
        });
      } else {
        await createMemory({
          content: text,
          category: nextCategory,
          // 手动保存的偏好默认按 preference 归类，供服务端召回排序加权
          type: nextCategory ? "preference" : "fact",
          importance: 3,
          source: "user",
          kind,
          scope,
          project_id: scope === "project" ? projectId : null,
          expires_at: expiresOn ? expiresDate : null,
        });
      }
    },
    onSuccess: () => {
      toast.success(memory ? "记忆已更新；旧内容已存入修订历史" : "记忆已添加");
      onSaved();
      onOpenChange(false);
    },
    // 服务端会拒绝明显敏感的内容或非法类别/范围（400），这里如实展示原因
    onError: (err) => toast.error(err instanceof Error ? err.message : "保存失败"),
  });

  const hint = MEMORY_CATEGORIES.find((c) => c.value === category)?.hint;
  const kindHint = MEMORY_KINDS.find((k) => k.value === kind)?.hint;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[85vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{memory ? "编辑记忆" : "新增一条记忆"}</DialogTitle>
        </DialogHeader>
        <div className="space-y-3">
          <div className="space-y-1">
            <Label>类别</Label>
            <Select value={category} onValueChange={setCategory}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {MEMORY_CATEGORIES.map((c) => (
                  <SelectItem key={c.value} value={c.value}>
                    {c.label}
                  </SelectItem>
                ))}
                <SelectItem value={NO_CATEGORY}>不作为个人偏好（仅长期记忆）</SelectItem>
              </SelectContent>
            </Select>
            {hint && <p className="text-xs text-muted-foreground">{hint}</p>}
          </div>

          <div className="space-y-1">
            <Label>记忆类别</Label>
            <Select value={kind} onValueChange={(v) => setKind(v as MemoryKind)}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {MEMORY_KINDS.map((k) => (
                  <SelectItem key={k.value} value={k.value}>
                    {k.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            {kindHint && (
              <p className="text-xs text-muted-foreground">{kindHint}</p>
            )}
          </div>

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
                <SelectItem value="personal">个人（所有会话都可用）</SelectItem>
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
                <p className="text-xs text-destructive">
                  请先选择项目，否则无法保存为项目范围记忆。
                </p>
              )}
            </div>
          )}

          <div className="space-y-1">
            <Label htmlFor="memory-content">内容</Label>
            <Textarea
              id="memory-content"
              rows={4}
              value={content}
              onChange={(e) => setContent(e.target.value)}
              placeholder="例：默认中文回答，先给结论再解释"
            />
          </div>

          <div className="flex items-center justify-between">
            <div>
              <Label>有效期</Label>
              <p className="text-xs text-muted-foreground">
                到期后不再自动参考，记录仍保留，可随时改回长期。
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

          <div className="flex items-center justify-between">
            <Label>启用</Label>
            <Switch checked={enabled} onCheckedChange={(v) => setEnabled(v === true)} />
          </div>

          {memory && (
            <p className="text-xs text-muted-foreground">
              当前状态：{kindLabel(memory.kind)} ·{" "}
              {memory.scope === "project" ? "项目内" : "个人"} ·{" "}
              {memory.status === "superseded"
                ? "已被新决策替代，不再参考"
                : memory.status === "expired"
                  ? "已过期，不再自动参考"
                  : memory.enabled
                    ? "启用中"
                    : "已停用"}
              。编辑会保留旧内容作为历史版本。
            </p>
          )}

          <p className="text-xs text-muted-foreground">{MEMORY_SAVE_NOTICE}</p>
          <p className="text-xs text-destructive">{MEMORY_SENSITIVE_NOTICE}</p>

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
              onClick={() => save.mutate()}
              disabled={save.isPending || !content.trim() || projectMissing}
            >
              保存
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
