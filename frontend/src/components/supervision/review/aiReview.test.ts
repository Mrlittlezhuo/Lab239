import { describe, expect, it } from "vitest";

import type {
  AiMentorDiff,
  AiReviewFinding,
  AiReviewRun,
  AiStaleness,
  AiSuggestion,
  TaskAc,
} from "@/api/supervision";
import { ApiError } from "@/lib/errors";
import {
  buildDraftBody,
  canAdoptRow,
  describeUnknown,
  draftHasContent,
  emptySelfCheckDraft,
  findingMissingMaterialLines,
  findingNotes,
  isModelUnavailable,
  isRunCurrentForSubmission,
  mergeAiMentorRows,
  MODEL_UNAVAILABLE_TEXT,
  queueNote,
  runActions,
} from "@/components/supervision/review/aiReview";

function run(overrides: Partial<AiReviewRun> = {}): AiReviewRun {
  return {
    run_id: "r1",
    code: "AR-r1",
    display_code: "AR-r1-rev1",
    revision: 1,
    project_id: "p1",
    task_id: "t1",
    task_code: "T-001",
    submission_id: null,
    submission_code: "",
    submission_version: 0,
    submission_display: "",
    trigger_source: "student_manual",
    triggered_by: "u1",
    triggered_role: "student",
    status: "queued",
    summary_state: "",
    summary_note: "",
    ac_version: 1,
    total_batches: 0,
    completed_batches: 0,
    unchecked_acs: [],
    partial_results: false,
    call_plan: [],
    model_calls: [],
    input_budget: {},
    material_manifest: [],
    unread_materials: [],
    partially_read_materials: [],
    security_flags: [],
    finding_counts: {},
    visible_to_mentor: false,
    attached_to_submission: false,
    check_snapshot: {},
    draft_fingerprint: "fp",
    model_name: "",
    rules_version: "rules-1",
    parser_version: "parser-1",
    whitelist_version: "wl-1",
    budget_config_version: "budget-1",
    stale: false,
    stale_reasons: [],
    stale_label: "",
    attach_allowed: false,
    queue_status: {},
    model_available: false,
    failure_reason: "",
    cancel_reason: "",
    cancelled_by: "",
    ...overrides,
  };
}

function finding(overrides: Partial<AiReviewFinding> = {}): AiReviewFinding {
  return {
    finding_id: "f1",
    run_id: "r1",
    run_code: "AR-r1",
    ac_code: "AC-1",
    ac_statement: "复现基线并在同一测试集上给出指标",
    ac_version: 1,
    required: true,
    check_type: "code",
    verdict: "partial",
    evidence_depth: "E2",
    support_relation: "partially_supports",
    reason: "有实现代码，没有运行产物",
    satisfied_parts: "已有数据加载与训练脚本",
    missing_parts: "缺少评测结果文件",
    evidence_refs: [{ name: "train.py", location: "L20-L60" }],
    missing_materials: ["eval_results.json"],
    risks: [{ message: "指标口径未说明" }],
    mentor_focus: "确认评测脚本使用的测试集",
    injection_suspected: false,
    injection_note: "",
    runtime_claim_flag: false,
    downgrade_reason: "",
    validated: true,
    batch_index: 0,
    ...overrides,
  };
}

function staleness(overrides: Partial<AiStaleness> = {}): AiStaleness {
  return {
    run_code: "AR-r1",
    stale: false,
    reasons: [],
    based_on: "学生草稿快照",
    snapshot_fingerprint: "fp",
    attach_allowed: true,
    rerun_allowed: true,
    label: "当前草稿",
    ...overrides,
  };
}

describe("503 的语义", () => {
  it("503 是模型未接入，不是失败", () => {
    expect(isModelUnavailable(new ApiError(503, "模型能力尚未接入"))).toBe(true);
    expect(isModelUnavailable(new ApiError(500, "炸了"))).toBe(false);
    expect(isModelUnavailable(new Error("网络断了"))).toBe(false);
    expect(isModelUnavailable(undefined)).toBe(false);
    expect(MODEL_UNAVAILABLE_TEXT).toContain("导师审核不受影响");
  });
});

