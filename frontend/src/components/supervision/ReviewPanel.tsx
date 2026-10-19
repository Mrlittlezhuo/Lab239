import { useState } from "react";
import { useQuery } from "@tanstack/react-query";

import {
  getReview,
  listAiSuggestions,
  listReviewQueue,
  listSubmissions,
  listTasks,
  latestAiReview,
} from "@/api/supervision";
import type { AiReviewRun, ProjectDetail, Submission } from "@/api/supervision";
import { AlertPanel, errorText } from "@/components/supervision/AlertPanel";
import { PublishedReview } from "@/components/supervision/review/PublishedReview";
import { ReviewDraftForm } from "@/components/supervision/review/ReviewDraftForm";
import { SubmissionSnapshotCard } from "@/components/supervision/review/SubmissionSnapshotCard";
import { AI_STATUS_LABEL } from "@/components/supervision/review/aiReview";
import {
  joinAcWithAssessment,
  parseAcSnapshot,
  parseSelfAssessmentRows,
  submissionMaterials,
} from "@/components/supervision/review/snapshot";
import { Badge } from "@/components/ui/badge";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { cn } from "@/lib/utils";

function when(value?: string | null): string {
  return value ? value.slice(0, 16).replace("T", " ") : "时间未知";
}

/**
 * 导师审核面板（《学生研究与导师审核功能重新设计方案》里程碑1、§7.3）。
 *
 * 待审核队列来自后端 `review-queue`（有效提交且没有已发布审核，§5.6）；
 * 想回看已审核的提交，选一个任务列出它的全部提交版本。
 * 草稿与发布分离：保存草稿可反复做，发布后只能追加意见。
 *
 * 学生不会走到这个页签（路由只对导师开放），万一被渲染也只给只读说明：
 * 后端没有「只返回已发布结论」的接口，前端不去请求草稿再自行过滤。
 */
export function ReviewPanel({
  project,
  refresh,
}: {
  project: ProjectDetail;
  refresh: () => void;
}) {
  if (project.my_role !== "mentor") return <StudentReviewNotice />;
  return <MentorReview project={project} refresh={refresh} />;
}

function StudentReviewNotice() {
  return (
    <div className="space-y-3">
      <AlertPanel
        title="导师审核只对导师开放"
        description="未发布的审核草稿学生看不到；已发布的结论会出现在「进展与提交」的提交详情和站内通知里。"
      />
      <AlertPanel
        tone="warning"
        title="这里不会读取审核草稿"
        description="后端目前没有「只返回已发布结论」的接口，所以学生视图不请求审核记录，避免把导师草稿带到浏览器里。"
      />
    </div>
  );
}

