import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, X } from "lucide-react";
import { toast } from "sonner";

import {
  addProgressNote,
  createProgress,
  listProgress,
} from "@/api/supervision";
import type { ProgressUpdate, ProjectDetail, Task } from "@/api/supervision";
import { AlertPanel, errorText } from "@/components/supervision/AlertPanel";
import {
  buildLinksPayload,
  EMPTY_PROGRESS_DRAFT,
  parseLinkRows,
  progressHasContent,
  ROLE_LABEL,
  type LinkDraft,
  type ProgressDraft,
} from "@/components/supervision/execution/logic";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";

const FIELDS: { key: keyof ProgressDraft; label: string; placeholder: string }[] = [
  { key: "done", label: "已完成的工作", placeholder: "这段时间做了什么" },
  { key: "results", label: "得到的结果", placeholder: "指标、现象、结论" },
  { key: "materials", label: "产生的材料", placeholder: "代码、数据、图表放在哪里" },
  { key: "problems", label: "遇到的问题", placeholder: "卡在哪里、试过什么" },
  { key: "next_steps", label: "下一步计划", placeholder: "接下来准备做什么" },
];

/**
 * 进展记录（§4.8）：随时记录，不触发审核、不算正式提交；
 * 原始进展不可改写，只能追加只读补充说明。
 */
