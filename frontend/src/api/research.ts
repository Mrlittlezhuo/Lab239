import { deleteJson, getJson, patchJson, postJson, putJson } from "@/api/http";

export interface ResearchProject {
  project_id: string;
  name: string;
  research_question: string;
  scope: string;
  status: string; // active/archived
  current_stage: string | null;
  create_time: string | null;
  update_time: string | null;
}

export interface ResearchPlan {
  summary?: string;
  sub_questions?: { id?: string; question?: string; queries?: string[]; sources?: string[] }[];
  sources?: string[];
  [key: string]: unknown;
}

export interface ResearchRun {
  run_id: string;
  project_id: string;
  status: string; // pending/awaiting_confirmation/running/completed/failed/cancelled
  stage: string | null;
  plan: ResearchPlan;
  source_stats: { source?: string; queries?: number; retrieved?: number; kept?: number; error?: string }[];
  summary: Record<string, unknown>;
  agent_run_id: string | null;
  error: string | null;
  create_time: string | null;
  start_time: string | null;
  end_time: string | null;
}

export interface ResearchPaper {
  project_paper_id: string;
  paper_id: string;
  title: string;
  authors: string;
  year: number | null;
  abstract: string;
  doi: string;
  arxiv_id: string;
  url: string;
  source: string;
  sources: { source?: string; query?: string }[];
  fulltext_status: string;
  relevance: string;
  screening_status: string; // pending_review/included/excluded
  screening_reason: string;
  first_run_id: string;
  /** 该论文在本项目内的原文可得性：unknown/none/abstract/fulltext */
  fulltext_state: string;
  unit_count: number;
  fulltext_time: string | null;
  create_time: string | null;
}

export interface PaperUnit {
  unit_id: string;
  paper_id: string;
  unit_kind: string; // block/abstract
  page: number | null;
  section: string;
  block_index: number;
  text: string;
  char_count: number;
  source_kind: string;
  create_time: string | null;
}

export interface EvidenceLocator {
  page?: number | null;
  section?: string;
  block_index?: number | null;
}

export interface EvidenceCard {
  evidence_id: string;
  project_id: string;
  paper_id: string;
  paper_title?: string;
  run_id: string | null;
  sub_question_id: string;
  evidence_type: string;
  summary: string;
  quote: string;
  level: string; // fulltext/abstract
  quote_verified: boolean;
  unit_id: string | null;
  locator: EvidenceLocator;
  source_kind: string;
  extraction_model: string;
  extraction_mode: string;
  review_status: string; // pending/confirmed/edited/rejected
  review_note: string;
  reviewed_at: string | null;
  version: number;
  create_time: string | null;
  update_time: string | null;
}

export interface FulltextResult {
  state: string;
  created: number;
  changed: boolean;
  fulltext_hash: string;
  unit_count: number;
  detail: string;
  source_kind: string;
}

export interface ExtractResult {
  created: number;
  updated: number;
  unchanged: number;
  kept_reviewed: number;
  rejected: string[];
  total: number;
  detail?: FulltextResult | null;
}

export interface ReverifyResult {
  verified: number;
  unverified: number;
  total: number;
}

export interface Dimension {
  id: string;
  label: string;
}

export interface ComparisonUnit {
  comparison_id: string;
  project_id: string;
  paper_id: string;
  paper_title?: string;
  dimension_id: string;
  content: string;
  evidence_ids: string[];
  level: string;
  run_id: string | null;
  version: number;
  update_time: string | null;
}

export interface ConflictSide {
  evidence_id: string;
  paper_id: string;
  quote: string;
}

export interface ResearchConflict {
  conflict_id: string;
  project_id: string;
  run_id: string | null;
  sub_question_id: string;
  dimension_id: string;
  topic: string;
  description: string;
  side_a: ConflictSide[];
  side_b: ConflictSide[];
  status: string; // open/resolved
  resolution: string;
  version: number;
  create_time: string | null;
  update_time: string | null;
}

export interface CoverageRow {
  id: string;
  question: string;
  queries: string[];
  evidence_count: number;
  paper_count: number;
  paper_ids: string[];
  abstract_only: boolean;
  unverified_only: boolean;
  verified_count: number;
  levels: string[];
  status?: string; // covered/thin/missing
}

export interface Coverage {
  sub_questions: CoverageRow[];
  dimensions: CoverageRow[];
  missing_dimensions: { id: string; label: string }[];
  papers_without_evidence: string[];
  conflicts: { conflict_id: string; topic: string; status: string }[];
  suggestions: {
    sub_question_id: string;
    question: string;
    reason: string;
    queries: string[];
    requires_confirmation: boolean;
  }[];
  totals: {
    evidence: number;
    sub_questions: number;
    covered: number;
    thin: number;
    missing: number;
    open_conflicts: number;
  };
}