function MentorReview({
  project,
  refresh,
}: {
  project: ProjectDetail;
  refresh: () => void;
}) {
  const [browseTaskRef, setBrowseTaskRef] = useState("");
  const [selectedRef, setSelectedRef] = useState("");

  const queue = useQuery({
    queryKey: ["supervision", "review-queue", project.project_id],
    queryFn: () => listReviewQueue(project.project_id),
  });
  const tasks = useQuery({
    queryKey: ["supervision", "tasks", project.project_id],
    queryFn: () => listTasks(project.project_id),
  });
  const submissions = useQuery({
    queryKey: ["supervision", "submissions", project.project_id, browseTaskRef],
    queryFn: () => listSubmissions(project.project_id, browseTaskRef),
    enabled: browseTaskRef !== "",
  });

  const rows = (browseTaskRef === "" ? (queue.data ?? []) : (submissions.data ?? [])).filter(
    // 已撤回的提交不在审核范围里（后端也会拒绝）
    (item) => item.status !== "withdrawn",
  );
  const selected: Submission | null =
    rows.find((item) => item.display_code === selectedRef) ?? rows[0] ?? null;

  const review = useQuery({
    queryKey: ["supervision", "review", project.project_id, selected?.display_code ?? ""],
    queryFn: () => getReview(project.project_id, selected?.display_code ?? ""),
    enabled: selected !== null,
  });

  const aiLatest = useQuery({
    queryKey: [
      "supervision",
      "ai-review",
      "latest",
      project.project_id,
      selected?.task_id ?? "",
      selected?.display_code ?? "",
    ],
    queryFn: () => latestAiReview(project.project_id, selected?.task_id ?? "", selected?.display_code ?? ""),
    enabled: selected !== null,
  });
  const aiRun = aiLatest.data ?? null;
  const suggestions = useQuery({
    queryKey: ["supervision", "ai-review", "suggestions", project.project_id, aiRun?.code ?? ""],
    queryFn: () => listAiSuggestions(project.project_id, aiRun?.code ?? ""),
    enabled: aiRun !== null && aiRun.status === "completed",
  });

  const loading =
    queue.isLoading || (browseTaskRef !== "" && submissions.isLoading) || tasks.isLoading;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2 rounded-md border p-3">
        <span className="text-sm font-medium">审核范围</span>
        <Select
          value={browseTaskRef === "" ? "queue" : browseTaskRef}
          onValueChange={(value) => setBrowseTaskRef(value === "queue" ? "" : value)}
        >
          <SelectTrigger className="w-[min(100%,360px)]">
            <SelectValue placeholder="待审核队列" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="queue">
              待审核队列（{queue.data?.length ?? 0} 份）
            </SelectItem>
            {(tasks.data ?? []).map((task) => (
              <SelectItem key={task.task_id} value={task.task_id}>
                {task.code} · {task.title}（全部提交版本）
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <span className="text-xs text-muted-foreground">
          队列只含有效提交且没有已发布审核的提交；回看历史版本请选具体任务。
        </span>
      </div>

      {loading && <p className="text-sm text-muted-foreground">加载中…</p>}
      {queue.isError && (
        <AlertPanel tone="error" title="待审核队列加载失败" description={errorText(queue.error)} />
      )}
      {submissions.isError && (
        <AlertPanel tone="error" title="提交列表加载失败" description={errorText(submissions.error)} />
      )}
      {!loading && rows.length === 0 && (
        <AlertPanel
          title={browseTaskRef === "" ? "待审核队列是空的" : "该任务还没有提交"}
          description={
            browseTaskRef === ""
              ? "学生正式提交后会出现在这里；已审核完成的提交不再出现在队列里。"
              : "学生提交新版本后会出现在这里。"
          }
        />
      )}

      {selected && (
        <div className="grid gap-4 lg:grid-cols-[minmax(0,260px)_minmax(0,1fr)]">
          <ul className="space-y-2">
            {rows.map((item) => (
              <li key={item.submission_id}>
                <button
                  type="button"
                  onClick={() => setSelectedRef(item.display_code)}
                  className={cn(
                    "w-full rounded-md border p-2 text-left text-sm hover:bg-accent hover:text-accent-foreground",
                    selected.submission_id === item.submission_id && "border-primary",
                  )}
                >
                  <div className="font-medium">{item.display_code}</div>
                  <div className="text-xs text-muted-foreground">
                    {when(item.create_time)} · {parseAcSnapshot(item.ac_snapshot).length} 条标准 ·{" "}
                    {submissionMaterials(item).length} 份材料
                  </div>
                </button>
              </li>
            ))}
          </ul>

          <div className="space-y-4">
            <SubmissionSnapshotCard submission={selected} />
            <AiStrip run={aiRun} label={aiRun ? `${aiRun.display_code}` : ""} />

            {review.isFetching && !review.data && (
              <p className="text-sm text-muted-foreground">审核记录加载中…</p>
            )}
            {review.isError && (
              <AlertPanel tone="error" title="审核记录加载失败" description={errorText(review.error)} />
            )}
            {review.data && review.data.status === "published" ? (
              <PublishedReview
                project={project}
                review={review.data ?? null}
                rows={joinAcWithAssessment(
                  parseAcSnapshot(selected.ac_snapshot),
                  parseSelfAssessmentRows(selected.self_assessment),
                )}
              />
            ) : (
              !review.isError && (
                <ReviewDraftForm
                  key={`${selected.submission_id}-${review.data?.review_id ?? "new"}`}
                  project={project}
                  submissionRef={selected.display_code}
                  rows={joinAcWithAssessment(
                    parseAcSnapshot(selected.ac_snapshot),
                    parseSelfAssessmentRows(selected.self_assessment),
                  )}
                  review={review.data ?? null}
                  suggestions={suggestions.data ?? []}
                  aiRunLabel={
                    aiRun
                      ? `${aiRun.display_code}（${AI_STATUS_LABEL[aiRun.status] ?? aiRun.status}）`
                      : ""
                  }
                  refresh={refresh}
                />
              )
            )}
          </div>
        </div>
      )}
    </div>
  );
}

/**
 * AI 预审状态条（§6.3）：只说明 AI 侧的状态与绑定版本，
 * 不与导师结论合并成一个「审核结果」（§13.4、P-22）。
 */
function AiStrip({ run, label }: { run: AiReviewRun | null; label: string }) {
  if (run === null) {
    return (
      <AlertPanel
        title="本次提交没有 AI 预审"
        description="AI 预审只是找材料与标出缺失项的辅助手段，在「AI 预审」页签发起；没有它导师照常审核。"
      />
    );
  }
  return (
    <div className="flex flex-wrap items-center gap-2 rounded-md border border-dashed p-3 text-sm">
      <span className="font-medium">AI 预审状态</span>
      <Badge variant="secondary">{AI_STATUS_LABEL[run.status] ?? run.status}</Badge>
      <span className="text-xs text-muted-foreground">
        {label} · 触发：{run.trigger_source === "mentor_manual" ? "导师手动" : run.trigger_source === "auto_on_submission" ? "提交后自动" : "学生自检"} ·{" "}
        绑定 {run.submission_display || run.submission_code || "学生草稿"} · 完成于{" "}
        {when(run.finished_at)}
      </span>
      {run.stale && (
        <Badge variant="outline">{run.stale_label || "已过时"}</Badge>
      )}
      <span className="text-xs text-muted-foreground">
        与导师结论是两个独立域，AI 不会替导师填结论。
      </span>
    </div>
  );
}
