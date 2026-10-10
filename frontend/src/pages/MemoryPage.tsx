import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, Trash2 } from "lucide-react";
import { toast } from "sonner";

import {
  createMemory,
  deleteMemory,
  getAutoMemory,
  listMemories,
  setAutoMemory,
  updateMemory,
} from "@/api/memory";
import { Badge } from "@/components/ui/badge";
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

const TYPES = ["preference", "fact", "goal", "important"];

export function MemoryPage() {
  const queryClient = useQueryClient();
  const [createOpen, setCreateOpen] = useState(false);
  const [content, setContent] = useState("");
  const [type, setType] = useState("fact");
  const [importance, setImportance] = useState(3);

  const { data: memories = [] } = useQuery({
    queryKey: ["memories"],
    queryFn: listMemories,
  });
  const { data: auto } = useQuery({
    queryKey: ["memory-auto"],
    queryFn: getAutoMemory,
  });

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["memories"] });
    queryClient.invalidateQueries({ queryKey: ["memory-auto"] });
  };

  const createMutation = useMutation({
    mutationFn: () => createMemory({ content, type, importance }),
    onSuccess: () => {
      toast.success("记忆已添加");
      setCreateOpen(false);
      setContent("");
      invalidate();
    },
    onError: (err) => toast.error(err instanceof Error ? err.message : "添加失败"),
  });

  const toggleMutation = useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) =>
      updateMemory(id, { enabled }),
    onSuccess: invalidate,
    onError: (err) => toast.error(err instanceof Error ? err.message : "更新失败"),
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => deleteMemory(id),
    onSuccess: invalidate,
    onError: (err) => toast.error(err instanceof Error ? err.message : "删除失败"),
  });

  const autoMutation = useMutation({
    mutationFn: (enabled: boolean) => setAutoMemory(enabled),
    onSuccess: invalidate,
    onError: (err) => toast.error(err instanceof Error ? err.message : "开关更新失败"),
  });

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-semibold">我的记忆</h1>
        <div className="flex items-center gap-3">
          <label className="flex items-center gap-2 text-sm">
            自动记忆
            <Switch
              checked={auto?.auto_memory ?? false}
              onCheckedChange={(v) => autoMutation.mutate(v === true)}
            />
          </label>
          <Button size="sm" onClick={() => setCreateOpen(true)}>
            <Plus className="h-4 w-4" />
            添加记忆
          </Button>
        </div>
      </div>

      <div className="space-y-2">
        {memories.map((m) => (
          <div key={m.id} className="rounded-lg border p-3">
            <div className="mb-1 flex items-center gap-2 text-xs text-muted-foreground">
              <Badge variant="secondary">{m.type}</Badge>
              <span>重要性 {m.importance}</span>
              <span>· 来源 {m.source}</span>
              <div className="ml-auto flex items-center gap-2">
                <Switch
                  checked={m.enabled}
                  onCheckedChange={(v) =>
                    toggleMutation.mutate({ id: m.id, enabled: v === true })
                  }
                />
                <button
                  type="button"
                  className="text-muted-foreground hover:text-destructive"
                  onClick={() => deleteMutation.mutate(m.id)}
                  title="删除"
                >
                  <Trash2 className="h-4 w-4" />
                </button>
              </div>
            </div>
            <p className={`text-sm ${m.enabled ? "" : "text-muted-foreground line-through"}`}>
              {m.content}
            </p>
          </div>
        ))}
        {memories.length === 0 && (
          <div className="py-10 text-center text-muted-foreground">
            还没有记忆。添加后聊天会自动参考这些内容。
          </div>
        )}
      </div>

      <Dialog open={createOpen} onOpenChange={setCreateOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>添加记忆</DialogTitle>
          </DialogHeader>
          <div className="space-y-3">
            <div className="space-y-1">
              <Label>内容</Label>
              <Textarea value={content} onChange={(e) => setContent(e.target.value)} />
            </div>
            <div className="flex gap-3">
              <div className="flex-1 space-y-1">
                <Label>类型</Label>
                <Select value={type} onValueChange={setType}>
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {TYPES.map((t) => (
                      <SelectItem key={t} value={t}>
                        {t}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <div className="flex-1 space-y-1">
                <Label>重要性（1-5）</Label>
                <Input
                  type="number"
                  min={1}
                  max={5}
                  value={importance}
                  onChange={(e) => setImportance(Number(e.target.value))}
                />
              </div>
            </div>
            <Button
              className="w-full"
              onClick={() => createMutation.mutate()}
              disabled={createMutation.isPending || !content.trim()}
            >
              保存
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
