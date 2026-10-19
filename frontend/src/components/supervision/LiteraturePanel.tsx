import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import {
  createLiteratureTask,
  listLiteratureTasks,
  listSharedBriefs,
} from "@/api/supervision";
import type { LiteratureTaskBody, ProjectDetail } from "@/api/supervision";
import { AlertPanel, errorText } from "@/components/supervision/AlertPanel";
import { LiteratureCitations } from "@/components/supervision/literature/LiteratureCitations";
import { LiteratureConclusions } from "@/components/supervision/literature/LiteratureConclusions";
import { LiteratureResults } from "@/components/supervision/literature/LiteratureResults";
import { LiteratureTaskCard } from "@/components/supervision/literature/LiteratureTaskCard";
import {
  PURPOSE_LABEL,
  TASK_STATUS_LABEL,
  isStudent,
  labelOf,
} from "@/components/supervision/literature/literatureModel";
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
import { Textarea } from "@/components/ui/textarea";

function terms(value: string): string[] {
  return value
    .split(/[、,，\n]+/)
    .map((item) => item.trim())
    .filter((item) => item !== "");
}

function year(value: string): number | null {
  const trimmed = value.trim();
  if (trimmed === "") return null;
  const parsed = Number(trimmed);
  return Number.isFinite(parsed) ? Math.trunc(parsed) : null;
}

/**
 * 论文检索面板（里程碑3）。
 *
 * 产品口径全部照后端：检索任务必须绑定已确认并共享的结构化研究定义；
 * 检索方案由模型生成但必须由学生确认后才允许检索；外部来源或模型未接入时
 * 是 503 说明而不是失败，任务与已有候选都保留；纳入/排除与结论只有学生能定，
 * 导师只能查看与给意见。
 */
