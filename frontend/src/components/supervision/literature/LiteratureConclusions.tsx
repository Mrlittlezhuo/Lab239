import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import {
  createLiteratureConclusion,
  listLiteratureConclusions,
  listLiteratureResults,
  mentorReviewLiteratureConclusion,
  reviseLiteratureConclusion,
} from "@/api/supervision";
import type { LiteratureConclusion, ProjectDetail } from "@/api/supervision";
import { AlertPanel, errorText } from "@/components/supervision/AlertPanel";
import {
  CONCLUSION_STATUS_LABEL,
  MENTOR_STATE_LABEL,
  conclusionStalenessNote,
  isStudent,
  labelOf,
} from "@/components/supervision/literature/literatureModel";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";

interface Row {
  key: number;
  statement: string;
  extra: string;
  code: string;
}

const EMPTY_ROWS: Row[] = [];
let rowKey = 0;
function nextKey(): number {
  rowKey += 1;
  return rowKey;
}

function lines(value: string): string[] {
  return value
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line !== "");
}

function codes(value: string): string[] {
  return value
    .split(/[,，\s]+/)
    .map((item) => item.trim())
    .filter((item) => item !== "");
}

/** 一行可编辑的条目（陈述 + 一个附加字段 + 可选的论文编号）。 */
function RowEditor({
  rows,
  onChange,
  statementLabel,
  extraLabel,
  showCode,
  codeOptions,
}: {
  rows: Row[];
  onChange: (next: Row[]) => void;
  statementLabel: string;
  extraLabel?: string;
  showCode?: boolean;
  codeOptions?: string[];
}) {
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs font-medium text-muted-foreground">{statementLabel}</span>
        <Button
          size="sm"
          variant="outline"
          onClick={() => onChange([...rows, { key: nextKey(), statement: "", extra: "", code: "" }])}
        >
          加一条
        </Button>
      </div>
      {rows.map((row, index) => (
        <div key={row.key} className="flex flex-wrap items-center gap-2">
          <Input
            value={row.statement}
            onChange={(event) =>
              onChange(
                rows.map((item, position) =>
                  position === index ? { ...item, statement: event.target.value } : item,
                ),
              )
            }
            placeholder="写一条"
            className="min-w-[16rem] flex-1"
          />
          {extraLabel && (
            <Input
              value={row.extra}
              onChange={(event) =>
                onChange(
                  rows.map((item, position) =>
                    position === index ? { ...item, extra: event.target.value } : item,
                  ),
                )
              }
              placeholder={extraLabel}
              className="min-w-[12rem] flex-1"
            />
          )}
          {showCode && (
            <Input
              value={row.code}
              onChange={(event) =>
                onChange(
                  rows.map((item, position) =>
                    position === index ? { ...item, code: event.target.value } : item,
                  ),
                )
              }
              placeholder={codeOptions ? `论文编号（${codeOptions.slice(0, 3).join("、")}…）` : "论文编号 P-xxx"}
              className="w-[14rem]"
            />
          )}
          <Button size="sm" variant="ghost" onClick={() => onChange(rows.filter((_, position) => position !== index))}>
            删除
          </Button>
        </div>
      ))}
      {rows.length === 0 && (
        <p className="text-xs text-muted-foreground">（还没有条目）</p>
      )}
    </div>
  );
}

/**
 * 文献研究结论：论文事实 / 学生判断 / 待验证假设必须分开写（§4.5）。
 *
 * 口径：论文事实必须指向当前已纳入的论文（否则后端 400）；结论绑定当时的
 * 文献快照与哈希，纳入集合变了会标出「依据已变」，修订只追加新版本，
 * 旧版本标 superseded 保留；导师只能确认/质疑/要求补充证据。
 */
