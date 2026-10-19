import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import {
  confirmBrief,
  createBrief,
  deleteBrief,
  getBrief,
  listBriefs,
  listSharedBriefs,
  rejectBrief,
  updateBrief,
} from "@/api/supervision";
import type { BriefStatus, ProjectDetail, ResearchBrief } from "@/api/supervision";
import { AlertPanel, errorText } from "@/components/supervision/AlertPanel";
import {
  briefContentPayload,
  briefRefOf,
  canConfirmBrief,
  canDiscardBrief,
  canEditBrief,
  canRejectBrief,
  changedContentFields,
  confirmBlockedReason,
  contentFromBrief,
  editWouldForkNote,
  emptyBriefContent,
  entryTypeLabel,
  formatTime,
  isContentEmpty,
  kindLabel,
  roleLabel,
  sharedCannotRemoveNote,
  sharedVersionNote,
  shownVersionNote,
  statusLabel,
  BRIEF_ENTRY_TYPE_LABEL,
  BRIEF_STATUS_LABEL,
  type BriefContentDraft,
} from "@/components/supervision/research/briefModel";
import { BriefContentEditor, BriefContentView } from "@/components/supervision/research/BriefFields";
import { BriefSourceMessages } from "@/components/supervision/research/BriefSourceMessages";
import { BriefVersionHistory } from "@/components/supervision/research/BriefVersionHistory";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { cn } from "@/lib/utils";

type Scope = "mine" | "shared" | "all";

const SCOPES: { key: Scope; label: string }[] = [
  { key: "mine", label: "我的" },
  { key: "shared", label: "共享给我的" },
  { key: "all", label: "全部" },
];

/** Radix Select 不接受空串作为 item value，用哨兵表示「不限状态」。 */
const ANY_STATUS = "__any__";

const STATUS_ORDER: BriefStatus[] = ["draft", "confirmed", "rejected", "superseded"];

interface EditorState {
  title: string;
  entryType: string;
  content: BriefContentDraft;
  baseTitle: string;
  baseContent: BriefContentDraft;
}

/**
 * 结构化研究定义（ResearchBrief）面板。
 *
 * 版本与可见性口径全部来自后端字段（`status` / `shared_version` /
 * `shown_version` / `is_owner` / `can_edit`），前端不拼混合状态：
 * 草稿只对创建者可见；确认后当前版本进入项目共享区，另一方只读得到那一版；
 * 已确认的定义再改内容会生成新版本并把状态退回草稿，不原地覆盖。
 */