describe("运行上的可用动作", () => {
  it("排队中：可执行、可取消，不能附带或采纳", () => {
    const actions = runActions(run({ status: "queued" }));
    expect(actions).toEqual({
      execute: true,
      cancel: true,
      retry: false,
      attach: false,
      adopt: false,
    });
  });

  it("失败或取消后只能重新运行，不能执行", () => {
    for (const status of ["failed", "cancelled"] as const) {
      const actions = runActions(run({ status }));
      expect(actions.execute).toBe(false);
      expect(actions.cancel).toBe(false);
      expect(actions.retry).toBe(true);
    }
  });

  it("已完成但已过时 → 提供重新运行；staleness 查询结果优先于 run 自带标记", () => {
    const completed = run({ status: "completed" });
    expect(runActions(completed).retry).toBe(false);
    expect(runActions(completed, staleness({ stale: true, attach_allowed: false })).retry).toBe(
      true,
    );
  });

  it("自检结果只有完成、未附带且未过时才能附到提交", () => {
    const done = run({ status: "completed", attach_allowed: true });
    expect(runActions(done).attach).toBe(true);
    expect(runActions(done, staleness({ stale: true, attach_allowed: false })).attach).toBe(false);
    expect(
      runActions({ ...done, attached_to_submission: true, submission_id: "s1" }).attach,
    ).toBe(false);
    expect(runActions({ ...done, status: "running" }).attach).toBe(false);
  });

  it("采纳建议只对绑定正式提交的已完成运行开放（自检不是审核依据）", () => {
    expect(runActions(run({ status: "completed" })).adopt).toBe(false);
    expect(
      runActions(
        run({
          status: "completed",
          trigger_source: "mentor_manual",
          submission_id: "s1",
          submission_code: "S-001",
          submission_version: 1,
        }),
      ).adopt,
    ).toBe(true);
  });
});

describe("排队可见性", () => {
  it("文案来自后端 queue_status，不承诺精确时间", () => {
    expect(
      queueNote(
        run({
          queue_status: {
            status: "queued",
            queued_ahead: 2,
            has_queued_ahead: true,
            note: "排队中；可以离开页面，结果完成后仍会保存",
          },
        }),
      ),
    ).toBe("排队中；可以离开页面，结果完成后仍会保存；前方还有 2 个预审请求在排队");
    expect(queueNote(run({ status: "completed", queue_status: {} }))).toBe("");
  });
});

describe("旧结果不能当成当前结果（P-08）", () => {
  it("提交版本落后于最新提交时不是当前结果", () => {
    const bound = run({
      submission_id: "s1",
      submission_code: "S-001",
      submission_version: 1,
    });
    expect(isRunCurrentForSubmission(bound, 2)).toBe(false);
    expect(isRunCurrentForSubmission(bound, 1)).toBe(true);
    expect(isRunCurrentForSubmission(run({}), 3)).toBe(true);
  });
});

describe("逐条 AI 结论", () => {
  it("注入、运行断言、降级与未校验各自给出说明", () => {
    expect(
      findingNotes(
        finding({
          injection_suspected: true,
          injection_note: "train.py 里出现「忽略验收标准」",
          runtime_claim_flag: true,
          downgrade_reason: "只有静态代码，没有运行产物",
          validated: false,
        }),
      ),
    ).toEqual([
      "材料里出现了疑似指令文本，已按数据处理：train.py 里出现「忽略验收标准」",
      "材料里有「已运行/测试通过」类说法，但静态材料不能证明已运行",
      "结论已降级：只有静态代码，没有运行产物",
      "该条不是模型返回的可信结构化结论，只作线索，不作为依据",
    ]);
    expect(findingNotes(finding())).toEqual([]);
  });

  it("缺失材料既可能是字符串也可能是对象", () => {
    // 接口把 missing_materials 声明成 Record<string, unknown>[]，
    // 后端实际返回字符串数组，两种都要能显示。
    expect(
      findingMissingMaterialLines(
        finding({
          // 后端只会给字符串；这里刻意混进一个对象，测渲染 helper 的防御路径。
          missing_materials: ["eval.json", { name: "run.log" }] as unknown as string[],
        }),
      ),
    ).toEqual(["eval.json", "run.log"]);
  });

  it("describeUnknown 把后端 unknown 形状压成一行", () => {
    expect(describeUnknown("纯文本")).toBe("纯文本");
    expect(describeUnknown(null)).toBe("");
    expect(describeUnknown(7)).toBe("7");
    expect(describeUnknown([{ name: "a" }, "b"])).toBe("a、b");
    expect(describeUnknown({ name: "train.py", location: "L10" })).toBe("train.py");
    // 有可读标识字段时只取它；没有才退化成 key=value 列表。
    expect(describeUnknown({ ac_code: "AC-1", depth: 3 })).toBe("AC-1");
    expect(describeUnknown({ depth: 3, note: 5 })).toBe("depth=3 note=5");
    expect(describeUnknown({})).toBe("");
  });
});