export interface SupplementaryRun {
  run_id: string;
  project_id: string;
  parent_run_id: string | null;
  round_no: number;
  max_rounds: number;
  status: string;
}

export interface ReportCitation {
  marker: string;
  evidence_id: string;
  paper_id: string;
  paper_title: string;
  quote: string;
  level: string;
  locator: EvidenceLocator;
  verified: boolean;
}

export interface ResearchReport {
  report_id: string;
  report_key: string;
  project_id: string;
  run_id: string | null;
  title: string;
  version: number;
  status: string; // generated/edited/failed
  content: string;
  citations: ReportCitation[];
  evidence_ids: string[];
  unsupported: string;
  generated_by: string;
  error: string | null;
  create_time: string | null;
  update_time: string | null;
  warnings?: string[];
}

export function listProjects(status?: string): Promise<ResearchProject[]> {
  return getJson<{ projects: ResearchProject[] }>("/research/projects", {
    query: { status },
  }).then((r) => r.projects);
}

export function createProject(input: {
  name: string;
  research_question: string;
  scope?: string;
}): Promise<ResearchProject> {
  return postJson<ResearchProject>("/research/projects", input);
}

export function updateProject(
  id: string,
  patch: Partial<Pick<ResearchProject, "name" | "research_question" | "scope" | "status">>,
): Promise<ResearchProject> {
  return patchJson<ResearchProject>(`/research/projects/${id}`, patch);
}

export function deleteProject(id: string): Promise<unknown> {
  return deleteJson(`/research/projects/${id}`);
}

export function listRuns(projectId: string): Promise<ResearchRun[]> {
  return getJson<{ runs: ResearchRun[] }>(`/research/projects/${projectId}/runs`).then(
    (r) => r.runs,
  );
}

export function createRun(projectId: string): Promise<ResearchRun> {
  return postJson<ResearchRun>(`/research/projects/${projectId}/runs`);
}

export function getRun(runId: string): Promise<ResearchRun> {
  return getJson<ResearchRun>(`/research/runs/${runId}`);
}

export function generatePlan(runId: string): Promise<ResearchRun> {
  return postJson<ResearchRun>(`/research/runs/${runId}/plan`);
}

export function updatePlan(runId: string, plan: ResearchPlan): Promise<ResearchRun> {
  return putJson<ResearchRun>(`/research/runs/${runId}/plan`, { plan });
}

export function confirmRun(runId: string): Promise<ResearchRun> {
  return postJson<ResearchRun>(`/research/runs/${runId}/confirm`);
}

export function resumeRun(runId: string): Promise<ResearchRun> {
  return postJson<ResearchRun>(`/research/runs/${runId}/resume`);
}

export function cancelRun(runId: string): Promise<Record<string, unknown>> {
  return postJson(`/research/runs/${runId}/cancel`);
}

/** 纳入论文后基于该运行继续产出证据/比较/报告（任务 018 阶段 E）。 */
export function runArtifacts(runId: string): Promise<ResearchRun> {
  return postJson<ResearchRun>(`/research/runs/${runId}/artifacts`);
}

export function listPapers(
  projectId: string,
  screeningStatus?: string,
): Promise<ResearchPaper[]> {
  return getJson<{ papers: ResearchPaper[] }>(`/research/projects/${projectId}/papers`, {
    query: { screening_status: screeningStatus },
  }).then((r) => r.papers);
}

export function setScreening(
  projectPaperId: string,
  screeningStatus: string,
  reason = "",
): Promise<ResearchPaper> {
  return patchJson<ResearchPaper>(`/research/papers/${projectPaperId}/screening`, {
    screening_status: screeningStatus,
    reason,
  });
}

// ---------------------------------------------------------------------------
// 阶段 A：原文单元与证据卡片
// ---------------------------------------------------------------------------

/** 解析并入库该论文在本项目内的原文单元（幂等，取不到全文会如实降级）。 */
export function ingestFulltext(
  projectId: string,
  paperId: string,
  body: { run_id?: string; kb_names?: string[] } = {},
): Promise<FulltextResult> {
  return postJson<FulltextResult>(
    `/research/projects/${projectId}/papers/${paperId}/fulltext`,
    body,
  );
}

export function listUnits(projectId: string, paperId: string): Promise<PaperUnit[]> {
  return getJson<{ units: PaperUnit[] }>(
    `/research/projects/${projectId}/papers/${paperId}/units`,
  ).then((r) => r.units);
}

export function extractEvidence(
  projectId: string,
  paperId: string,
  body: {
    sub_question_id: string;
    sub_question: string;
    question?: string;
    run_id?: string;
    model?: string;
  },
): Promise<ExtractResult> {
  return postJson<ExtractResult>(
    `/research/projects/${projectId}/papers/${paperId}/evidence`,
    body,
  );
}

