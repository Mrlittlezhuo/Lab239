import { useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ChevronDown,
  ChevronRight,
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
  listKbStatistics,
  listKnowledgeBases,
  searchDocs,
  updateKbInfo,
  uploadDocs,
} from "@/api/kb";
import type { KbSearchDoc, KbStatistics } from "@/api/kb";
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

/** 服务端 file_mtime 是 epoch 秒（Float），直接渲染会是一串裸数字。 */
function formatMtime(seconds?: number | null): string {
  if (typeof seconds !== "number" || !seconds) return "-";
  const d = new Date(seconds * 1000);
  if (Number.isNaN(d.getTime())) return "-";
  return d.toLocaleString("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
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
  const [editingInfoFor, setEditingInfoFor] = useState<string | null>(null);
  const [infoDraft, setInfoDraft] = useState("");
  const [chunkTarget, setChunkTarget] = useState<{ kb: string; file: string } | null>(
    null,
  );
  const [searchKb, setSearchKb] = useState<string | null>(null);
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState<KbSearchDoc[] | null>(null);
  const [uploadOverride, setUploadOverride] = useState(true);
  const [recreateKb, setRecreateKb] = useState<string | null>(null);

  const { progress, start: startRecreate, clear: clearProgress } =
    useRecreateProgress();

  const { data: kbs = [], isLoading } = useQuery({
    queryKey: ["knowledge-bases"],
    queryFn: listKnowledgeBases,
  });
  const { data: stats = [] } = useQuery({
    queryKey: ["kb-statistics"],
    queryFn: listKbStatistics,
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

  // 知识库名大小写不敏感（服务端查询一律 ilike）
  const statsByKb = useMemo(() => {
    const map = new Map<string, KbStatistics>();
    for (const s of stats) map.set(s.kb_name.toLowerCase(), s);
    return map;
  }, [stats]);

  const invalidateAll = () => {
    queryClient.invalidateQueries({ queryKey: ["knowledge-bases"] });
    queryClient.invalidateQueries({ queryKey: ["kb-statistics"] });
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
    mutationFn: (input: { kb: string; info: string }) =>
      updateKbInfo(input.kb, input.info),
    onSuccess: () => {
      toast.success("介绍已更新");
      setEditingInfoFor(null);
      invalidateAll();
    },
    onError: (err) => toast.error(err instanceof Error ? err.message : "更新失败"),
  });

  async function runSearchTest() {
    if (!searchKb) return;
    try {
      const results = await searchDocs({
        query: searchQuery,
        knowledge_base_name: searchKb,
        top_k: 5,
        score_threshold: 0,
      });
      setSearchResults(results);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "检索失败");
    }
  }

  function toggleExpand(name: string) {
    navigate(kbName === name ? "/kb" : `/kb/${encodeURIComponent(name)}`);
  }

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-2 border-b px-4 py-3">
        <h1 className="text-lg font-semibold">知识库管理</h1>
        <span className="text-xs text-muted-foreground">
          共 {kbs.length} 个公共知识库
        </span>
        <Button size="sm" className="ml-auto" onClick={() => setCreateOpen(true)}>
          <Plus className="h-4 w-4" />
          新建知识库
        </Button>
      </div>

      <div className="min-w-0 flex-1 space-y-3 overflow-y-auto p-4">
        <div className="rounded-md border border-amber-300 bg-amber-50 p-2 text-xs text-amber-800">
          公共知识库：上传、修改、删除都会影响所有用户。
        </div>

        {/* 重建进度放在列表外层：折叠卡片也不会丢掉进度条 */}
        {progress && recreateKb && (
          <div className="rounded-md border p-2 text-sm">
            <div className="mb-1 flex items-center justify-between">
              <span>
                正在重建「{recreateKb}」：{progress.finished}
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

        {isLoading && (
          <div className="py-10 text-center text-sm text-muted-foreground">
            正在读取知识库…
          </div>
        )}

        {!isLoading && kbs.length === 0 && (
          <div className="py-16 text-center text-muted-foreground">
            暂无知识库，点击右上角「新建知识库」创建。
          </div>
        )}

        {kbs.map((kb) => {
          const expanded = kbName === kb.kb_name;
          const stat = statsByKb.get(kb.kb_name.toLowerCase());
          return (
            <div key={kb.kb_name} className="rounded-lg border bg-card">
              <div className="space-y-2 p-3">
                <div className="flex items-center gap-2">
                  <FileText className="h-4 w-4 shrink-0 text-muted-foreground" />
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                      <h2 className="truncate font-semibold">{kb.kb_name}</h2>
                      <Badge variant="outline">{kb.vs_type ?? "-"}</Badge>
                      <Badge variant="outline">{kb.embed_model ?? "-"}</Badge>
                      {stat ? (
                        <span
                          className="text-xs text-muted-foreground"
                          title="统计取自已入库文件（knowledge_file），只放在 content 目录但未上传的文件不计入"
                        >
                          {stat.file_count} 个文件 · {stat.docs_count} 个片段 ·{" "}
                          {formatSize(stat.total_size)} · 更新于{" "}
                          {formatMtime(stat.latest_mtime)}
                        </span>
                      ) : (
                        <span
                          className="text-xs text-muted-foreground"
                          title="没取到统计：后端未更新，或该库暂无已入库文件（点「管理文件」看明细）"
                        >
                          {kb.file_count ?? 0} 个文件 · 统计未就绪
                        </span>
                      )}
                    </div>
                    <div className="mt-1 flex items-center gap-1">
                      <p
                        className={cn(
                          "min-w-0 flex-1 text-sm",
                          kb.kb_info
                            ? "text-foreground/80"
                            : "text-muted-foreground",
                        )}
                      >
                        {kb.kb_info || "未填写介绍"}
                      </p>
                      <Button
                        variant="ghost"
                        size="sm"
                        className="h-6 shrink-0 px-1 text-muted-foreground"
                        title={
                          editingInfoFor === kb.kb_name ? "取消编辑" : "编辑介绍"
                        }
                        aria-label={
                          editingInfoFor === kb.kb_name ? "取消编辑" : "编辑介绍"
                        }
                        onClick={() => {
                          setInfoDraft(kb.kb_info ?? "");
                          setEditingInfoFor((v) =>
                            v === kb.kb_name ? null : kb.kb_name,
                          );
                        }}
                      >
                        <Pencil className="h-3 w-3" />
                      </Button>
                    </div>
                  </div>
                  <div className="flex shrink-0 items-center gap-1">
                    <Button
                      size="sm"
                      title={`用「${kb.kb_name}」新建一个知识库对话`}
                      onClick={() =>
                        navigate(`/rag?kb=${encodeURIComponent(kb.kb_name)}`)
                      }
                    >
                      <MessageSquareText className="h-4 w-4" />
                      去对话
                    </Button>
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => toggleExpand(kb.kb_name)}
                    >
                      {expanded ? (
                        <ChevronDown className="h-4 w-4" />
                      ) : (
                        <ChevronRight className="h-4 w-4" />
                      )}
                      管理文件
                    </Button>
                  </div>
                </div>

                {editingInfoFor === kb.kb_name && (
                  <div className="flex gap-2">
                    <Textarea
                      value={infoDraft}
                      onChange={(e) => setInfoDraft(e.target.value)}
                      rows={2}
                      maxLength={200}
                      className="flex-1"
                      placeholder="知识库内容简介（最多 200 字），用于 Agent 选择知识库"
                    />
                    <Button
                      size="sm"
                      onClick={() =>
                        infoMutation.mutate({ kb: kb.kb_name, info: infoDraft })
                      }
                      disabled={infoMutation.isPending}
                    >
                      保存
                    </Button>
                  </div>
                )}
              </div>

              {expanded && (
                <div className="space-y-3 border-t p-3">
                  <div className="flex flex-wrap items-center gap-2">
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => {
                        setSearchKb(kb.kb_name);
                        setSearchResults(null);
                        setSearchQuery("");
                      }}
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
                        setRecreateKb(kb.kb_name);
                        void startRecreate(kb.kb_name);
                      }}
                      title="从 content 目录重建向量库"
                    >
                      <RefreshCw
                        className={cn(
                          "h-4 w-4",
                          progress?.running && recreateKb === kb.kb_name && "animate-spin",
                        )}
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
                    <label className="flex items-center gap-2 text-xs text-muted-foreground">
                      <Checkbox
                        checked={uploadOverride}
                        onCheckedChange={(v) => setUploadOverride(v === true)}
                      />
                      上传时覆盖同名文件
                    </label>
                    <Button
                      variant="ghost"
                      size="sm"
                      className="ml-auto text-destructive"
                      onClick={() => setConfirmDeleteKb(kb.kb_name)}
                    >
                      <Trash2 className="h-4 w-4" />
                      删除知识库
                    </Button>
                  </div>

                  <table className="w-full text-sm">
                    <thead>
                      <tr className="border-b text-left text-xs text-muted-foreground">
                        <th className="py-2">文件</th>
                        <th className="py-2">片段</th>
                        <th className="py-2">大小</th>
                        <th className="py-2">修改时间</th>
                        <th className="py-2 text-right">操作</th>
                      </tr>
                    </thead>
                    <tbody>
                      {files.map((f) => (
                        <tr key={f.file_name} className="border-b last:border-0">
                          <td className="py-2">
                            <div className="flex items-center gap-2">
                              <span className="break-all">{f.file_name}</span>
                              {f.in_db === false && (
                                <Badge variant="secondary" title="文件在 content 目录里，但尚未向量化入库，检索不到">
                                  未入库
                                </Badge>
                              )}
                            </div>
                          </td>
                          <td>{typeof f.docs_count === "number" ? f.docs_count : "-"}</td>
                          <td>{formatSize(f.file_size as number | undefined)}</td>
                          <td>{formatMtime(f.file_mtime)}</td>
                          <td className="text-right">
                            <Button
                              variant="ghost"
                              size="sm"
                              onClick={() =>
                                setChunkTarget({ kb: kb.kb_name, file: f.file_name })
                              }
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
                          <td
                            colSpan={5}
                            className="py-6 text-center text-muted-foreground"
                          >
                            暂无文件，点击「上传文件」添加。
                          </td>
                        </tr>
                      )}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          );
        })}
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
            <Textarea
              value={newInfo}
              maxLength={200}
              onChange={(e) => setNewInfo(e.target.value)}
            />
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
      <Dialog
        open={searchKb !== null}
        onOpenChange={(open) => !open && setSearchKb(null)}
      >
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle>检索测试：{searchKb}</DialogTitle>
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

      {chunkTarget && (
        <ChunkEditor
          kbName={chunkTarget.kb}
          fileName={chunkTarget.file}
          open={chunkTarget !== null}
          onOpenChange={(open) => !open && setChunkTarget(null)}
          onSaved={invalidateAll}
        />
      )}
    </div>
  );
}
