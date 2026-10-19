import type { Submission } from "@/api/supervision";

/**
 * 提交快照 JSON 字段的防御性解析。
 *
 * `Submission.ac_snapshot / self_assessment / materials / completeness_check` 在接口上
 * 是 `Record<string, unknown>[]`：字段名由后端 `supervision_work_repository` 固定，
 * 但值可能缺失或类型不符。审核面板必须绑定提交当时的验收标准快照（§11.4），
 * 所以这里只解析快照，不回退到任务的当前版本。
 */

function str(value: unknown): string {
  return typeof value === "string" ? value : value == null ? "" : String(value);
}

function flag(value: unknown): boolean {
  return value === true || value === "true" || value === 1;
}

function num(value: unknown): number {
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

/** 提交当时的验收标准快照行（导师逐条结论只能针对这些 ac_code）。 */
export interface AcSnapshotRow {
  ac_code: string;
  statement: string;
  evidence_requirement: string;
  required: boolean;
  check_type: string;
  version: number;
}

export function parseAcSnapshot(
  rows: Record<string, unknown>[] | undefined,
): AcSnapshotRow[] {
  return (rows ?? [])
    .map((row) => ({
      ac_code: str(row.ac_code),
      statement: str(row.statement),
      evidence_requirement: str(row.evidence_requirement),
      required: flag(row.required),
      check_type: str(row.check_type),
      version: num(row.version),
    }))
    .filter((row) => row.ac_code !== "");
}

/** 学生提交时写的逐条自评。 */
export interface SelfAssessmentRow {
  ac_code: string;
  self_assessment: string;
  evidence: string;
  note: string;
}

export function parseSelfAssessmentRows(
  rows: Record<string, unknown>[] | undefined,
): SelfAssessmentRow[] {
  return (rows ?? []).map((row) => ({
    ac_code: str(row.ac_code),
    self_assessment: str(row.self_assessment),
    evidence: str(row.evidence),
    note: str(row.note),
  }));
}

/** 提交关联的材料（附件固定版本）。 */
export interface MaterialRow {
  attachment_code: string;
  name: string;
  kind: string;
  version: number;
  size: number;
}

export function parseMaterialRows(
  rows: Record<string, unknown>[] | undefined,
): MaterialRow[] {
  return (rows ?? [])
    .map((row) => ({
      attachment_code: str(row.attachment_code),
      name: str(row.name),
      kind: str(row.kind),
      version: num(row.version),
      size: num(row.size),
    }))
    .filter((row) => row.attachment_code !== "");
}

/** §6.4 完整性提醒：后端只警告，不阻止提交。 */
export interface CompletenessRow {
  key: string;
  passed: boolean;
  message: string;
}

export function parseCompletenessRows(
  rows: Record<string, unknown>[] | undefined,
): CompletenessRow[] {
  return (rows ?? []).map((row) => ({
    key: str(row.key),
    passed: flag(row.passed),
    message: str(row.message),
  }));
}

/** 快照 AC 与学生自评按 ac_code 关联后的一行。 */
export type AcRowWithAssessment = AcSnapshotRow & {
  assessment: SelfAssessmentRow | null;
};

/** 把快照 AC 与学生自评按 ac_code 关联，供逐条矩阵展示。 */
export function joinAcWithAssessment(
  acRows: AcSnapshotRow[],
  assessmentRows: SelfAssessmentRow[],
): AcRowWithAssessment[] {
  const byCode = new Map(assessmentRows.map((row) => [row.ac_code, row]));
  return acRows.map((row) => ({
    ...row,
    assessment: byCode.get(row.ac_code) ?? null,
  }));
}

export function submissionAcRows(submission: Submission): AcSnapshotRow[] {
  return parseAcSnapshot(submission.ac_snapshot);
}

export function submissionMaterials(submission: Submission): MaterialRow[] {
  return parseMaterialRows(submission.materials);
}
