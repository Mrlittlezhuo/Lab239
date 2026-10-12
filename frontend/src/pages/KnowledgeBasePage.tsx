import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  FileText,
  MessageSquareText,
  Pencil,
  Plus,
  RefreshCw,
  Search,
  Trash2,
  Upload,
} from "lucide-react";
import { useNavigate, useParams } from "react-router-dom";
import { toast } from "sonner";

import {
  createKnowledgeBase,
  deleteDocs,
  deleteKnowledgeBase,
  listFiles,
  listKnowledgeBases,
  searchDocs,
  updateKbInfo,
  uploadDocs,
} from "@/api/kb";
import type { KbSearchDoc } from "@/api/kb";
import { getServerModels } from "@/api/server";
import { ChunkEditor } from "@/components/kb/ChunkEditor";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { useRecreateProgress } from "@/hooks/useRecreateProgress";
import { cn } from "@/lib/utils";

function formatSize(bytes?: number): string {
  if (typeof bytes !== "number") return "-";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export function KnowledgeBasePage() {
  const { kbName } = useParams<{ kbName?: string }>();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const fileRef = useRef<HTMLInputElement>(null);

  const [createOpen, setCreateOpen] = useState(false);
  const [newName, setNewName] = useState("");
  const [newInfo, setNewInfo] = useState("");
  const [confirmDeleteKb, setConfirmDeleteKb] = useState<string | null>(null);
  const [confirmDeleteFile, setConfirmDeleteFile] = useState<string | null>(null);
  const [editingInfo, setEditingInfo] = useState(false);
  const [infoDraft, setInfoDraft] = useState("");
  const [chunkFile, setChunkFile] = useState<string | null>(null);
  const [searchOpen, setSearchOpen] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState<KbSearchDoc[] | null>(null);
  const [uploadOverride, setUploadOverride] = useState(true);

  const { progress, start: startRecreate, clear: clearProgress } =
    useRecreateProgress();

  const { data: kbs = [], isLoading } = useQuery({
    queryKey: ["knowledge-bases"],
    queryFn: listKnowledgeBases,
  });
  const { data: serverModels } = useQuery({
    queryKey: ["server-models"],
    queryFn: getServerModels,
  });
  const { data: files = [] } = useQuery({
    queryKey: ["kb-files", kbName],
    queryFn: () => (kbName ? listFiles(kbName) : Promise.resolve([])),
    enabled: !!kbName,
  });

  const invalidateAll = () => {
    queryClient.invalidateQueries({ queryKey: ["knowledge-bases"] });
    queryClient.invalidateQueries({ queryKey: ["kb-files"] });
  };

  const createMutation = useMutation({
    mutationFn: () =>
      createKnowledgeBase({
        knowledge_base_name: newName,
        vector_store_type: serverModels?.kb_defaults.default_vs_type ?? "faiss",
        kb_info: newInfo,
        embed_model: serverModels?.default_embedding ?? "",
      }),
    onSuccess: () => {
      toast.success("知识库已创建（公共知识库，影响所有用户）");
      setCreateOpen(false);
      setNewName("");
      setNewInfo("");
      invalidateAll();
    },
    onError: (err) => toast.error(err instanceof Error ? err.message : "创建失败"),
  });

  const deleteKbMutation = useMutation({
    mutationFn: (name: string) => deleteKnowledgeBase(name),
    onSuccess: (_r, name) => {
      toast.success(`已删除知识库 ${name}`);
      if (kbName === name) navigate("/kb");
      invalidateAll();
    },
    onError: (err) => toast.error(err instanceof Error ? err.message : "删除失败"),
  });

  const deleteFileMutation = useMutation({
    mutationFn: (fileName: string) =>
      deleteDocs({
        knowledge_base_name: kbName!,
        file_names: [fileName],
        deleteContent: true,
      }),
    onSuccess: () => {
      toast.success("文件已删除");
      invalidateAll();
    },
    onError: (err) => toast.error(err instanceof Error ? err.message : "删除失败"),
  });

  const uploadMutation = useMutation({
    mutationFn: (filesToUpload: File[]) =>
      uploadDocs({
        knowledge_base_name: kbName!,
        files: filesToUpload,
        override: uploadOverride,
        toVectorStore: true,
      }),
    onSuccess: (res) => {
      const failed = (res.data as { failed_files?: Record<string, string> })
        ?.failed_files;
      if (failed && Object.keys(failed).length > 0) {
        toast.warning(`部分文件失败：${Object.keys(failed).join("、")}`);
      } else {
        toast.success("上传并向量化完成");
      }
      invalidateAll();
    },
    onError: (err) => toast.error(err instanceof Error ? err.message : "上传失败"),
  });

  const infoMutation = useMutation({
    mutationFn: (info: string) => updateKbInfo(kbName!, info),
    onSuccess: () => {
      toast.success("介绍已更新");
      setEditingInfo(false);
      invalidateAll();
    },
    onError: (err) => toast.error(err instanceof Error ? err.message : "更新失败"),
  });

  const currentKb = kbs.find((kb) => kb.kb_name === kbName);

  async function runSearchTest() {
    if (!kbName) return;
    try {
      const results = await searchDocs({
        query: searchQuery,
        knowledge_base_name: kbName,
        top_k: 5,
        score_threshold: 0,
      });
      setSearchResults(results);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "检索失败");
    }
  }

  return (
    <div className="flex h-full">
      <div className="flex w-60 shrink-0 flex-col border-r">
        <div className="p-2">
          <Button size="sm" className="w-full" onClick={() => setCreateOpen(true)}>
            <Plus className="h-4 w-4" />
            新建知识库
          </Button>
        </div>
        <div className="flex-1 space-y-1 overflow-y-auto px-2 pb-2">
          {kbs.map((kb) => (
            <div
              key={kb.kb_name}
              className={cn(
                "group flex cursor-pointer items-center gap-1 rounded-md px-2 py-1.5 text-sm hover:bg-accent",
                kbName === kb.name && "bg-accent",
              )}
              onClick={() => navigate(`/kb/${encodeURIComponent(kb.kb_name)}`)}
            >
              <FileText className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
              <span className="min-w-0 flex-1 truncate">{kb.kb_name}</span>
              {typeof kb.file_count === "number" && (
                <Badge variant="secondary">{kb.file_count}</Badge>
              )}
              <button
                type="button"
                className="hidden text-muted-foreground hover:text-destructive group-hover:block"
                title="删除知识库"
                onClick={(e) => {
                  e.stopPropagation();
                  setConfirmDeleteKb(kb.kb_name);
                }}
              >
                <Trash2 className="h-3.5 w-3.5" />
              </button>
            </div>
          ))}
          {!isLoading && kbs.length === 0 && (
            <div className="px-2 py-4 text-center text-xs text-muted-foreground">
              暂无知识库
            </div>
          )}
        </div>
      </div>

      <div className="min-w-0 flex-1 overflow-y-auto p-2">
        {!kbName ? (
          <div className="py-16 text-center text-muted-foreground">
            选择左侧知识库查看和管理文件
          </div>
        ) : (
          <div className="space-y-4">
            <div className="flex flex-wrap items-center gap-2">
              <h1 className="text-lg font-semibold">{kbName}</h1>
              <Badge variant="outline">{currentKb?.vs_type ?? "-"}</Badge>
              <Badge variant="outline">{currentKb?.embed_model ?? "-"}</Badge>
              <div className="ml-auto flex items-center gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setSearchOpen(true)}
                >
                  <Search className="h-4 w-4" />
                  检索测试
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  disabled={progress?.running}
                  onClick={() => {
                    clearProgress();
                    void startRecreate(kbName);
                  }}
                  title="从 content 目录重建向量库"
                >
                  <RefreshCw
                    className={cn("h-4 w-4", progress?.running && "animate-spin")}
                  />
                  重建向量库
                </Button>
                <input
                  ref={fileRef}
                  type="file"
                  multiple
                  className="hidden"
                  onChange={(e) => {
                    const picked = Array.from(e.target.files ?? []);
                    if (picked.length > 0) uploadMutation.mutate(picked);
                    if (fileRef.current) fileRef.current.value = "";
                  }}
                />
                <Button size="sm" onClick={() => fileRef.current?.click()}>
                  <Upload className="h-4 w-4" />
                  上传文件
                </Button>
              </div>
            </div>

            <div className="rounded-md border border-amber-300 bg-amber-50 p-2 text-xs text-amber-800">
              公共知识库：上传、修改、删除都会影响所有用户。
            </div>

            {progress && (
              <div className="rounded-md border p-2 text-sm">
                <div className="mb-1 flex items-center justify-between">
                  <span>
                    重建进度：{progress.finished}
                    {progress.total > 0 ? ` / ${progress.total}` : ""}
                    {progress.doc ? `（${progress.doc}）` : ""}
                  </span>
                  {progress.error && (
                    <span className="text-destructive">{progress.error}</span>
                  )}
                </div>
                {progress.total > 0 && (
                  <div className="h-2 rounded bg-muted">
                    <div
                      className="h-2 rounded bg-primary transition-all"
                      style={{
                        width: `${Math.min(100, (progress.finished / progress.total) * 100)}%`,
                      }}
                    />
                  </div>
                )}
              </div>
            )}

            <div className="space-y-1">
              <div className="flex items-center gap-2">
                <Label className="text-sm text-muted-foreground">介绍</Label>
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-6 text-xs"
                  onClick={() => {
                    setInfoDraft(currentKb?.kb_info ?? "");
                    setEditingInfo((v) => !v);
                  }}
                >
                  <Pencil className="h-3 w-3" />
                  {editingInfo ? "取消" : "编辑"}
                </Button>
              </div>
              {editingInfo ? (
                <div className="flex gap-2">
                  <Textarea
                    value={infoDraft}
                    onChange={(e) => setInfoDraft(e.target.value)}
                    rows={2}
                    className="flex-1"
                  />
                  <Button
                    size="sm"
                    onClick={() => infoMutation.mutate(infoDraft)}
                    disabled={infoMutation.isPending}
                  >
                    保存
                  </Button>
                </div>
              ) : (
                <p className="text-sm text-muted-foreground">
                  {currentKb?.kb_info || "（无介绍）"}
                </p>
              )}
            </div>

            <table className="w-full text-sm">
              <thead>
                <tr className="border-b text-left text-xs text-muted-foreground">
                  <th className="py-2">文件</th>
                  <th className="py-2">大小</th>
                  <th className="py-2">修改时间</th>
                  <th className="py-2 text-right">操作</th>
                </tr>
              </thead>
              <tbody>
                {files.map((f) => (
                  <tr key={f.file_name} className="border-b last:border-0">
                    <td className="py-2">{f.file_name}</td>
                    <td>{formatSize(f.file_size as number | undefined)}</td>
                    <td>{f.file_mtime ?? "-"}</td>
                    <td className="text-right">
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => setChunkFile(f.file_name)}
                      >
                        <MessageSquareText className="h-4 w-4" />
                        chunk
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        className="text-destructive"
                        onClick={() => setConfirmDeleteFile(f.file_name)}
                      >
                        <Trash2 className="h-4 w-4" />
                        删除
                      </Button>
                    </td>
                  </tr>
                ))}
                {files.length === 0 && (
                  <tr>
                    <td colSpan={4} className="py-6 text-center text-muted-foreground">
                      暂无文件
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
            <label className="flex items-center gap-2 text-xs text-muted-foreground">
              <Checkbox
                checked={uploadOverride}
                onCheckedChange={(v) => setUploadOverride(v === true)}
              />
              上传时覆盖同名文件
            </label>
          </div>
        )}
      </div>

      {/* 创建知识库 */}
      <Dialog open={createOpen} onOpenChange={setCreateOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>新建知识库（公共，影响所有用户）</DialogTitle>
          </DialogHeader>
          <div className="space-y-2">
            <Label>名称</Label>
            <Input value={newName} onChange={(e) => setNewName(e.target.value)} />
            <Label>介绍</Label>
            <Textarea value={newInfo} onChange={(e) => setNewInfo(e.target.value)} />
            <Button
              className="w-full"
              onClick={() => createMutation.mutate()}
              disabled={createMutation.isPending || !newName.trim()}
            >
              创建
            </Button>
          </div>
        </DialogContent>
      </Dialog>

      {/* 删除知识库（两步确认） */}
      <Dialog
        open={confirmDeleteKb !== null}
        onOpenChange={(open) => !open && setConfirmDeleteKb(null)}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>删除知识库？</DialogTitle>
          </DialogHeader>
          <p className="text-sm">
            将永久删除 <b>{confirmDeleteKb}</b> 的全部文件与向量索引，影响所有用户，不可恢复。
          </p>
          <div className="flex justify-end gap-2">
            <Button variant="outline" onClick={() => setConfirmDeleteKb(null)}>
              取消
            </Button>
            <Button
              variant="destructive"
              onClick={() => {
                if (confirmDeleteKb) deleteKbMutation.mutate(confirmDeleteKb);
                setConfirmDeleteKb(null);
              }}
            >
              确认删除
            </Button>
          </div>
        </DialogContent>
      </Dialog>

      {/* 删除文件（两步确认） */}
      <Dialog
        open={confirmDeleteFile !== null}
        onOpenChange={(open) => !open && setConfirmDeleteFile(null)}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>删除文件？</DialogTitle>
          </DialogHeader>
          <p className="text-sm">
            将从知识库中删除 <b>{confirmDeleteFile}</b>（含向量与源文件）。
          </p>
          <div className="flex justify-end gap-2">
            <Button variant="outline" onClick={() => setConfirmDeleteFile(null)}>
              取消
            </Button>
            <Button
              variant="destructive"
              onClick={() => {
                if (confirmDeleteFile) deleteFileMutation.mutate(confirmDeleteFile);
                setConfirmDeleteFile(null);
              }}
            >
              确认删除
            </Button>
          </div>
        </DialogContent>
      </Dialog>

      {/* 检索测试 */}
      <Dialog open={searchOpen} onOpenChange={setSearchOpen}>
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle>检索测试：{kbName}</DialogTitle>
          </DialogHeader>
          <div className="flex gap-2">
            <Input
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              placeholder="输入查询"
              onKeyDown={(e) => e.key === "Enter" && runSearchTest()}
            />
            <Button onClick={runSearchTest}>检索</Button>
          </div>
          <div className="max-h-96 space-y-2 overflow-y-auto">
            {(searchResults ?? []).map((doc) => (
              <div key={doc.id} className="rounded-md border p-2 text-sm">
                <div className="mb-1 text-xs text-muted-foreground">
                  {String(doc.metadata?.source ?? "")} ·{" "}
                  {typeof doc.score === "number" ? doc.score.toFixed(3) : "-"}
                </div>
                <p className="line-clamp-4 whitespace-pre-wrap">{doc.page_content}</p>
              </div>
            ))}
            {searchResults && searchResults.length === 0 && (
              <div className="py-4 text-center text-sm text-muted-foreground">
                无结果
              </div>
            )}
          </div>
        </DialogContent>
      </Dialog>

      {kbName && chunkFile && (
        <ChunkEditor
          kbName={kbName}
          fileName={chunkFile}
          open={chunkFile !== null}
          onOpenChange={(open) => !open && setChunkFile(null)}
          onSaved={invalidateAll}
        />
      )}
    </div>
  );
}