describe("AI 建议与导师结论对照（P-22）", () => {
  const suggestions: AiSuggestion[] = [
    {
      ac_code: "AC-1",
      verdict: "partial",
      suggested_conclusion: "partially_met",
      suggested_label: "部分满足",
      reason: "缺运行产物",
      note: "",
    },
    {
      ac_code: "AC-2",
      verdict: "unverifiable",
      suggested_conclusion: "more_evidence_required",
      suggested_label: "要求补充材料",
      reason: "没有可读材料",
      note: "AI 的 unverifiable 是认识状态",
    },
  ];

  it("导师还没填结论时不判断一致性", () => {
    const rows = mergeAiMentorRows(suggestions, []);
    expect(rows.map((row) => row.ac_code)).toEqual(["AC-1", "AC-2"]);
    expect(rows[0].mentor_conclusion).toBe("");
    expect(rows[0].consistent).toBeNull();
  });

  it("按 ac_code 合成两侧并集，并保留一致性", () => {
    const diff: AiMentorDiff[] = [
      {
        ac_code: "AC-1",
        verdict: "partial",
        suggested_conclusion: "partially_met",
        mentor_conclusion: "met",
        ai_relation: "different",
        consistent: false,
      },
      {
        ac_code: "AC-9",
        verdict: "satisfied",
        suggested_conclusion: "met",
        mentor_conclusion: "met",
        ai_relation: "adopted",
        consistent: true,
      },
    ];
    const rows = mergeAiMentorRows(suggestions, diff);
    expect(rows.map((row) => row.ac_code)).toEqual(["AC-1", "AC-2", "AC-9"]);
    expect(rows[0]).toMatchObject({
      mentor_conclusion: "met",
      ai_relation: "different",
      consistent: false,
    });
    expect(rows[2].suggested_label).toBe("");
  });

  it("采纳按钮：未填或与 AI 不同才提示采纳，且必须绑定正式提交", () => {
    const rows = mergeAiMentorRows(suggestions, [
      {
        ac_code: "AC-1",
        verdict: "partial",
        suggested_conclusion: "partially_met",
        mentor_conclusion: "partially_met",
        ai_relation: "adopted",
        consistent: true,
      },
    ]);
    const adoptable = runActions(
      run({
        status: "completed",
        trigger_source: "mentor_manual",
        submission_id: "s1",
      }),
    );
    const selfCheck = runActions(run({ status: "completed" }));
    expect(canAdoptRow(rows[0], adoptable)).toBe(false);
    expect(canAdoptRow(rows[1], adoptable)).toBe(true);
    expect(canAdoptRow(rows[1], selfCheck)).toBe(false);
  });
});

describe("学生草稿 → AiDraftBody", () => {
  const acs: TaskAc[] = [
    {
      ac_code: "AC-1",
      statement: "复现基线",
      evidence_requirement: "结果文件",
      required: true,
      check_type: "code",
      version: 1,
    },
    {
      ac_code: "AC-2",
      statement: "写清指标口径",
      evidence_requirement: "文档章节",
      required: false,
      check_type: "doc",
      version: 1,
    },
  ];

  it("空草稿按任务 AC 建行", () => {
    const draft = emptySelfCheckDraft(acs);
    expect(Object.keys(draft.selfAssessment)).toEqual(["AC-1", "AC-2"]);
    expect(draftHasContent(draft)).toBe(false);
  });

  it("没选自评的行不进请求体", () => {
    const draft = emptySelfCheckDraft(acs);
    draft.summary = "跑通了训练脚本";
    draft.selfAssessment["AC-1"] = {
      ac_code: "AC-1",
      self_assessment: "partially_done",
      evidence: "train.py L20-L60",
      note: "",
    };
    const body = buildDraftBody(draft);
    expect(body.self_assessment).toEqual([
      {
        ac_code: "AC-1",
        self_assessment: "partially_done",
        evidence: "train.py L20-L60",
        note: "",
      },
    ]);
    expect(body.attachment_codes).toEqual([]);
    expect(draftHasContent(draft)).toBe(true);
  });

  it("核心材料必须是已勾选证据的子集", () => {
    const draft = emptySelfCheckDraft(acs);
    draft.attachmentCodes = ["A-001", "A-002"];
    draft.coreAttachmentCodes = ["A-002", "A-999"];
    expect(buildDraftBody(draft).core_attachment_codes).toEqual(["A-002"]);
  });
});
