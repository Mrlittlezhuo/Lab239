import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import {
  analyzeLatexArchive,
  createLatexProgressDraft,
  listAttachments,
  listLatexProgressDrafts,
  listLatexSnapshots,
  listTasks,
} from "@/api/supervision";
import type { LatexSnapshot, ProjectDetail } from "@/api/supervision";
import { AlertPanel, errorText } from "@/components/supervision/AlertPanel";
import { LatexDraftCard } from "@/components/supervision/latex/LatexDraftCard";
import { LatexSnapshotCard } from "@/components/supervision/latex/LatexSnapshotCard";
import { describeBlocked, needsMainFileChoice } from "@/components/supervision/latex/latexModel";
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

/** 归档附件判定：后端只接收 .zip 稿件归档（其它格式直接 400）。 */
function isZipArchive(ext: string): boolean {
  return ext.replace(/^\./, "").toLowerCase() === "zip";
}

/**
 * LaTeX 稿件静态分析与进展草稿（《AI预审功能详细设计方案》里程碑5）。
 *
 * 必须照后端语义显示的口径：
 * 1. 全程静态解析，``compiled`` 恒 false——界面绝不出现"已编译/已通过"；
 * 2. 危险归档（zip 炸弹、路径穿越、越界条目）会被拒绝并给出全部命中项，
 *    拒绝本身已记审计，快照不会落库；
 * 3. 主文件不唯一时后端返回候选清单，必须由用户选，系统不猜；
 * 4. 草稿只追加不覆盖，且只有学生本人确认才变成正式进展。
 */
