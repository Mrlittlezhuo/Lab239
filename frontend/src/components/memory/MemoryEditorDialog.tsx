import { useEffect, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { toast } from "sonner";

import { createMemory, updateMemory, type Memory, type MemoryCategory } from "@/api/memory";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
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
import {
  MEMORY_CATEGORIES,
  MEMORY_SAVE_NOTICE,
  MEMORY_SENSITIVE_NOTICE,
} from "@/components/memory/categories";

/** Radix Select 不接受空串作为 item value，用哨兵表示"不作为个人偏好"。 */
const NO_CATEGORY = "__none__";

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
  const [enabled, setEnabled] = useState(true);

  // 打开时按目标记忆重置表单（同一组件复用于新增与编辑）
  useEffect(() => {
    if (!open) return;
    setContent(memory?.content ?? "");
    setCategory(memory?.category ?? NO_CATEGORY);
    setEnabled(memory?.enabled ?? true);
  }, [open, memory]);

  const save = useMutation({
    mutationFn: async () => {
      const text = content.trim();
      if (!text) throw new Error("内容不能为空");
      const nextCategory =
        category === NO_CATEGORY ? null : (category as MemoryCategory);
      if (memory) {
        // 类别必须显式传空串才能清除（服务端把 undefined 当作"不改动"）
        await updateMemory(memory.id, {
          content: text,
          category: nextCategory ?? "",
          enabled,
        });
      } else {
        await createMemory({
          content: text,
          category: nextCategory,
          // 手动保存的偏好默认按 preference 归类，供服务端召回排序加权
          type: nextCategory ? "preference" : "fact",
          importance: 3,
          source: "user",
        });
      }
    },
    onSuccess: () => {
      toast.success(memory ? "记忆已更新" : "记忆已添加");
      onSaved();
      onOpenChange(false);
    },
    // 服务端会拒绝明显敏感的内容（400），这里如实展示原因，不静默吞掉
    onError: (err) => toast.error(err instanceof Error ? err.message : "保存失败"),
  });

  const hint = MEMORY_CATEGORIES.find((c) => c.value === category)?.hint;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
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
            <Label>启用</Label>
            <Switch checked={enabled} onCheckedChange={(v) => setEnabled(v === true)} />
          </div>

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
              disabled={save.isPending || !content.trim()}
            >
              保存
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
