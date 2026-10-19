import { describe, expect, it } from "vitest";

import type { Review, ReviewAcConclusion } from "@/api/supervision";
import {
  changedConclusions,
  concludedCount,
  conclusionPayload,
  emptyReviewForm,
  hasDraftContent,
  initialMatrix,
  isFormDirty,
  itemsToLines,
  linesToItems,
  matrixFromConclusions,
  reviewFormFromReview,
  reviewPayload,
  validatePublish,
} from "@/components/supervision/review/reviewDraft";
import type {
  AcSnapshotRow,
} from "@/components/supervision/review/snapshot";
import { parseAcSnapshot } from "@/components/supervision/review/snapshot";

function row(code: string, required = true): AcSnapshotRow {
  return {
    ac_code: code,
    statement: `${code} 的验收陈述`,
    evidence_requirement: "需要日志",
    required,
    check_type: "manual",
    version: 1,
  };
}

const ROWS: AcSnapshotRow[] = [row("AC-1"), row("AC-2"), row("AC-3", false)];

function conclusion(
  code: string,
  value: string,
  reason = "",
  aiRelation = "none",
): ReviewAcConclusion {
  return {
    ac_code: code,
    conclusion: value as ReviewAcConclusion["conclusion"],
    reason,
    ai_relation: aiRelation,
  };
}

function review(overrides: Partial<Review> = {}): Review {
  return {
    review_id: "r1",
    code: "MR-001",
    submission_id: "s1",
    task_id: "t1",
    status: "drafting",
    overall_decision: "",
    decision_reason: "",
    feedback: "",
    must_fix: [],
    suggestions: [],
    next_steps: "",
    reviewer_id: "u1",
    first_touched_at: null,
    published_at: null,
    conclusions: [],
    notes: [],
    ...overrides,
  };
}

describe("parseAcSnapshot", () => {
  it("缺字段或类型不符时给出可用默认值", () => {
    const rows = parseAcSnapshot([
      { ac_code: "AC-1", statement: 42, required: "true" },
      { statement: "没有 ac_code，直接丢掉" },
    ]);
    expect(rows).toHaveLength(1);
    expect(rows[0]).toEqual({
      ac_code: "AC-1",
      statement: "42",
      evidence_requirement: "",
      required: true,
      check_type: "",
      version: 0,
    });
  });
});

describe("逐条结论矩阵", () => {
  it("矩阵按提交快照建行，与快照顺序一致", () => {
    const matrix = initialMatrix(ROWS);
    expect(Object.keys(matrix)).toEqual(["AC-1", "AC-2", "AC-3"]);
    expect(matrix["AC-1"]).toEqual({
      ac_code: "AC-1",
      conclusion: "",
      reason: "",
      ai_relation: "none",
    });
  });

  it("已保存结论覆盖初始矩阵，快照里没有的 ac_code 被丢弃", () => {
    const matrix = matrixFromConclusions(ROWS, [
      conclusion("AC-2", "not_met", "缺少日志", "different"),
      conclusion("AC-9", "met", "不属于本次快照"),
    ]);
    expect(matrix["AC-2"].conclusion).toBe("not_met");
    expect(matrix["AC-2"].ai_relation).toBe("different");
    expect(matrix["AC-1"].conclusion).toBe("");
    expect(matrix["AC-9"]).toBeUndefined();
  });

  it("没选结论的行不进请求体（后端对空结论返回 400）", () => {
    const matrix = initialMatrix(ROWS);
    matrix["AC-1"].conclusion = "met";
    matrix["AC-1"].reason = "有结果文件";
    expect(conclusionPayload(matrix)).toEqual([
      { ac_code: "AC-1", conclusion: "met", reason: "有结果文件", ai_relation: "none" },
    ]);
    expect(concludedCount(matrix)).toBe(1);
  });
});

describe("草稿 diff", () => {
  it("只返回真正改动的行", () => {
    const saved = [conclusion("AC-1", "met", "理由 A"), conclusion("AC-2", "met", "理由 B")];
    const matrix = matrixFromConclusions(ROWS, saved);
    expect(changedConclusions(matrix, saved)).toEqual([]);

    matrix["AC-1"].reason = "理由改了";
    matrix["AC-3"].conclusion = "not_applicable";
    matrix["AC-3"].reason = "本任务不含该项";
    const changed = changedConclusions(matrix, saved);
    expect(changed.map((item) => item.ac_code)).toEqual(["AC-1", "AC-3"]);
  });

  it("ai_relation 变化也算改动（采纳 AI 建议后需要能保存）", () => {
    const saved = [conclusion("AC-1", "met", "理由 A", "none")];
    const matrix = matrixFromConclusions(ROWS, saved);
    matrix["AC-1"].ai_relation = "adopted";
    expect(changedConclusions(matrix, saved).map((item) => item.ac_code)).toEqual(["AC-1"]);
  });
});