export function LiteraturePanel({ project }: { project: ProjectDetail }) {
  const queryClient = useQueryClient();
  const student = isStudent(project);
  const [creating, setCreating] = useState(false);
  const [briefRef, setBriefRef] = useState("");
  const [goal, setGoal] = useState("");
  const [purpose, setPurpose] = useState("verify");
  const [keywords, setKeywords] = useState("");
  const [synonyms, setSynonyms] = useState("");
  const [excludeTerms, setExcludeTerms] = useState("");
  const [sources, setSources] = useState("");
  const [yearFrom, setYearFrom] = useState("");
  const [yearTo, setYearTo] = useState("");
  const [resultLimit, setResultLimit] = useState("30");
  const [deepExtractLimit, setDeepExtractLimit] = useState("");
  const [requireFulltext, setRequireFulltext] = useState(false);
  const [asDraft, setAsDraft] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);

  const tasks = useQuery({
    queryKey: ["supervision", "literature", "tasks", project.project_id],
    queryFn: () => listLiteratureTasks(project.project_id),
  });

  const briefs = useQuery({
    queryKey: ["supervision", "literature", "shared-briefs", project.project_id],
    queryFn: () => listSharedBriefs(project.project_id),
  });

  // 只有确认过并共享出去的研究定义才能驱动正式检索（后端同样裁决）。
  const usableBriefs = (briefs.data ?? []).filter(
    (brief) => brief.status === "confirmed" && brief.shared_version !== null,
  );

  function invalidate() {
    queryClient.invalidateQueries({ queryKey: ["supervision", "literature"] });
  }

  const create = useMutation({
    mutationFn: () => {
      const body: LiteratureTaskBody = {
        brief_ref: briefRef,
        goal: goal.trim(),
        purpose,
        keywords: terms(keywords),
        synonyms: terms(synonyms),
        exclude_terms: terms(excludeTerms),
        sources: terms(sources),
        year_from: year(yearFrom),
        year_to: year(yearTo),
        require_fulltext: requireFulltext,
        result_limit: Number(resultLimit) || 30,
        deep_extract_limit: deepExtractLimit.trim() === "" ? null : Number(deepExtractLimit),
        as_draft: asDraft,
      };
      return createLiteratureTask(project.project_id, body);
    },
    onSuccess: (task) => {
      setCreating(false);
      setGoal("");
      setSelected(task.code);
      toast.success(
        asDraft
          ? `检索任务 ${task.code} 已存为草稿（依据 ${task.brief_code} v${task.brief_version}）`
          : `检索任务 ${task.code} 已创建（依据 ${task.brief_code} v${task.brief_version}）`,
      );
      invalidate();
    },
    onError: (error) => toast.error(errorText(error)),
  });

  const rows = tasks.data ?? [];
  const active = selected ?? (rows.length > 0 ? rows[0].code : "");

  return (
    <div className="space-y-4">
      <div className="space-y-2">
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="text-base font-medium">论文检索任务（{rows.length}）</h2>
          {student && (
            <Button
              size="sm"
              variant="outline"
              onClick={() => setCreating((value) => !value)}
              disabled={usableBriefs.length === 0}
            >
              新建检索任务
            </Button>
          )}
          {student && usableBriefs.length === 0 && (
            <span className="text-xs text-muted-foreground">
              还没有已确认并共享的研究定义：先到「研究定义」页确认本版，再创建检索任务。
            </span>
          )}
          {!student && (
            <span className="text-xs text-muted-foreground">
              导师可以查看任务、候选与结论，并给出确认/质疑/要求补充证据。
            </span>
          )}
        </div>

        {tasks.isError && (
          <AlertPanel tone="error" title="检索任务加载失败" description={errorText(tasks.error)} />
        )}

        {creating && (
          <div className="space-y-3 rounded-md border border-dashed p-3">
            <p className="text-xs text-muted-foreground">
              检索任务必须绑定一条已确认并共享的结构化研究定义，并把它当时的版本钉住：
              之后研究定义再改，本任务的检索结果与结论仍按当时的依据解释。
            </p>
            <div className="space-y-1">
              <Label className="text-xs text-muted-foreground">依据的研究定义</Label>
              <Select value={briefRef} onValueChange={setBriefRef}>
                <SelectTrigger className="h-9 w-[420px]">
                  <SelectValue placeholder="选择一条已确认共享的研究定义" />
                </SelectTrigger>
                <SelectContent>
                  {usableBriefs.map((brief) => (
                    <SelectItem key={brief.brief_id} value={brief.code}>
                      {brief.display_code}：{brief.title || "（无标题）"}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1">
              <Label htmlFor="lit-task-goal" className="text-xs text-muted-foreground">
                检索目标（必填：要验证、补充依据、找反例还是探索方向）
              </Label>
              <Textarea
                id="lit-task-goal"
                rows={3}
                value={goal}
                onChange={(event) => setGoal(event.target.value)}
              />
            </div>
            <div className="flex flex-wrap gap-2">
              <div className="space-y-1">
                <Label className="text-xs text-muted-foreground">目标性质</Label>
                <Select value={purpose} onValueChange={setPurpose}>
                  <SelectTrigger className="h-9 w-[180px]">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {Object.entries(PURPOSE_LABEL).map(([key, text]) => (
                      <SelectItem key={key} value={key}>
                        {text}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-1">
                <Label htmlFor="lit-task-keywords" className="text-xs text-muted-foreground">
                  关键词（顿号或逗号分隔）
                </Label>
                <Input
                  id="lit-task-keywords"
                  value={keywords}
                  onChange={(event) => setKeywords(event.target.value)}
                  placeholder="例如：图神经网络、缺补全"
                  className="w-[280px]"
                />
              </div>
              <div className="space-y-1">
                <Label htmlFor="lit-task-synonyms" className="text-xs text-muted-foreground">
                  同义词
                </Label>
                <Input
                  id="lit-task-synonyms"
                  value={synonyms}
                  onChange={(event) => setSynonyms(event.target.value)}
                  className="w-[220px]"
                />
              </div>
              <div className="space-y-1">
                <Label htmlFor="lit-task-exclude" className="text-xs text-muted-foreground">
                  排除词
                </Label>
                <Input
                  id="lit-task-exclude"
                  value={excludeTerms}
                  onChange={(event) => setExcludeTerms(event.target.value)}
                  className="w-[200px]"
                />
              </div>
            </div>
            <div className="flex flex-wrap gap-2">
              <div className="space-y-1">
                <Label htmlFor="lit-task-sources" className="text-xs text-muted-foreground">
                  来源（留空由检索方案建议）
                </Label>
                <Input
                  id="lit-task-sources"
                  value={sources}
                  onChange={(event) => setSources(event.target.value)}
                  placeholder="例如：arxiv、crossref"
                  className="w-[240px]"
                />
              </div>
              <div className="space-y-1">
                <Label htmlFor="lit-task-year-from" className="text-xs text-muted-foreground">
                  起始年份
                </Label>
                <Input
                  id="lit-task-year-from"
                  value={yearFrom}
                  onChange={(event) => setYearFrom(event.target.value)}
                  className="w-[100px]"
                />
              </div>
              <div className="space-y-1">
                <Label htmlFor="lit-task-year-to" className="text-xs text-muted-foreground">
                  结束年份
                </Label>
                <Input
                  id="lit-task-year-to"
                  value={yearTo}
                  onChange={(event) => setYearTo(event.target.value)}
                  className="w-[100px]"
                />
              </div>
              <div className="space-y-1">
                <Label htmlFor="lit-task-limit" className="text-xs text-muted-foreground">
                  结果上限（1-200）
                </Label>
                <Input
                  id="lit-task-limit"
                  value={resultLimit}
                  onChange={(event) => setResultLimit(event.target.value)}
                  className="w-[120px]"
                />
              </div>
              <div className="space-y-1">
                <Label htmlFor="lit-task-deep-limit" className="text-xs text-muted-foreground">
                  深度提取上限（0-20，留空用默认）
                </Label>
                <Input
                  id="lit-task-deep-limit"
                  value={deepExtractLimit}
                  onChange={(event) => setDeepExtractLimit(event.target.value)}
                  className="w-[140px]"
                />
              </div>
            </div>
            <div className="flex flex-wrap items-center gap-4 text-sm">
              <label className="flex items-center gap-2">
                <input
                  type="checkbox"
                  checked={requireFulltext}
                  onChange={(event) => setRequireFulltext(event.target.checked)}
                />
                只保留能拿到全文的候选
              </label>
              <label className="flex items-center gap-2">
                <input
                  type="checkbox"
                  checked={asDraft}
                  onChange={(event) => setAsDraft(event.target.checked)}
                />
                先存草稿（不进入待检索）
              </label>
            </div>
            <div className="flex gap-2">
              <Button
                size="sm"
                onClick={() => create.mutate()}
                disabled={create.isPending || briefRef === "" || goal.trim() === ""}
              >
                {create.isPending ? "创建中…" : "创建检索任务"}
              </Button>
              <Button size="sm" variant="outline" onClick={() => setCreating(false)}>
                取消
              </Button>
            </div>
          </div>
        )}

        {!tasks.isLoading && !tasks.isError && rows.length === 0 && (
          <AlertPanel
            title="还没有检索任务"
            description="检索任务从已确认并共享的研究定义生成，系统不会自己决定搜什么：方案要经你确认后才允许检索。"
          />
        )}

        <ul className="space-y-3">
          {rows.map((task) => (
            <li key={task.task_id}>
              <LiteratureTaskCard project={project} task={task} onOpenResults={() => setSelected(task.code)} />
            </li>
          ))}
        </ul>
      </div>

      {active && (
        <>
          <div className="flex flex-wrap items-center gap-2 border-t pt-3">
            <h2 className="text-base font-medium">
              {active} 的候选论文
            </h2>
            <Badge variant="outline">
              {labelOf(TASK_STATUS_LABEL, rows.find((task) => task.code === active)?.status ?? "")}
            </Badge>
            <div className="flex flex-wrap gap-2">
              {rows.map((task) => (
                <Button
                  key={task.task_id}
                  size="sm"
                  variant={task.code === active ? "default" : "outline"}
                  onClick={() => setSelected(task.code)}
                >
                  {task.code}
                </Button>
              ))}
            </div>
          </div>
          <LiteratureResults project={project} taskRef={active} />
          <div className="border-t pt-3">
            <LiteratureConclusions project={project} taskRef={active} />
          </div>
        </>
      )}

      <div className="border-t pt-3">
        <LiteratureCitations project={project} />
      </div>
    </div>
  );
}
