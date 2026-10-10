import { useEffect, useMemo, useState } from "react";
import { ChevronLeft, ChevronRight } from "lucide-react";
import { toast } from "sonner";

import { searchDocs, updateDocs } from "@/api/kb";
import type { KbSearchDoc } from "@/api/kb";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Textarea } from "@/components/ui/textarea";

const PAGE_SIZE = 50;

/**
 * 文件 chunk 编辑器：search_docs(file_name) 可能返回上千 chunk，
 * 客户端分页 50/页；保存时只提交被修改过的 chunk（按 id diff）。
 */
export function ChunkEditor({
  kbName,
  fileName,
  open,
  onOpenChange,
  onSaved,
}: {
  kbName: string;
  fileName: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSaved: () => void;
}) {
  const [docs, setDocs] = useState<KbSearchDoc[]>([]);
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [page, setPage] = useState(0);

  useEffect(() => {
    if (!open) return;
    setLoading(true);
    setEdits({});
    setPage(0);
    searchDocs({ knowledge_base_name: kbName, file_name: fileName })
      .then(setDocs)
      .catch((err) =>
        toast.error(err instanceof Error ? err.message : "加载 chunk 失败"),
      )
      .finally(() => setLoading(false));
  }, [open, kbName, fileName]);

  const changedCount = Object.keys(edits).length;
  const pageCount = Math.max(1, Math.ceil(docs.length / PAGE_SIZE));
  const pageDocs = useMemo(
    () => docs.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE),
    [docs, page],
  );

  async function save() {
    if (changedCount === 0) {
      onOpenChange(false);
      return;
    }
    setSaving(true);
    try {
      const payload = docs
        .filter((d) => edits[d.id] !== undefined)
        .map((d) => ({
          page_content: edits[d.id],
          metadata: d.metadata,
        }));
      await updateDocs({
        knowledge_base_name: kbName,
        file_names: [fileName],
        docs: { [fileName]: { docs: payload } },
      });
      toast.success(`已更新 ${changedCount} 个 chunk，正在重新向量化`);
      onSaved();
      onOpenChange(false);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "保存失败");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-3xl">
        <DialogHeader>
          <DialogTitle>
            编辑 chunk：{fileName}（{docs.length} 个，已改 {changedCount} 个）
          </DialogTitle>
        </DialogHeader>
        {loading ? (
          <div className="py-8 text-center text-muted-foreground">加载中…</div>
        ) : (
          <>
            <div className="max-h-[55vh] space-y-3 overflow-y-auto">
              {pageDocs.map((doc) => (
                <div key={doc.id} className="rounded-md border p-2">
                  <div className="mb-1 flex items-center justify-between text-xs text-muted-foreground">
                    <span>{String(doc.metadata?.doc_id ?? doc.id)}</span>
                    {typeof doc.score === "number" && <span>{doc.score.toFixed(3)}</span>}
                  </div>
                  <Textarea
                    value={edits[doc.id] ?? doc.page_content}
                    onChange={(e) =>
                      setEdits((prev) => ({ ...prev, [doc.id]: e.target.value }))
                    }
                    rows={5}
                  />
                </div>
              ))}
              {docs.length === 0 && (
                <div className="py-8 text-center text-muted-foreground">
                  没有找到 chunk
                </div>
              )}
            </div>
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2 text-sm">
                <Button
                  variant="outline"
                  size="icon"
                  className="h-8 w-8"
                  disabled={page === 0}
                  onClick={() => setPage((p) => p - 1)}
                >
                  <ChevronLeft className="h-4 w-4" />
                </Button>
                <span>
                  {page + 1} / {pageCount}
                </span>
                <Button
                  variant="outline"
                  size="icon"
                  className="h-8 w-8"
                  disabled={page >= pageCount - 1}
                  onClick={() => setPage((p) => p + 1)}
                >
                  <ChevronRight className="h-4 w-4" />
                </Button>
              </div>
              <Button onClick={save} disabled={saving}>
                {saving ? "保存中…" : `保存修改（${changedCount}）`}
              </Button>
            </div>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}