describe("多行文本与列表", () => {
  it("拆行时去掉空行与首尾空白", () => {
    expect(linesToItems(" 补日志 \n\n  改配置  \n")).toEqual(["补日志", "改配置"]);
    expect(itemsToLines(["a", "b"])).toBe("a\nb");
  });

  it("reviewPayload 把 must_fix / suggestions 拆成数组", () => {
    const form = emptyReviewForm(ROWS);
    form.mustFix = "补运行日志\n补配置说明";
    form.suggestions = "顺手清理命名";
    form.nextSteps = "下周重跑";
    const payload = reviewPayload(form);
    expect(payload.must_fix).toEqual(["补运行日志", "补配置说明"]);
    expect(payload.suggestions).toEqual(["顺手清理命名"]);
    expect(payload.next_steps).toBe("下周重跑");
    expect(payload.conclusions).toEqual([]);
  });
});

describe("表单脏检查", () => {
  it("没有已保存草稿且什么都没填时不算脏", () => {
    expect(isFormDirty(emptyReviewForm(ROWS), null)).toBe(false);
    expect(hasDraftContent(emptyReviewForm(ROWS))).toBe(false);
  });

  it("已保存草稿回填后不算脏，改动后才算", () => {
    const saved = review({
      feedback: "总体可以",
      must_fix: ["补日志"],
      conclusions: [conclusion("AC-1", "met", "有结果文件")],
    });
    const form = reviewFormFromReview(saved, ROWS);
    expect(isFormDirty(form, saved)).toBe(false);
    expect(hasDraftContent(form)).toBe(true);

    form.mustFix = "补日志\n补配置";
    expect(isFormDirty(form, saved)).toBe(true);
  });
});

describe("发布前校验（镜像后端 publish_review）", () => {
  it("没选整体决定时阻塞", () => {
    const form = emptyReviewForm(ROWS);
    form.matrix["AC-1"].conclusion = "met";
    form.matrix["AC-1"].reason = "有结果文件";
    expect(validatePublish(form, ROWS, "", "")).toEqual(["请先选择整体决定。"]);
  });

  it("选了结论却没写理由时阻塞（含不适用）", () => {
    const form = emptyReviewForm(ROWS);
    form.matrix["AC-3"].conclusion = "not_applicable";
    expect(validatePublish(form, ROWS, "paused", "等材料")).toEqual([
      "以下验收标准已选结论但没写理由：AC-3。",
    ]);
  });

  it("确认通过要求所有必选 AC 都有结论，可选 AC 允许留空", () => {
    const form = emptyReviewForm(ROWS);
    form.matrix["AC-1"].conclusion = "met";
    form.matrix["AC-1"].reason = "有结果文件";
    const problems = validatePublish(form, ROWS, "approved", "");
    expect(problems).toEqual([
      "确认通过前，所有必选验收标准都要有导师结论；缺少：AC-2。",
    ]);

    form.matrix["AC-2"].conclusion = "partially_met";
    form.matrix["AC-2"].reason = "缺一部分日志";
    expect(validatePublish(form, ROWS, "approved", "")).toEqual([]);
  });

  it("要求修改必须至少一条必须修改项", () => {
    const form = emptyReviewForm(ROWS);
    expect(validatePublish(form, ROWS, "revision_required", "")).toEqual([
      "要求修改时必须至少填写一条必须修改项。",
    ]);
    form.mustFix = "补运行日志";
    expect(validatePublish(form, ROWS, "revision_required", "")).toEqual([]);
  });

  it("拒绝与暂缓必须填决定原因", () => {
    const form = emptyReviewForm(ROWS);
    expect(validatePublish(form, ROWS, "rejected", "  ")).toEqual([
      "拒绝或暂缓审核必须填写决定原因。",
    ]);
    expect(validatePublish(form, ROWS, "paused", "等材料齐了再审")).toEqual([]);
  });
});