export function BriefPanel({
  project,
  refresh,
}: {
  project: ProjectDetail;
  refresh: () => void;
}) {
  const queryClient = useQueryClient();
  const [scope, setScope] = useState<Scope>("mine");
  const [statusFilter, setStatusFilter] = useState(ANY_STATUS);
  const [selectedRef, setSelectedRef] = useState<string | null>(null);
  const [editor, setEditor] = useState<EditorState | null>(null);
  const [creating, setCreating] = useState(false);
  const [createForm, setCreateForm] = useState<{
    title: string;
    entryType: string;
    content: BriefContentDraft;
  }>({ title: "", entryType: "mixed", content: emptyBriefContent() });
  const [confirmNote, setConfirmNote] = useState("");
  const [reason, setReason] = useState("");
  const [action, setAction] = useState<"reject" | "discard" | null>(null);

  const briefs = useQuery({
    queryKey: ["supervision", "briefs", project.project_id, scope, statusFilter],
    queryFn: () =>
      listBriefs(project.project_id, {
        scope,
        status: statusFilter === ANY_STATUS ? undefined : statusFilter,
      }),
  });

  const shared = useQuery({
    queryKey: ["supervision", "shared-briefs", project.project_id],
    queryFn: () => listSharedBriefs(project.project_id),
  });

  const detail = useQuery({
    queryKey: ["supervision", "brief", project.project_id, selectedRef],
    queryFn: () => getBrief(project.project_id, selectedRef as string),
    enabled: Boolean(selectedRef),
  });

  function invalidate() {
    queryClient.invalidateQueries({ queryKey: ["supervision", "briefs"] });
    queryClient.invalidateQueries({ queryKey: ["supervision", "shared-briefs"] });
    queryClient.invalidateQueries({ queryKey: ["supervision", "brief"] });
    queryClient.invalidateQueries({ queryKey: ["supervision", "brief-versions"] });
    queryClient.invalidateQueries({ queryKey: ["supervision", "brief-compare"] });
    queryClient.invalidateQueries({ queryKey: ["supervision", "brief-source"] });
    queryClient.invalidateQueries({ queryKey: ["supervision", "project-context"] });
    refresh();
  }

  const create = useMutation({
    mutationFn: () =>
      createBrief(project.project_id, {
        title: createForm.title.trim(),
        entry_type: createForm.entryType,
        content: briefContentPayload(createForm.content),
      }),
    onSuccess: (brief) => {
      toast.success(`已创建草稿 ${brief.display_code}`);
      setCreating(false);
      setCreateForm({ title: "", entryType: "mixed", content: emptyBriefContent() });
      setSelectedRef(briefRefOf(brief));
      setEditor(null);
      invalidate();
    },
    onError: (error) => toast.error(errorText(error)),
  });

  const save = useMutation({
    mutationFn: () =>
      updateBrief(project.project_id, selectedRef as string, {
        title: editor?.title ?? "",
        entry_type: editor?.entryType ?? "",
        content: briefContentPayload(editor?.content ?? emptyBriefContent()),
      }),
    onSuccess: (brief) => {
      toast.success(
        brief.status === "draft" && brief.shared_version !== null
          ? `已生成新版本 ${brief.display_code}，已共享的版本继续对项目生效`
          : "草稿已保存",
      );
      setEditor(null);
      invalidate();
    },
    onError: (error) => toast.error(errorText(error)),
  });

  const confirm = useMutation({
    mutationFn: () => confirmBrief(project.project_id, selectedRef as string, confirmNote),
    onSuccess: (brief) => {
      toast.success(`已确认共享 ${brief.display_code}，对方现在能读到这一版`);
      setConfirmNote("");
      setEditor(null);
      invalidate();
    },
    onError: (error) => toast.error(errorText(error)),
  });

  const reject = useMutation({
    mutationFn: () => rejectBrief(project.project_id, selectedRef as string, reason.trim()),
    onSuccess: () => {
      toast.success("已否定该草稿，记录与审计保留");
      setReason("");
      setAction(null);
      invalidate();
    },
    onError: (error) => toast.error(errorText(error)),
  });

  const discard = useMutation({
    mutationFn: () => deleteBrief(project.project_id, selectedRef as string, reason.trim()),
    onSuccess: () => {
      toast.success("草稿已丢弃（软删除，历史保留）");
      setReason("");
      setAction(null);
      setSelectedRef(null);
      setEditor(null);
      invalidate();
    },
    onError: (error) => toast.error(errorText(error)),
  });

  const brief = detail.data ?? null;
  const dirty =
    editor && brief
      ? changedContentFields(contentFromBrief(brief), editor.content).length > 0 ||
        editor.title.trim() !== editor.baseTitle.trim() ||
        editor.entryType !== brief.entry_type
      : false;

  function openEditor(row: ResearchBrief) {
    setEditor({
      title: row.title,
      entryType: row.entry_type,
      content: contentFromBrief(row),
      baseTitle: row.title,
      baseContent: contentFromBrief(row),
    });
  }

  return (
    <div className="space-y-4">
      <AlertPanel
        title="研究定义规则"
        description={
          "学生与导师都能创建自己的研究定义；草稿只有创建者能看到。" +
          "确认共享后，当前版本进入项目共享区，另一方只读得到已确认的那一版（含来源会话的消息不共享）。" +
          "已确认的定义不能原地修改：再改动会生成新版本并把状态退回草稿，旧版本继续对项目生效。"
        }
      />

      <div className="space-y-2 rounded-md border p-3">
        <div className="flex flex-wrap items-center gap-2">
          <div className="text-sm font-medium">研究定义列表</div>
          <div className="flex gap-1">
            {SCOPES.map((item) => (
              <button
                key={item.key}
                type="button"
                onClick={() => setScope(item.key)}
                className={cn(
                  "rounded-md px-2 py-1 text-sm hover:bg-accent hover:text-accent-foreground",
                  scope === item.key ? "bg-accent font-medium" : "text-muted-foreground",
                )}
              >
                {item.label}
              </button>
            ))}
          </div>
          <Select value={statusFilter} onValueChange={setStatusFilter}>
            <SelectTrigger className="h-8 w-40">
              <SelectValue placeholder="不限状态" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ANY_STATUS}>不限状态</SelectItem>
              {STATUS_ORDER.map((status) => (
                <SelectItem key={status} value={status}>
                  {BRIEF_STATUS_LABEL[status]}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Button size="sm" className="ml-auto" onClick={() => setCreating((value) => !value)}>
            {creating ? "取消新建" : "手动新建研究定义"}
          </Button>
        </div>

        {briefs.isLoading && <p className="text-sm text-muted-foreground">加载中…</p>}
        {briefs.isError && (
          <AlertPanel tone="error" title="研究定义加载失败" description={errorText(briefs.error)} />
        )}
        {!briefs.isLoading && !briefs.isError && (briefs.data?.length ?? 0) === 0 && (
          <AlertPanel
            title={scope === "mine" ? "你还没有研究定义" : "这个范围里还没有研究定义"}
            description="可以从研究讨论里提取，也可以手动填写；草稿确认后才对项目共享。"
          />
        )}

        <ul className="space-y-2">
          {(briefs.data ?? []).map((row) => (
            <BriefRow
              key={row.brief_id}
              brief={row}
              active={selectedRef === briefRefOf(row)}
              onOpen={() => {
                setSelectedRef(briefRefOf(row));
                setEditor(null);
                setAction(null);
                setReason("");
              }}
            />
          ))}
        </ul>
      </div>

      <div className="space-y-2 rounded-md border p-3">
        <div className="text-sm font-medium">项目共享区（已确认的研究定义）</div>
        <p className="text-xs text-muted-foreground">
          只含已确认的结构化版本，不含来源消息与草稿；学生与导师读到的内容一致。
        </p>
        {shared.isError && (
          <AlertPanel tone="error" title="共享区加载失败" description={errorText(shared.error)} />
        )}
        {!shared.isLoading && !shared.isError && (shared.data?.length ?? 0) === 0 && (
          <AlertPanel
            title="还没有已确认共享的研究定义"
            description="确认一版研究定义后，它才会成为检索与任务的正式依据。"
          />
        )}
        <ul className="space-y-2">
          {(shared.data ?? []).map((row) => (
            <BriefRow
              key={`shared-${row.brief_id}`}
              brief={row}
              active={selectedRef === briefRefOf(row)}
              onOpen={() => {
                setSelectedRef(briefRefOf(row));
                setEditor(null);
                setAction(null);
                setReason("");
              }}
            />
          ))}
        </ul>
      </div>

      {creating && (
        <form
          className="space-y-3 rounded-md border p-3"
          onSubmit={(event) => {
            event.preventDefault();
            create.mutate();
          }}
        >
          <div className="text-sm font-medium">手动新建研究定义草稿</div>
          <p className="text-xs text-muted-foreground">
            模型能力尚未接入时，这是唯一入口；内容照样能确认共享并进入项目共享区。
          </p>
          <div className="grid gap-2 md:grid-cols-2">
            <div className="space-y-1">
              <Label htmlFor="brief-title" className="text-xs text-muted-foreground">
                标题
              </Label>
              <Input
                id="brief-title"
                value={createForm.title}
                onChange={(event) => setCreateForm({ ...createForm, title: event.target.value })}
                placeholder="例如：中文摘要质量评估"
                maxLength={200}
              />
            </div>
            <div className="space-y-1">
              <Label className="text-xs text-muted-foreground">条目类型</Label>
              <Select
                value={createForm.entryType}
                onValueChange={(value) => setCreateForm({ ...createForm, entryType: value })}
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {Object.entries(BRIEF_ENTRY_TYPE_LABEL).map(([value, label]) => (
                    <SelectItem key={value} value={value}>
                      {label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>
          <BriefContentEditor
            content={createForm.content}
            onChange={(content) => setCreateForm({ ...createForm, content })}
          />
          <div className="flex justify-end">
            <Button type="submit" disabled={create.isPending}>
              创建草稿
            </Button>
          </div>
        </form>
      )}

      {selectedRef && detail.isError && (
        <AlertPanel tone="error" title="打不开该研究定义" description={errorText(detail.error)} />
      )}

      {brief && (
        <div className="space-y-3 rounded-md border p-3">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-medium">{brief.title || "未命名研究定义"}</span>
            <Badge variant="secondary">{statusLabel(brief.status)}</Badge>
            <Badge variant="outline">{brief.display_code}</Badge>
            <Badge variant="outline">{kindLabel(brief.kind)}</Badge>
            <Badge variant="outline">
              {roleLabel(brief.owner_role)}
              {brief.is_owner ? "（我）" : ""}
            </Badge>
            <span className="text-xs text-muted-foreground">
              当前 v{brief.current_version}
              {brief.shared_version !== null && brief.shared_version !== undefined
                ? ` · 已共享 v${brief.shared_version}`
                : " · 未共享"}
              {brief.shown_version !== brief.current_version ? ` · 你读到 v${brief.shown_version}` : ""}
            </span>
          </div>

          <div className="text-xs text-muted-foreground">
            条目类型：{entryTypeLabel(brief.entry_type)}
            {brief.confirmed_at
              ? ` · 由 ${brief.confirmed_by || "创建者"} 于 ${formatTime(brief.confirmed_at)} 确认`
              : ""}
          </div>
          {brief.confirm_note && (
            <p className="whitespace-pre-wrap text-sm text-muted-foreground">
              确认说明：{brief.confirm_note}
            </p>
          )}

          {shownVersionNote(brief) && <AlertPanel title="可见范围" description={shownVersionNote(brief) ?? ""} />}
          {sharedVersionNote(brief) && (
            <AlertPanel tone="warning" title="已有新版本" description={sharedVersionNote(brief) ?? ""} />
          )}
          {editWouldForkNote(brief) && (
            <AlertPanel tone="warning" title="已确认的版本不可原地修改" description={editWouldForkNote(brief) ?? ""} />
          )}
          {sharedCannotRemoveNote(brief) && (
            <AlertPanel title="不能否定或丢弃" description={sharedCannotRemoveNote(brief) ?? ""} />
          )}

          {editor ? (
            <div className="space-y-3 rounded-md border p-3">
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-sm font-medium">编辑内容</span>
                {dirty && <Badge variant="outline">有未保存修改</Badge>}
              </div>
              <div className="grid gap-2 md:grid-cols-2">
                <div className="space-y-1">
                  <Label htmlFor="brief-edit-title" className="text-xs text-muted-foreground">
                    标题
                  </Label>
                  <Input
                    id="brief-edit-title"
                    value={editor.title}
                    onChange={(event) => setEditor({ ...editor, title: event.target.value })}
                    maxLength={200}
                  />
                </div>
                <div className="space-y-1">
                  <Label className="text-xs text-muted-foreground">条目类型</Label>
                  <Select
                    value={editor.entryType}
                    onValueChange={(value) => setEditor({ ...editor, entryType: value })}
                  >
                    <SelectTrigger>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {Object.entries(BRIEF_ENTRY_TYPE_LABEL).map(([value, label]) => (
                        <SelectItem key={value} value={value}>
                          {label}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
              </div>
              <BriefContentEditor
                content={editor.content}
                onChange={(content) => setEditor({ ...editor, content })}
              />
              <div className="flex flex-wrap gap-2">
                <Button size="sm" onClick={() => save.mutate()} disabled={save.isPending || !dirty}>
                  保存草稿
                </Button>
                <Button size="sm" variant="outline" onClick={() => setEditor(null)}>
                  放弃修改
                </Button>
              </div>
            </div>
          ) : (
            <BriefContentView brief={brief} />
          )}

          {brief.is_owner && (
            <div className="space-y-2 border-t pt-2">
              <div className="flex flex-wrap gap-2">
                {canEditBrief(brief) && (
                  <Button size="sm" variant="outline" onClick={() => openEditor(brief)}>
                    编辑
                  </Button>
                )}
                {canConfirmBrief(brief) && (
                  <>
                    <Input
                      className="max-w-64"
                      value={confirmNote}
                      onChange={(event) => setConfirmNote(event.target.value)}
                      placeholder="确认说明（可留空）"
                    />
                    <Button
                      size="sm"
                      onClick={() => confirm.mutate()}
                      disabled={confirm.isPending || Boolean(confirmBlockedReason(brief))}
                    >
                      确认本版并共享
                    </Button>
                  </>
                )}
                {canRejectBrief(brief) && (
                  <Button size="sm" variant="ghost" onClick={() => setAction("reject")}>
                    否定
                  </Button>
                )}
                {canDiscardBrief(brief) && (
                  <Button size="sm" variant="ghost" onClick={() => setAction("discard")}>
                    丢弃草稿
                  </Button>
                )}
              </div>
              {canConfirmBrief(brief) && confirmBlockedReason(brief) && (
                <p className="text-xs text-muted-foreground">{confirmBlockedReason(brief)}</p>
              )}
              {action && (
                <div className="space-y-2 rounded-md border p-2">
                  <div className="text-sm">
                    {action === "reject"
                      ? "否定这份草稿？记录与审计会保留，草稿不再作为工作定义。"
                      : "丢弃这份草稿？只有未共享过的草稿可以丢弃，已共享的版本是项目审计资产。"}
                  </div>
                  <Input
                    value={reason}
                    onChange={(event) => setReason(event.target.value)}
                    placeholder="原因（必填）"
                  />
                  <div className="flex gap-2">
                    <Button
                      size="sm"
                      variant="destructive"
                      disabled={
                        !reason.trim() ||
                        (action === "reject" ? reject.isPending : discard.isPending)
                      }
                      onClick={() => (action === "reject" ? reject.mutate() : discard.mutate())}
                    >
                      确认
                    </Button>
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() => {
                        setAction(null);
                        setReason("");
                      }}
                    >
                      取消
                    </Button>
                  </div>
                </div>
              )}
            </div>
          )}

          <BriefSourceMessages project={project} brief={brief} />
          <BriefVersionHistory project={project} brief={brief} />
        </div>
      )}
    </div>
  );
}

function BriefRow({
  brief,
  active,
  onOpen,
}: {
  brief: ResearchBrief;
  active: boolean;
  onOpen: () => void;
}) {
  const empty = isContentEmpty(contentFromBrief(brief));
  return (
    <li>
      <button
        type="button"
        onClick={onOpen}
        className={cn(
          "w-full rounded-md border p-3 text-left text-sm hover:bg-accent hover:text-accent-foreground",
          active && "border-primary bg-accent/60",
        )}
      >
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-medium">{brief.title || "未命名研究定义"}</span>
          <Badge variant="secondary">{statusLabel(brief.status)}</Badge>
          <Badge variant="outline">{brief.display_code}</Badge>
          <Badge variant="outline">{roleLabel(brief.owner_role)}</Badge>
          {brief.is_owner && <Badge variant="outline">我的</Badge>}
        </div>
        <div className="mt-1 text-xs text-muted-foreground">
          {kindLabel(brief.kind)} · {entryTypeLabel(brief.entry_type)} · 当前 v
          {brief.current_version}
          {brief.shared_version !== null && brief.shared_version !== undefined
            ? ` · 已共享 v${brief.shared_version}`
            : " · 未共享"}
          {empty ? " · 结构化内容还没填" : ""}
        </div>
      </button>
    </li>
  );
}