export function listEvidence(
  projectId: string,
  filters: {
    paper_id?: string;
    sub_question_id?: string;
    review_status?: string;
    include_rejected?: boolean;
  } = {},
): Promise<EvidenceCard[]> {
  return getJson<{ evidence: EvidenceCard[] }>(`/research/projects/${projectId}/evidence`, {
    query: { ...filters },
  }).then((r) => r.evidence);
}

export function reviewEvidence(
  evidenceId: string,
  body: {
    review_status: string;
    summary?: string;
    quote?: string;
    note?: string;
  },
): Promise<EvidenceCard> {
  return patchJson<EvidenceCard>(`/research/evidence/${evidenceId}`, body);
}

export function deleteEvidence(evidenceId: string): Promise<unknown> {
  return deleteJson(`/research/evidence/${evidenceId}`);
}

export function reverifyEvidence(projectId: string): Promise<ReverifyResult> {
  return postJson<ReverifyResult>(`/research/projects/${projectId}/evidence/reverify`);
}

// ---------------------------------------------------------------------------
// 阶段 B：比较维度、比较矩阵、冲突、覆盖与补充检索
// ---------------------------------------------------------------------------

export function getDimensions(projectId: string): Promise<Dimension[]> {
  return getJson<{ dimensions: Dimension[] }>(`/research/projects/${projectId}/dimensions`).then(
    (r) => r.dimensions,
  );
}

export function setDimensions(projectId: string, dimensions: Dimension[]): Promise<Dimension[]> {
  return putJson<{ dimensions: Dimension[] }>(`/research/projects/${projectId}/dimensions`, {
    dimensions,
  }).then((r) => r.dimensions);
}

export function generateComparisons(
  projectId: string,
  body: {
    run_id?: string;
    dimension_ids?: string[];
    model?: string;
    detect_conflicts?: boolean;
  } = {},
): Promise<{
  written: number;
  skipped: number;
  conflicts: number;
  rejected: string[];
  dimensions: Dimension[];
  evidence_used: number;
}> {
  return postJson(`/research/projects/${projectId}/comparisons`, body);
}

export function listComparisons(
  projectId: string,
  filters: { paper_id?: string; dimension_id?: string } = {},
): Promise<ComparisonUnit[]> {
  return getJson<{ comparisons: ComparisonUnit[] }>(
    `/research/projects/${projectId}/comparisons`,
    { query: { ...filters } },
  ).then((r) => r.comparisons);
}

export function listConflicts(projectId: string, status?: string): Promise<ResearchConflict[]> {
  return getJson<{ conflicts: ResearchConflict[] }>(`/research/projects/${projectId}/conflicts`, {
    query: { status },
  }).then((r) => r.conflicts);
}

export function resolveConflict(
  conflictId: string,
  resolution: string,
): Promise<ResearchConflict> {
  return patchJson<ResearchConflict>(`/research/conflicts/${conflictId}`, { resolution });
}

export function deleteConflict(conflictId: string): Promise<unknown> {
  return deleteJson(`/research/conflicts/${conflictId}`);
}

export function getCoverage(projectId: string): Promise<Coverage> {
  return getJson<Coverage>(`/research/projects/${projectId}/coverage`);
}

/** 基于上一轮运行创建补充检索的新运行（仍需确认，轮次有上限）。 */
export function supplementRun(runId: string): Promise<SupplementaryRun> {
  return postJson<SupplementaryRun>(`/research/runs/${runId}/supplement`);
}

// ---------------------------------------------------------------------------
// 阶段 C：版本化报告与导出
// ---------------------------------------------------------------------------

export function generateReport(
  projectId: string,
  body: { run_id?: string; model?: string } = {},
): Promise<ResearchReport> {
  return postJson<ResearchReport>(`/research/projects/${projectId}/reports`, body);
}

export function listReports(projectId: string, includeVersions = false): Promise<ResearchReport[]> {
  return getJson<{ reports: ResearchReport[] }>(`/research/projects/${projectId}/reports`, {
    query: { include_versions: includeVersions },
  }).then((r) => r.reports);
}

export function getReport(reportId: string): Promise<ResearchReport> {
  return getJson<ResearchReport>(`/research/reports/${reportId}`);
}

/** 保存编辑：后端插入新版本，旧版本不被覆盖。 */
export function editReport(
  reportId: string,
  body: { content: string; title?: string },
): Promise<ResearchReport> {
  return patchJson<ResearchReport>(`/research/reports/${reportId}`, body);
}

export function deleteReport(reportId: string): Promise<unknown> {
  return deleteJson(`/research/reports/${reportId}`);
}

/** 导出 Markdown（引用渲染为脚注）。返回原始文本，由调用方触发下载。 */
export function exportReportMarkdown(reportId: string): Promise<string> {
  return getJson<string>(`/research/reports/${reportId}/export.md`);
}

export function exportReportBibtex(reportId: string): Promise<string> {
  return getJson<string>(`/research/reports/${reportId}/export.bib`);
}