export function LatexPanel({
  project,
  refresh,
}: {
  project: ProjectDetail;
  refresh: () => void;
}) {
  const [attachmentRef, setAttachmentRef] = useState("");
  const [mainFile, setMainFile] = useState("");
  const [baseAttachmentRef, setBaseAttachmentRef] = useState("");
  const [baseMainFile, setBaseMainFile] = useState("");
  const [selectedSnapshot, setSelectedSnapshot] = useState<string | null>(null);
  const [selectedDraft, setSelectedDraft] = useState<string | null>(null);
  const [blocked, setBlocked] = useState<{ headline: string; lines: string[] } | null>(null);
  const queryClient = useQueryClient();

  const attachments = useQuery({
    queryKey: ["supervision", "attachments", project.project_id],
    queryFn: () => listAttachments(project.project_id),
  });

  const tasks = useQuery({
    queryKey: ["supervision", "tasks", project.project_id],
    queryFn: () => listTasks(project.project_id),
  });

  const snapshots = useQuery({
    queryKey: ["supervision", "latex", "snapshots", project.project_id],
    queryFn: () => listLatexSnapshots(project.project_id),
  });

  const drafts = useQuery({
    queryKey: ["supervision", "latex", "drafts", project.project_id],
    queryFn: () => listLatexProgressDrafts(project.project_id),
  });

  const archives = useMemo(
    () => (attachments.data ?? []).filter((item) => isZipArchive(item.ext) && item.status === "active"),
    [attachments.data],
  );

  const snapshotRows = snapshots.data ?? [];
  const draftRows = drafts.data ?? [];
  const activeSnapshot: LatexSnapshot | null =
    snapshotRows.find((row) => row.code === selectedSnapshot) ?? snapshotRows[0] ?? null;
  const activeDraft =
    draftRows.find((row) => row.code === selectedDraft) ?? draftRows[0] ?? null;

  // 快照与草稿都是"只追加"的列表，写完就整体失效重取，避免本地拼出后端没给的状态
  function invalidateLatex() {
    queryClient.invalidateQueries({ queryKey: ["supervision", "latex"] });
  }

  const analyze = useMutation({
    mutationFn: () =>
      analyzeLatexArchive(project.project_id, {
        attachment_ref: attachmentRef,
        main_file: mainFile.trim(),
      }),
    onSuccess: (snapshot) => {
      setBlocked(null);
      setSelectedSnapshot(snapshot.code);
      toast.success(`已生成分析快照 ${snapshot.code}`);
      invalidateLatex();
      refresh();
    },
    onError: (error) => {
      const detail = describeBlocked(error);
      if (detail) {
        setBlocked(detail);
        return;
      }
      toast.error(errorText(error));
    },
  });

  const makeDraft = useMutation({
    mutationFn: () =>
      createLatexProgressDraft(project.project_id, {
        attachment_ref: attachmentRef,
        base_attachment_ref: baseAttachmentRef,
        main_file: mainFile.trim(),
        base_main_file: baseMainFile.trim(),
      }),
    onSuccess: (draft) => {
      setBlocked(null);
      setSelectedDraft(draft.code);
      toast.success(
        draft.base_attachment_code
          ? `已生成进展草稿 ${draft.code}`
          : `已生成基线草稿 ${draft.code}（没有对比版本，只记录当前结构）`,
      );
      invalidateLatex();
      refresh();
    },
    onError: (error) => {
      const detail = describeBlocked(error);
      if (detail) {
        setBlocked(detail);
        return;
      }
      toast.error(errorText(error));
    },
  });

  const blockedIsAmbiguous = Boolean(blocked?.lines.some((line) => line.startsWith("候选主文件")));

  return (
    <div className="space-y-4">
      <AlertPanel
        title="稿件只做静态解析：不编译、不运行、不执行任何命令"
        description={
          "上传 .zip 稿件归档后，系统按源码结构读出主文件、章节大纲、图表与引用计数，" +
          "并对两个版本做静态差异生成进展草稿。草稿不会自动变成进展：只有你本人确认才生成正式进展，" +
          "导师只能查看。差异不能证明实验已执行，这条说明会跟着草稿一起显示。"
        }
      />

      <div className="grid gap-4 lg:grid-cols-[1fr_1fr]">
        <section className="space-y-3 rounded-md border p-3">
          <div className="text-sm font-medium">分析稿件归档</div>
          {attachments.isLoading && <p className="text-sm text-muted-foreground">附件加载中…</p>}
          {attachments.isError && (
            <AlertPanel tone="error" title="附件加载失败" description={errorText(attachments.error)} />
          )}
          {!attachments.isLoading && !attachments.isError && archives.length === 0 && (
            <AlertPanel
              title="还没有可分析的 .zip 稿件"
              description="请先到「附件」页上传 .zip 稿件归档（含 main.tex 与图片等）。其它格式不会被解析。"
            />
          )}
          {archives.length > 0 && (
            <div className="space-y-2">
              <div className="space-y-1">
                <Label className="text-xs text-muted-foreground">稿件归档</Label>
                <Select
                  value={attachmentRef === "" ? "none" : attachmentRef}
                  onValueChange={(value) => {
                    setAttachmentRef(value === "none" ? "" : value);
                    setBlocked(null);
                  }}
                >
                  <SelectTrigger className="h-9 w-full">
                    <SelectValue placeholder="选择要分析的归档" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="none">不选择</SelectItem>
                    {archives.map((item) => (
                      <SelectItem key={item.attachment_id} value={item.code}>
                        {item.name}（{item.code} v{item.version}）
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-1">
                <Label htmlFor="latex-main-file" className="text-xs text-muted-foreground">
                  主文件（可留空自动判定；有歧义时必须填）
                </Label>
                <Input
                  id="latex-main-file"
                  value={mainFile}
                  onChange={(event) => setMainFile(event.target.value)}
                  placeholder="按归档内相对路径填写，例如 main.tex"
                />
              </div>
              <Button
                size="sm"
                onClick={() => analyze.mutate()}
                disabled={!attachmentRef || analyze.isPending}
              >
                {analyze.isPending ? "解析中…" : "分析稿件"}
              </Button>
            </div>
          )}

          {blocked && (
            <AlertPanel
              tone={blockedIsAmbiguous ? "warning" : "error"}
              title={blockedIsAmbiguous ? "需要指定主文件" : "归档未通过安全检查"}
              description={[blocked.headline, ...blocked.lines].filter((line) => line !== "").join("\n")}
            />
          )}
          {blockedIsAmbiguous && archives.length > 0 && (
            <p className="text-xs text-muted-foreground">
              把上面候选路径之一填进「主文件」再点「分析稿件」即可。系统不会替你猜主文件。
            </p>
          )}
        </section>

        <section className="space-y-3 rounded-md border p-3">
          <div className="text-sm font-medium">生成进展草稿（两个版本做差异）</div>
          <p className="text-xs text-muted-foreground">
            选当前版本与对比版本，系统按章节、图、表、公式、引用与字数生成静态差异条目。
            不选对比版本时只出基线，不会编造"进展"。
          </p>
          {archives.length > 0 && (
            <div className="space-y-2">
              <div className="space-y-1">
                <Label className="text-xs text-muted-foreground">当前版本</Label>
                <Select
                  value={attachmentRef === "" ? "none" : attachmentRef}
                  onValueChange={(value) => setAttachmentRef(value === "none" ? "" : value)}
                >
                  <SelectTrigger className="h-9 w-full">
                    <SelectValue placeholder="选择当前稿件归档" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="none">不选择</SelectItem>
                    {archives.map((item) => (
                      <SelectItem key={item.attachment_id} value={item.code}>
                        {item.name}（{item.code} v{item.version}）
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-1">
                <Label className="text-xs text-muted-foreground">对比版本（留空 = 只出基线）</Label>
                <Select
                  value={baseAttachmentRef === "" ? "none" : baseAttachmentRef}
                  onValueChange={(value) => setBaseAttachmentRef(value === "none" ? "" : value)}
                >
                  <SelectTrigger className="h-9 w-full">
                    <SelectValue placeholder="不选：只记录当前结构" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="none">不对比</SelectItem>
                    {archives
                      .filter((item) => item.code !== attachmentRef)
                      .map((item) => (
                        <SelectItem key={item.attachment_id} value={item.code}>
                          {item.name}（{item.code} v{item.version}）
                        </SelectItem>
                      ))}
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-1">
                <Label htmlFor="latex-base-main-file" className="text-xs text-muted-foreground">
                  对比版本的主文件（可留空自动判定）
                </Label>
                <Input
                  id="latex-base-main-file"
                  value={baseMainFile}
                  onChange={(event) => setBaseMainFile(event.target.value)}
                  placeholder="例如 main.tex"
                />
              </div>
              <Button
                size="sm"
                onClick={() => makeDraft.mutate()}
                disabled={!attachmentRef || makeDraft.isPending}
              >
                {makeDraft.isPending ? "生成中…" : "生成进展草稿"}
              </Button>
            </div>
          )}
        </section>
      </div>

      <section className="space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          <h3 className="text-sm font-medium">分析快照</h3>
          <span className="text-xs text-muted-foreground">
            每次分析都新增一条快照，不覆盖旧结果；同一份归档可以换主文件重新分析。
          </span>
        </div>
        {snapshots.isError && (
          <AlertPanel tone="error" title="快照加载失败" description={errorText(snapshots.error)} />
        )}
        {!snapshots.isLoading && !snapshots.isError && snapshotRows.length === 0 && (
          <AlertPanel title="还没有分析快照" description="在上面选一份 .zip 稿件归档点「分析稿件」。" />
        )}
        {snapshotRows.length > 0 && (
          <div className="flex flex-wrap gap-1">
            {snapshotRows.map((row) => (
              <Button
                key={row.snapshot_id}
                size="sm"
                variant={row.code === activeSnapshot?.code ? "default" : "outline"}
                onClick={() => setSelectedSnapshot(row.code)}
              >
                {row.code} · {row.attachment_code} v{row.attachment_version}
                {needsMainFileChoice(row) ? " · 需指定主文件" : ""}
              </Button>
            ))}
          </div>
        )}
        {activeSnapshot && <LatexSnapshotCard snapshot={activeSnapshot} />}
      </section>

      <section className="space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          <h3 className="text-sm font-medium">进展草稿与正式进展</h3>
          <span className="text-xs text-muted-foreground">
            草稿按版本追加；确认后生成一条正式进展（PR-NNN），草稿本身保留为历史。
          </span>
        </div>
        {drafts.isError && (
          <AlertPanel tone="error" title="草稿加载失败" description={errorText(drafts.error)} />
        )}
        {!drafts.isLoading && !drafts.isError && draftRows.length === 0 && (
          <AlertPanel
            title="还没有进展草稿"
            description="在上面选当前版本（以及可选的对比版本）点「生成进展草稿」。"
          />
        )}
        {draftRows.length > 0 && (
          <div className="flex flex-wrap gap-1">
            {draftRows.map((row) => (
              <Button
                key={row.draft_id}
                size="sm"
                variant={row.code === activeDraft?.code ? "default" : "outline"}
                onClick={() => setSelectedDraft(row.code)}
              >
                {row.code}
              </Button>
            ))}
          </div>
        )}
        {activeDraft && (
          <LatexDraftCard
            project={project}
            draft={activeDraft}
            tasks={tasks.data ?? []}
            selected={activeDraft.code === selectedDraft}
            onSelect={() => setSelectedDraft(activeDraft.code)}
          />
        )}
      </section>
    </div>
  );
}
