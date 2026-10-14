import { useQuery } from "@tanstack/react-query";

import { listMemoryRevisions } from "@/api/memory";
import { kindLabel, statusLabel } from "@/components/memory/categories";
import { Badge } from "@/components/ui/badge";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

/**
 * 某条记忆的修订历史（第三期设计方案 §4.2、§4.5）。
 *
 * 编辑与替代都会先写一条快照，因此"这条以前写过什么、什么时候被新决策
 * 替代"始终可查；列表按新→旧排列，只属于当前用户。
 */
export function MemoryRevisionsDialog({
  memoryId,
  memoryContent,
  open,
  onOpenChange,
}: {
  memoryId?: string;
  memoryContent?: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const { data, isFetching } = useQuery({
    queryKey: ["memory-revisions", memoryId],
    queryFn: () => listMemoryRevisions(memoryId as string),
    enabled: open && !!memoryId,
  });

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[85vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>修订历史</DialogTitle>
        </DialogHeader>

        <p className="text-sm">
          当前内容：<b>{memoryContent}</b>
        </p>

        {isFetching && (
          <p className="rounded-md border p-3 text-sm text-muted-foreground">
            正在读取修订历史…
          </p>
        )}

        {!isFetching && (data?.length ?? 0) === 0 && (
          <p className="rounded-md border p-3 text-sm text-muted-foreground">
            这条记忆还没有历史版本；编辑内容或被新决策替代后会在这里留下记录。
          </p>
        )}

        {data && data.length > 0 && (
          <ul className="space-y-2">
            {data.map((r) => (
              <li key={r.id} className="rounded-md border p-3 text-sm">
                <div className="mb-1 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                  <Badge variant="secondary">
                    {r.reason === "supersede" ? "被替代前" : "编辑前"}
                  </Badge>
                  <span>{kindLabel(r.kind)}</span>
                  <span>
                    {r.scope === "project"
                      ? `项目内${r.project_name ? `：${r.project_name}` : ""}`
                      : "个人"}
                  </span>
                  {r.create_time && (
                    <span>
                      {new Date(r.create_time).toLocaleString?.() ?? r.create_time}
                    </span>
                  )}
                </div>
                <p className="whitespace-pre-wrap">{r.content}</p>
              </li>
            ))}
          </ul>
        )}

        <p className="text-xs text-muted-foreground">
          历史版本只用于查看，不会被重新注入对话；状态为
          {statusLabel("superseded")}
          的旧记忆仍保留在列表里，但不会再被参考。
        </p>
      </DialogContent>
    </Dialog>
  );
}
