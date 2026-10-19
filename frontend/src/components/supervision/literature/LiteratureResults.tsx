import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import {
  compareLiteraturePapers,
  createLiteratureEvidenceCard,
  decideLiteratureResult,
  getLiteratureExtraction,
  listLiteratureResults,
  runLiteratureDeepExtract,
} from "@/api/supervision";
import type { LiteratureExtraction, LiteratureResult, ProjectDetail } from "@/api/supervision";
import { AlertPanel, errorText } from "@/components/supervision/AlertPanel";
import {
  DECISION_LABEL,
  DECISION_OPTIONS,
  EXTRACT_STATE_LABEL,
  MODEL_UNAVAILABLE_TEXT,
  RELEVANCE_LABEL,
  SUPPORT_RELATION_LABEL,
  isCapabilityUnavailable,
  isStudent,
  labelOf,
  relevanceReasonLines,
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

/** 深度提取结果按字段展开；v1 只有摘要级，绝不显示页码。 */
function ExtractionBlock({ extraction }: { extraction: LiteratureExtraction }) {
  const rows: [string, string][] = [
    ["研究问题", extraction.research_problem],
    ["方法", extraction.method],
    ["算法", extraction.algorithm],
    ["实验", extraction.experiments],
    ["结果与指标", extraction.results_metrics],
    ["局限", extraction.limitations],
  ];
  return (
    <div className="space-y-1 rounded-md border border-dashed p-2 text-sm">
      <div className="text-xs text-muted-foreground">
        深度提取 v{extraction.version} · 层级：
        {extraction.level === "abstract" ? "只有摘要（没有全文定位）" : extraction.level} · 模型{" "}
        {extraction.model_name || "未记录"} · 提示词 {extraction.prompt_version}
      </div>
      {extraction.summary && <div>{extraction.summary}</div>}
      <ul className="space-y-0.5">
        {rows
          .filter(([, value]) => value !== "")
          .map(([key, value]) => (
            <li key={key}>
              <span className="text-muted-foreground">{key}：</span>
              {value}
            </li>
          ))}
      </ul>
      {extraction.evidence.length > 0 && (
        <div className="text-xs text-muted-foreground">
          证据 {extraction.evidence.length} 条（原文定位见下）
        </div>
      )}
    </div>
  );
}

/**
 * 候选论文列表与筛选决定。
 *
 * 口径：关联等级来自规则（``relevance_score`` v1 恒空），AI 的摘要语义分析只是
 * 附加说明、不覆盖规则字段；纳入/排除/稍后阅读必须写理由且只有学生能做；
 * 深度提取要学生逐篇点，检索本身不会自动提取任何一篇。
 */
export function LiteratureResults({
  project,
  taskRef,
}: {
  project: ProjectDetail;
  taskRef: string;
}) {
  const queryClient = useQueryClient();
  const student = isStudent(project);
  const [decision, setDecision] = useState("");
  const [relevance, setRelevance] = useState("");
  const [picked, setPicked] = useState<string[]>([]);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [notice, setNotice] = useState("");

  const results = useQuery({
    queryKey: [
      "supervision",
      "literature",
      "results",
      project.project_id,
      taskRef,
      decision,
      relevance,
    ],
    queryFn: () =>
      listLiteratureResults(project.project_id, {
        task_ref: taskRef,
        decision,
        relevance,
        limit: 100,
      }),
  });

  const extraction = useQuery({
    queryKey: ["supervision", "literature", "extraction", project.project_id, expanded],
    queryFn: () => getLiteratureExtraction(project.project_id, expanded as string),
    enabled: Boolean(expanded),
  });

  function invalidate() {
    queryClient.invalidateQueries({ queryKey: ["supervision", "literature"] });
  }

  const extract = useMutation({
    mutationFn: (ref: string) => runLiteratureDeepExtract(project.project_id, ref),
    onSuccess: (saved) => {
      setNotice("");
      if (saved.extract_state === "failed") {
        toast.warning(`${saved.code} 深度提取失败；失败已记录，其它论文不受影响`);
      } else {
        toast.success(`${saved.code} 深度提取完成`);
      }
      invalidate();
    },
    onError: (error) => {
      if (isCapabilityUnavailable(error)) {
        setNotice(`${MODEL_UNAVAILABLE_TEXT}\n后端说明：${errorText(error)}`);
        return;
      }
      toast.error(errorText(error));
    },
  });

  const compare = useMutation({
    mutationFn: () => compareLiteraturePapers(project.project_id, taskRef, picked),
    onSuccess: () => {
      setNotice("");
      toast.success("多篇比较完成（一次最多 6 篇）");
      invalidate();
    },
    onError: (error) => {
      if (isCapabilityUnavailable(error)) {
        setNotice(`${MODEL_UNAVAILABLE_TEXT}\n后端说明：${errorText(error)}`);
        return;
      }
      toast.error(errorText(error));
    },
  });

  const rows = results.data ?? [];

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="text-sm font-medium">候选论文（{rows.length}）</h3>
        <Select value={decision === "" ? "all" : decision} onValueChange={(value) => setDecision(value === "all" ? "" : value)}>
          <SelectTrigger className="h-9 w-[160px]">
            <SelectValue placeholder="全部决定" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">全部决定</SelectItem>
            <SelectItem value="pending">未决定</SelectItem>
            <SelectItem value="included">已纳入</SelectItem>
            <SelectItem value="excluded">已排除</SelectItem>
            <SelectItem value="read_later">稍后阅读</SelectItem>
          </SelectContent>
        </Select>
        <Select value={relevance === "" ? "all" : relevance} onValueChange={(value) => setRelevance(value === "all" ? "" : value)}>
          <SelectTrigger className="h-9 w-[160px]">
            <SelectValue placeholder="全部等级" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">全部等级</SelectItem>
            <SelectItem value="high">高度相关</SelectItem>
            <SelectItem value="relevant">相关</SelectItem>
            <SelectItem value="reference">可参考</SelectItem>
            <SelectItem value="low">相关性低</SelectItem>
          </SelectContent>
        </Select>
        {student && (
          <Button
            size="sm"
            variant="outline"
            onClick={() => compare.mutate()}
            disabled={compare.isPending || picked.length < 2}
          >
            比较所选 {picked.length} 篇
          </Button>
        )}
      </div>

      {notice && <AlertPanel tone="warning" title="这一步暂时做不了，但不是失败" description={notice} />}
      {results.isError && (
        <AlertPanel tone="error" title="候选论文加载失败" description={errorText(results.error)} />
      )}
      {!results.isLoading && !results.isError && rows.length === 0 && (
        <AlertPanel
          title="还没有候选论文"
          description="先在上方生成并确认检索方案，再点「开始检索」。外部来源未接入时会给出说明，不会假装搜到了结果。"
        />
      )}

      <ul className="space-y-2">
        {rows.map((row) => (
          <li key={row.result_id} className="space-y-2 rounded-md border p-3">
            <div className="flex flex-wrap items-center gap-2">
              {student && (
                <input
                  type="checkbox"
                  className="mt-1"
                  checked={picked.includes(row.code)}
                  onChange={(event) =>
                    setPicked((current) =>
                      event.target.checked
                        ? [...current, row.code]
                        : current.filter((code) => code !== row.code),
                    )
                  }
                />
              )}
              <span className="min-w-0 flex-1 text-sm font-medium">
                {row.title}
                {row.authors ? `（${row.authors}）` : ""}
                {row.year ? ` ${row.year}` : ""}
              </span>
              <Badge variant="secondary">{labelOf(RELEVANCE_LABEL, row.relevance_level)}</Badge>
              <Badge variant="outline">{labelOf(DECISION_LABEL, row.decision)}</Badge>
              <Badge variant="outline">{labelOf(EXTRACT_STATE_LABEL, row.extract_state)}</Badge>
              <span className="text-xs text-muted-foreground">
                {row.code} · 来源 {row.source}
                {row.doi ? ` · doi ${row.doi}` : ""}
                {row.arxiv_id ? ` · arXiv ${row.arxiv_id}` : ""}
              </span>
            </div>

            <ul className="text-xs text-muted-foreground">
              {relevanceReasonLines(row).map((line) => (
                <li key={line}>{line}</li>
              ))}
            </ul>

            {row.abstract && (
              <details className="text-sm">
                <summary className="cursor-pointer text-muted-foreground">摘要</summary>
                <p className="mt-1 whitespace-pre-wrap">{row.abstract}</p>
              </details>
            )}

            {row.decision !== "pending" && (
              <div className="text-xs text-muted-foreground">
                {row.decided_by}（{row.decided_role}）决定：{row.decision_reason}
              </div>
            )}

            {Object.keys(row.semantic_analysis ?? {}).length > 0 && (
              <AlertPanel
                title="AI 语义分析（附加说明，不覆盖规则排序）"
                description={JSON.stringify(row.semantic_analysis)}
              />
            )}

            <div className="flex flex-wrap items-center gap-2">
              {student &&
                DECISION_OPTIONS.map((option) => (
                  <DecisionButton
                    key={option.value}
                    project={project}
                    row={row}
                    value={option.value}
                    label={option.label}
                    onDone={() => undefined}
                  />
                ))}
              {student && (
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => extract.mutate(row.code)}
                  disabled={
                    extract.isPending ||
                    row.extract_state === "running" ||
                    row.extract_state === "completed"
                  }
                >
                  深度提取
                </Button>
              )}
              <Button
                size="sm"
                variant="ghost"
                onClick={() => setExpanded(expanded === row.code ? null : row.code)}
              >
                {expanded === row.code ? "收起提取与证据" : "查看提取与证据卡片"}
              </Button>
            </div>

            {expanded === row.code && (
              <div className="space-y-2 border-t pt-2">
                {extraction.isLoading && expanded === row.code && (
                  <p className="text-xs text-muted-foreground">读取提取结果…</p>
                )}
                {extraction.data && <ExtractionBlock extraction={extraction.data} />}
                {!extraction.isError && !extraction.data && !extraction.isLoading && (
                  <p className="text-xs text-muted-foreground">这篇还没有深度提取结果。</p>
                )}
                <EvidenceCardForm project={project} row={row} extraction={extraction.data ?? null} />
                <ul className="space-y-1">
                  {row.evidence_cards.map((card) => (
                    <li key={card.card_id} className="rounded-md border border-dashed p-2 text-sm">
                      <div className="text-xs text-muted-foreground">
                        {card.code} · {card.evidence_type || "未分类"} ·{" "}
                        {labelOf(SUPPORT_RELATION_LABEL, card.support_relation)} ·{" "}
                        {card.level === "abstract" ? "摘要级定位" : card.level || "未标注层级"} ·{" "}
                        {card.quote_verified ? "引句已逐字核对" : "引句未能逐字核对"}
                      </div>
                      <div className="mt-1">{card.quote}</div>
                      {card.summary && (
                        <div className="mt-1 text-xs text-muted-foreground">说明：{card.summary}</div>
                      )}
                    </li>
                  ))}
                  {row.evidence_cards.length === 0 && (
                    <li className="text-xs text-muted-foreground">还没有证据卡片。</li>
                  )}
                </ul>
              </div>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

/** 决定按钮：点开后必须写理由才能提交（后端 reason 必填）。 */
function DecisionButton({
  project,
  row,
  value,
  label,
}: {
  project: ProjectDetail;
  row: LiteratureResult;
  value: string;
  label: string;
  onDone: () => void;
}) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");

  const decide = useMutation({
    mutationFn: () =>
      decideLiteratureResult(project.project_id, row.code, { decision: value, reason: reason.trim() }),
    onSuccess: (saved) => {
      setOpen(false);
      setReason("");
      toast.success(`${saved.code} 已标记为「${labelOf(DECISION_LABEL, saved.decision)}」`);
      queryClient.invalidateQueries({ queryKey: ["supervision", "literature"] });
    },
    onError: (error) => toast.error(errorText(error)),
  });

  return (
    <span className="contents">
      <Button size="sm" variant="outline" onClick={() => setOpen((current) => !current)}>
        {label}
      </Button>
      {open && (
        <span className="flex w-full flex-wrap items-center gap-2">
          <Input
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            placeholder={`为什么「${label}」（必填，会记进决定历史）`}
            className="min-w-[16rem] flex-1"
          />
          <Button
            size="sm"
            onClick={() => decide.mutate()}
            disabled={decide.isPending || reason.trim() === ""}
          >
            确认
          </Button>
          <Button size="sm" variant="ghost" onClick={() => setOpen(false)}>
            取消
          </Button>
        </span>
      )}
    </span>
  );
}

/** 加一张带原文定位的证据卡片；同一证据重复提交不会产生重复卡片。 */
function EvidenceCardForm({
  project,
  row,
  extraction,
}: {
  project: ProjectDetail;
  row: LiteratureResult;
  extraction: LiteratureExtraction | null;
}) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [quote, setQuote] = useState("");
  const [summary, setSummary] = useState("");
  const [support, setSupport] = useState("");
  const [locator, setLocator] = useState("");

  const create = useMutation({
    mutationFn: () =>
      createLiteratureEvidenceCard(project.project_id, {
        result_ref: row.code,
        quote: quote.trim(),
        summary: summary.trim(),
        support_relation: support,
        level: extraction?.level ?? "abstract",
        extraction_id: extraction?.extraction_id ?? "",
        locator: locator.trim() ? { note: locator.trim() } : {},
      }),
    onSuccess: (card) => {
      setOpen(false);
      setQuote("");
      setSummary("");
      setLocator("");
      toast.success(
        card.quote_verified
          ? `证据卡片 ${card.code} 已建，引句已逐字核对`
          : `证据卡片 ${card.code} 已建，但引句没能在标题+摘要里逐字对上（v1 只有摘要级定位）`,
      );
      queryClient.invalidateQueries({ queryKey: ["supervision", "literature"] });
    },
    onError: (error) => toast.error(errorText(error)),
  });

  if (!isStudent(project)) return null;

  return (
    <div className="space-y-2">
      <Button size="sm" variant="outline" onClick={() => setOpen((current) => !current)}>
        加证据卡片
      </Button>
      {open && (
        <div className="space-y-2 rounded-md border border-dashed p-2">
          <div className="space-y-1">
            <Label htmlFor={`lit-quote-${row.code}`} className="text-xs text-muted-foreground">
              原文引句（必填，系统会逐字核对）
            </Label>
            <Textarea
              id={`lit-quote-${row.code}`}
              rows={2}
              value={quote}
              onChange={(event) => setQuote(event.target.value)}
            />
          </div>
          <div className="space-y-1">
            <Label htmlFor={`lit-summary-${row.code}`} className="text-xs text-muted-foreground">
              这条证据说明什么
            </Label>
            <Input
              id={`lit-summary-${row.code}`}
              value={summary}
              onChange={(event) => setSummary(event.target.value)}
            />
          </div>
          <div className="space-y-1">
            <Label className="text-xs text-muted-foreground">支撑关系</Label>
            <Select value={support === "" ? "none" : support} onValueChange={(value) => setSupport(value === "none" ? "" : value)}>
              <SelectTrigger className="h-9 w-[220px]">
                <SelectValue placeholder="未标注支撑关系" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="none">未标注支撑关系</SelectItem>
                {Object.entries(SUPPORT_RELATION_LABEL)
                  .filter(([key]) => key !== "")
                  .map(([key, text]) => (
                    <SelectItem key={key} value={key}>
                      {text}
                    </SelectItem>
                  ))}
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1">
            <Label htmlFor={`lit-locator-${row.code}`} className="text-xs text-muted-foreground">
              定位补充（可选；v1 只有摘要级，没有页码）
            </Label>
            <Input
              id={`lit-locator-${row.code}`}
              value={locator}
              onChange={(event) => setLocator(event.target.value)}
              placeholder="例如：摘要第 2 句"
            />
          </div>
          <div className="flex gap-2">
            <Button size="sm" onClick={() => create.mutate()} disabled={create.isPending || quote.trim() === ""}>
              保存证据卡片
            </Button>
            <Button size="sm" variant="outline" onClick={() => setOpen(false)}>
              取消
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