export function LiteratureConclusions({
  project,
  taskRef,
}: {
  project: ProjectDetail;
  taskRef: string;
}) {
  const queryClient = useQueryClient();
  const student = isStudent(project);
  const [editing, setEditing] = useState(false);
  const [target, setTarget] = useState<LiteratureConclusion | null>(null);
  const [statement, setStatement] = useState("");
  const [facts, setFacts] = useState<Row[]>(EMPTY_ROWS);
  const [judgements, setJudgements] = useState<Row[]>(EMPTY_ROWS);
  const [hypotheses, setHypotheses] = useState<Row[]>(EMPTY_ROWS);
  const [gaps, setGaps] = useState("");
  const [cardCodes, setCardCodes] = useState("");
  const [confirm, setConfirm] = useState(false);
  const [reviewTarget, setReviewTarget] = useState<string | null>(null);
  const [reviewState, setReviewState] = useState("");
  const [reviewNote, setReviewNote] = useState("");

  const conclusions = useQuery({
    queryKey: ["supervision", "literature", "conclusions", project.project_id],
    queryFn: () => listLiteratureConclusions(project.project_id, { include_superseded: true }),
  });

  const included = useQuery({
    queryKey: ["supervision", "literature", "results", project.project_id, taskRef, "included"],
    queryFn: () => listLiteratureResults(project.project_id, { task_ref: taskRef, decision: "included", limit: 100 }),
  });

  const includedCodes = (included.data ?? []).map((row) => row.code);

  function invalidate() {
    queryClient.invalidateQueries({ queryKey: ["supervision", "literature"] });
  }

  function fillFrom(conclusion: LiteratureConclusion | null) {
    setTarget(conclusion);
    setStatement(conclusion?.statement ?? "");
    setFacts(
      (conclusion?.supported_facts ?? []).map((item) => ({
        key: nextKey(),
        statement: String(item.statement ?? ""),
        extra: String(item.evidence_card_code ?? ""),
        code: String(item.result_code ?? ""),
      })),
    );
    setJudgements(
      (conclusion?.student_judgements ?? []).map((item) => ({
        key: nextKey(),
        statement: String(item.statement ?? ""),
        extra: ((item.basis_result_codes as string[] | undefined) ?? []).join(" "),
        code: "",
      })),
    );
    setHypotheses(
      (conclusion?.unverified_hypotheses ?? []).map((item) => ({
        key: nextKey(),
        statement: String(item.statement ?? ""),
        extra: String(item.how_to_verify ?? ""),
        code: "",
      })),
    );
    setGaps((conclusion?.research_gaps ?? []).join("\n"));
    setCardCodes(
      (conclusion?.evidence_cards ?? [])
        .map((item) => String(item.code ?? ""))
        .filter((code) => code !== "")
        .join(" "),
    );
    setConfirm(false);
  }

  const body = () => ({
    statement: statement.trim(),
    supported_facts: facts
      .filter((row) => row.statement.trim() !== "")
      .map((row) => ({
        statement: row.statement.trim(),
        result_code: row.code.trim(),
        evidence_card_code: row.extra.trim(),
      })),
    student_judgements: judgements
      .filter((row) => row.statement.trim() !== "")
      .map((row) => ({
        statement: row.statement.trim(),
        basis_result_codes: codes(row.extra),
      })),
    unverified_hypotheses: hypotheses
      .filter((row) => row.statement.trim() !== "")
      .map((row) => ({
        statement: row.statement.trim(),
        how_to_verify: row.extra.trim(),
      })),
    research_gaps: lines(gaps),
    evidence_card_codes: codes(cardCodes),
    task_ref: taskRef,
    confirm,
  });

  const create = useMutation({
    mutationFn: () => createLiteratureConclusion(project.project_id, body()),
    onSuccess: (saved) => {
      setEditing(false);
      toast.success(
        saved.status === "confirmed"
          ? `文献结论 ${saved.code} v${saved.version} 已确认（绑定 ${saved.literature_count} 篇文献快照）`
          : `文献结论草稿 ${saved.code} v${saved.version} 已保存`,
      );
      invalidate();
    },
    onError: (error) => toast.error(errorText(error)),
  });

  const revise = useMutation({
    mutationFn: () => reviseLiteratureConclusion(project.project_id, (target as LiteratureConclusion).code, body()),
    onSuccess: (saved) => {
      setEditing(false);
      setTarget(null);
      toast.success(`已生成新版本 ${saved.code} v${saved.version}，旧版本保留为历史`);
      invalidate();
    },
    onError: (error) => toast.error(errorText(error)),
  });

  const mentorReview = useMutation({
    mutationFn: () =>
      mentorReviewLiteratureConclusion(project.project_id, reviewTarget as string, {
        state: reviewState,
        note: reviewNote.trim(),
      }),
    onSuccess: (saved) => {
      setReviewTarget(null);
      setReviewState("");
      setReviewNote("");
      toast.success(`已记录导师意见：${labelOf(MENTOR_STATE_LABEL, saved.mentor_state)}`);
      invalidate();
    },
    onError: (error) => toast.error(errorText(error)),
  });

  const rows = conclusions.data ?? [];

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="text-sm font-medium">文献研究结论</h3>
        {student && (
          <Button
            size="sm"
            variant="outline"
            onClick={() => {
              fillFrom(null);
              setEditing((current) => !current);
            }}
            disabled={includedCodes.length === 0}
          >
            新建结论
          </Button>
        )}
        {student && includedCodes.length === 0 && (
          <span className="text-xs text-muted-foreground">
            还没有已纳入的论文：先在候选列表里决定「纳入」，才能写论文事实。
          </span>
        )}
      </div>

      {editing && (
        <div className="space-y-3 rounded-md border border-dashed p-3">
          <p className="text-xs text-muted-foreground">
            论文事实、学生判断与待验证假设必须分开：论文事实要指向当前已纳入的论文编号
            （{includedCodes.slice(0, 4).join("、") || "暂无"}），
            自己的推断写进学生判断，还没验证的写进待验证假设并写清怎么验证。
          </p>
          <div className="space-y-1">
            <Label htmlFor="lit-conclusion-statement" className="text-xs text-muted-foreground">
              结论表述（必填）
            </Label>
            <Textarea
              id="lit-conclusion-statement"
              rows={3}
              value={statement}
              onChange={(event) => setStatement(event.target.value)}
            />
          </div>
          <RowEditor
            rows={facts}
            onChange={setFacts}
            statementLabel="论文事实（每条都要写论文编号）"
            extraLabel="证据卡片编号（可选）"
            showCode
            codeOptions={includedCodes}
          />
          <RowEditor
            rows={judgements}
            onChange={setJudgements}
            statementLabel="我的判断"
            extraLabel="依据的论文编号（空格分隔）"
          />
          <RowEditor
            rows={hypotheses}
            onChange={setHypotheses}
            statementLabel="待验证假设"
            extraLabel="打算怎么验证"
          />
          <div className="space-y-1">
            <Label htmlFor="lit-conclusion-gaps" className="text-xs text-muted-foreground">
              文献空白（每行一条）
            </Label>
            <Textarea
              id="lit-conclusion-gaps"
              rows={2}
              value={gaps}
              onChange={(event) => setGaps(event.target.value)}
            />
          </div>
          <div className="space-y-1">
            <Label htmlFor="lit-conclusion-cards" className="text-xs text-muted-foreground">
              引用的证据卡片编号（空格分隔）
            </Label>
            <Input
              id="lit-conclusion-cards"
              value={cardCodes}
              onChange={(event) => setCardCodes(event.target.value)}
              placeholder="例如 EC-001 EC-002"
            />
          </div>
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={confirm} onChange={(event) => setConfirm(event.target.checked)} />
            同时确认这条结论（不勾选则只存草稿）
          </label>
          <div className="flex gap-2">
            {target ? (
              <Button size="sm" onClick={() => revise.mutate()} disabled={revise.isPending || statement.trim() === ""}>
                {revise.isPending ? "提交中…" : `修订 ${target.code}（生成新版本）`}
              </Button>
            ) : (
              <Button size="sm" onClick={() => create.mutate()} disabled={create.isPending || statement.trim() === ""}>
                {create.isPending ? "保存中…" : "保存结论"}
              </Button>
            )}
            <Button
              size="sm"
              variant="outline"
              onClick={() => {
                setEditing(false);
                setTarget(null);
              }}
            >
              取消
            </Button>
          </div>
        </div>
      )}

      {conclusions.isError && (
        <AlertPanel tone="error" title="结论加载失败" description={errorText(conclusions.error)} />
      )}
      {!conclusions.isLoading && !conclusions.isError && rows.length === 0 && (
        <AlertPanel
          title="还没有文献结论"
          description="结论从当前已纳入的论文集合生成，并绑定当时的文献快照；修订只追加新版本。"
        />
      )}

      <ul className="space-y-2">
        {rows.map((conclusion) => {
          const stale = conclusionStalenessNote(conclusion);
          return (
            <li key={conclusion.conclusion_id} className="space-y-2 rounded-md border p-3">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-medium">
                  {conclusion.code} v{conclusion.version}
                </span>
                <Badge variant={conclusion.status === "confirmed" ? "secondary" : "outline"}>
                  {labelOf(CONCLUSION_STATUS_LABEL, conclusion.status)}
                </Badge>
                {conclusion.mentor_state && (
                  <Badge variant="outline">{labelOf(MENTOR_STATE_LABEL, conclusion.mentor_state)}</Badge>
                )}
                <span className="text-xs text-muted-foreground">
                  绑定 {conclusion.literature_count} 篇 · 快照哈希 {conclusion.literature_snapshot_hash.slice(0, 12)} ·{" "}
                  {conclusion.created_by}（{conclusion.created_role}）
                </span>
              </div>

              <div className="text-sm">{conclusion.statement}</div>

              <div className="grid gap-2 text-sm md:grid-cols-3">
                <div className="space-y-1 rounded-md border border-dashed p-2">
                  <div className="text-xs font-medium text-muted-foreground">论文事实（文献支持）</div>
                  {conclusion.supported_facts.map((item, index) => (
                    <div key={index}>
                      {String(item.statement ?? "")}
                      <span className="text-xs text-muted-foreground">
                        （{String(item.result_code ?? "")}）
                      </span>
                    </div>
                  ))}
                  {conclusion.supported_facts.length === 0 && (
                    <div className="text-xs text-muted-foreground">（无）</div>
                  )}
                </div>
                <div className="space-y-1 rounded-md border border-dashed p-2">
                  <div className="text-xs font-medium text-muted-foreground">我的判断</div>
                  {conclusion.student_judgements.map((item, index) => (
                    <div key={index}>{String(item.statement ?? "")}</div>
                  ))}
                  {conclusion.student_judgements.length === 0 && (
                    <div className="text-xs text-muted-foreground">（无）</div>
                  )}
                </div>
                <div className="space-y-1 rounded-md border border-dashed p-2">
                  <div className="text-xs font-medium text-muted-foreground">待验证假设</div>
                  {conclusion.unverified_hypotheses.map((item, index) => (
                    <div key={index}>
                      {String(item.statement ?? "")}
                      {item.how_to_verify ? (
                        <span className="text-xs text-muted-foreground">
                          （怎么验证：{String(item.how_to_verify)}）
                        </span>
                      ) : null}
                    </div>
                  ))}
                  {conclusion.unverified_hypotheses.length === 0 && (
                    <div className="text-xs text-muted-foreground">（无）</div>
                  )}
                </div>
              </div>

              {conclusion.research_gaps.length > 0 && (
                <div className="text-xs text-muted-foreground">
                  文献空白：{conclusion.research_gaps.join("；")}
                </div>
              )}

              {stale && <AlertPanel tone="warning" title="结论依据已变化" description={stale} />}
              {conclusion.superseded_at && (
                <AlertPanel
                  title={`已被 ${conclusion.superseded_by_code} v${conclusion.superseded_by_version} 取代`}
                  description="旧版本保留为历史，不会被删除或改写。"
                />
              )}
              {conclusion.mentor_note && (
                <AlertPanel
                  title={`导师意见：${labelOf(MENTOR_STATE_LABEL, conclusion.mentor_state)}`}
                  description={`${conclusion.mentor_note}（${conclusion.mentor_by} · ${conclusion.mentor_role}）`}
                />
              )}

              <div className="flex flex-wrap gap-2">
                {student && conclusion.status !== "superseded" && (
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => {
                      fillFrom(conclusion);
                      setEditing(true);
                    }}
                  >
                    修订（生成新版本）
                  </Button>
                )}
                {!isStudent(project) && conclusion.status === "confirmed" && (
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => {
                      setReviewTarget(conclusion.code);
                      setReviewState("");
                      setReviewNote("");
                    }}
                  >
                    给出导师意见
                  </Button>
                )}
              </div>

              {reviewTarget === conclusion.code && (
                <div className="space-y-2 rounded-md border border-dashed p-2">
                  <div className="flex flex-wrap gap-2">
                    {[
                      ["confirmed", "确认"],
                      ["questioned", "质疑"],
                      ["evidence_required", "要求补充证据"],
                    ].map(([value, text]) => (
                      <Button
                        key={value}
                        size="sm"
                        variant={reviewState === value ? "default" : "outline"}
                        onClick={() => setReviewState(value)}
                      >
                        {text}
                      </Button>
                    ))}
                  </div>
                  <Textarea
                    rows={2}
                    value={reviewNote}
                    onChange={(event) => setReviewNote(event.target.value)}
                    placeholder="意见内容（会显示给学生，并记入审计）"
                  />
                  <div className="flex gap-2">
                    <Button
                      size="sm"
                      onClick={() => mentorReview.mutate()}
                      disabled={mentorReview.isPending || reviewState === ""}
                    >
                      提交导师意见
                    </Button>
                    <Button size="sm" variant="outline" onClick={() => setReviewTarget(null)}>
                      取消
                    </Button>
                  </div>
                </div>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