export function ProgressSection({
  project,
  task,
  isMentor,
}: {
  project: ProjectDetail;
  task: Task | null;
  isMentor: boolean;
}) {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState<ProgressDraft>(EMPTY_PROGRESS_DRAFT);
  const [links, setLinks] = useState<LinkDraft[]>([]);
  const [noting, setNoting] = useState<ProgressUpdate | null>(null);
  const [note, setNote] = useState("");

  const progress = useQuery({
    queryKey: ["supervision", "progress", project.project_id],
    queryFn: () => listProgress(project.project_id),
  });

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: ["supervision", "progress"] });

  const create = useMutation({
    mutationFn: () =>
      createProgress(project.project_id, {
        task_ref: task?.code ?? "",
        ...draft,
        links: buildLinksPayload(links),
      }),
    onSuccess: () => {
      toast.success("已保存进展记录（不是正式提交）");
      setDraft(EMPTY_PROGRESS_DRAFT);
      setLinks([]);
      invalidate();
    },
    onError: (error) => toast.error(errorText(error)),
  });

  const addNote = useMutation({
    mutationFn: () => addProgressNote(project.project_id, noting?.progress_id ?? "", { note }),
    onSuccess: () => {
      toast.success("已追加补充说明");
      setNoting(null);
      setNote("");
      invalidate();
    },
    onError: (error) => toast.error(errorText(error)),
  });

  const rows = (progress.data ?? []).filter(
    (item) => task && item.task_id === task.task_id,
  );

  return (
    <section className="space-y-3 rounded-md border p-3">
      <div className="flex items-center justify-between gap-2">
        <h2 className="text-sm font-semibold">进展记录</h2>
        <span className="text-xs text-muted-foreground">
          进展只是记录，不会触发导师审核；正式提交请在下方进行。
        </span>
      </div>

      {!isMentor && task && (
        <form
          className="space-y-2 rounded-md border bg-muted/20 p-3"
          onSubmit={(event) => {
            event.preventDefault();
            create.mutate();
          }}
        >
          <div className="grid gap-2 md:grid-cols-2">
            {FIELDS.map((field) => (
              <div key={field.key} className="space-y-1">
                <Label htmlFor={`progress-${field.key}`}>{field.label}</Label>
                <Textarea
                  id={`progress-${field.key}`}
                  rows={2}
                  value={draft[field.key]}
                  placeholder={field.placeholder}
                  onChange={(event) =>
                    setDraft({ ...draft, [field.key]: event.target.value })
                  }
                />
              </div>
            ))}
          </div>
          <div className="space-y-1">
            <Label>相关链接</Label>
            {links.map((link, index) => (
              <div key={index} className="flex gap-2">
                <Input
                  value={link.title}
                  placeholder="标题（可留空）"
                  onChange={(event) =>
                    setLinks(
                      links.map((item, i) =>
                        i === index ? { ...item, title: event.target.value } : item,
                      ),
                    )
                  }
                />
                <Input
                  value={link.url}
                  placeholder="https://…"
                  onChange={(event) =>
                    setLinks(
                      links.map((item, i) =>
                        i === index ? { ...item, url: event.target.value } : item,
                      ),
                    )
                  }
                />
                <Button
                  type="button"
                  size="icon"
                  variant="ghost"
                  aria-label="删除链接"
                  onClick={() => setLinks(links.filter((_, i) => i !== index))}
                >
                  <X className="h-4 w-4" />
                </Button>
              </div>
            ))}
            <Button
              type="button"
              size="sm"
              variant="outline"
              onClick={() => setLinks([...links, { title: "", url: "" }])}
            >
              <Plus className="mr-1 h-4 w-4" />
              添加链接
            </Button>
          </div>
          <div className="flex justify-end">
            <Button
              type="submit"
              disabled={create.isPending || !progressHasContent(draft)}
            >
              保存进展记录
            </Button>
          </div>
        </form>
      )}

      {progress.isLoading && <p className="text-sm text-muted-foreground">加载中…</p>}
      {progress.isError && (
        <AlertPanel tone="error" title="进展加载失败" description={errorText(progress.error)} />
      )}
      {!task && !progress.isLoading && (
        <AlertPanel title="请先选择一个任务" description="进展记录绑定到具体任务。" />
      )}
      {task && !progress.isLoading && rows.length === 0 && (
        <AlertPanel
          title={isMentor ? "该任务还没有进展记录" : "还没有进展记录"}
          description={
            isMentor
              ? "学生尚未在该任务下记录进展。"
              : "先把做过的事、结果和问题写下来，导师能看到。"
          }
        />
      )}

      <ul className="space-y-2">
        {rows.map((item) => (
          <li key={item.progress_id} className="rounded-md border p-3">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-medium">{item.code}</span>
              <Badge variant="secondary">{ROLE_LABEL[item.author_role] ?? item.author_role}</Badge>
              <span className="text-xs text-muted-foreground">
                {item.create_time ? item.create_time.slice(0, 16).replace("T", " ") : ""}
              </span>
            </div>
            {FIELDS.filter((field) => item[field.key]).map((field) => (
              <p key={field.key} className="mt-2 whitespace-pre-wrap text-sm">
                <span className="text-muted-foreground">{field.label}：</span>
                {item[field.key]}
              </p>
            ))}
            {parseLinkRows(item.links).length > 0 && (
              <ul className="mt-2 space-y-1 text-sm">
                {parseLinkRows(item.links).map((link) => (
                  <li key={link.url}>
                    <a
                      className="break-all text-primary underline"
                      href={link.url}
                      target="_blank"
                      rel="noreferrer"
                    >
                      {link.title || link.url}
                    </a>
                  </li>
                ))}
              </ul>
            )}

            {item.notes.length > 0 && (
              <ul className="mt-2 space-y-1 border-t pt-2">
                {item.notes.map((row) => (
                  <li key={row.note_id} className="text-sm text-muted-foreground">
                    <span className="whitespace-pre-wrap">{row.note}</span>
                    <span className="ml-2 text-xs">
                      —— {ROLE_LABEL[row.author_role] ?? row.author_role}
                      {row.create_time ? ` · ${row.create_time.slice(0, 16).replace("T", " ")}` : ""}
                    </span>
                  </li>
                ))}
              </ul>
            )}

            <div className="mt-2 border-t pt-2">
              {noting?.progress_id === item.progress_id ? (
                <div className="space-y-2">
                  <Textarea
                    rows={2}
                    value={note}
                    onChange={(event) => setNote(event.target.value)}
                    placeholder="补充说明（只追加，不改动原始进展）"
                  />
                  <div className="flex gap-2">
                    <Button
                      size="sm"
                      onClick={() => addNote.mutate()}
                      disabled={addNote.isPending || !note.trim()}
                    >
                      追加说明
                    </Button>
                    <Button size="sm" variant="outline" onClick={() => setNoting(null)}>
                      取消
                    </Button>
                  </div>
                </div>
              ) : (
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => {
                    setNoting(item);
                    setNote("");
                  }}
                >
                  补充说明
                </Button>
              )}
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}
