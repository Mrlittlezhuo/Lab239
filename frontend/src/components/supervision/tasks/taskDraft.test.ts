/**
 * 任务草稿纯逻辑的单元测试（离线，不起 React）。
 *
 * 钉住的是后端会直接 400 的几条规则：至少一条 AC、AC 语句不能过于笼统、
 * 同一版本内 ac_code 唯一，以及"改了验收标准才会升版本"。
 */
import { describe, expect, it } from "vitest";

import type { Task } from "@/api/supervision";
import {
  addAcRow,
  addMaterialRow,
  acRowsChanged,
  buildTaskInput,
  draftFromTask,
  dueLabel,
  emptyTaskDraft,
  filterTasks,
  nextAcCode,
  removeAcRow,
  removeMaterialRow,
  updateAcRow,
  updateMaterialRow,
  validateTaskDraft,
} from "@/components/supervision/tasks/taskDraft";

function task(overrides: Partial<Task> = {}): Task {
  return {
    task_id: "t1",
    code: "T-001",
    title: "复现基线方法",
    purpose: "先有一个可比较的基线",
    description: "在公开数据集上跑通基线",
    status: "active",
    label: "待导师审核",
    priority: "normal",
    version: 2,
    due_at: "2026-10-20T18:30:00",
    requires_code_review: true,
    requires_runtime: false,
    required_materials: [{ key: "code", label: "代码归档", required: true }],
    milestone_id: "m1",
    acs: [
      {
        ac_code: "AC-1",
        statement: "固定随机种子下中位定位误差小于 1 m",
        evidence_requirement: "实验配置与结果文件",
        required: true,
        check_type: "experiment",
        version: 2,
      },
    ],
    published_at: "2026-10-10T09:00:00",
    cancel_reason: "",
    create_time: "2026-10-09T09:00:00",
    ...overrides,
  };
}

describe("AC 行操作", () => {
  it("新建草稿自带一条 AC-1，新增行按序号往后排", () => {
    const draft = emptyTaskDraft();
    expect(draft.acs).toHaveLength(1);
    expect(draft.acs[0].ac_code).toBe("AC-1");

    const withTwo = addAcRow(draft);
    expect(withTwo.acs.map((row) => row.ac_code)).toEqual(["AC-1", "AC-2"]);
    expect(withTwo.acs[1].key).not.toBe(withTwo.acs[0].key);
    // 原草稿不能被改动（表单靠不可变更新驱动重渲染）
    expect(draft.acs).toHaveLength(1);
  });

  it("nextAcCode 跳过已用编号，不会撞后端唯一约束", () => {
    expect(nextAcCode([{ ac_code: "AC-1" }, { ac_code: "AC-3" }])).toBe("AC-4");
    expect(nextAcCode([{ ac_code: "" }])).toBe("AC-1");
  });

  it("删除中间一行不影响其余行的标识", () => {
    let draft = addAcRow(emptyTaskDraft());
    draft = addAcRow(draft);
    const middle = draft.acs[1].key;
    const removed = removeAcRow(draft, middle);
    expect(removed.acs.map((row) => row.ac_code)).toEqual(["AC-1", "AC-3"]);
    expect(removed.acs[1].key).toBe(draft.acs[2].key);
  });

  it("updateAcRow 只改指定行", () => {
    const draft = addAcRow(emptyTaskDraft());
    const updated = updateAcRow(draft, draft.acs[0].key, {
      statement: "定位误差小于 1 m",
      required: false,
      check_type: "code",
    });
    expect(updated.acs[0].statement).toBe("定位误差小于 1 m");
    expect(updated.acs[0].required).toBe(false);
    expect(updated.acs[1].statement).toBe("");
  });

  it("材料行可增删改：行标识稳定，材料标识可改", () => {
    let draft = addMaterialRow(emptyTaskDraft());
    draft = addMaterialRow(draft);
    expect(draft.materials.map((row) => row.rowId)).toEqual(["mat-1", "mat-2"]);
    expect(draft.materials.map((row) => row.key)).toEqual(["M-1", "M-2"]);
    draft = updateMaterialRow(draft, "mat-1", { key: "code", label: "代码归档" });
    expect(draft.materials[0]).toEqual({
      rowId: "mat-1",
      key: "code",
      label: "代码归档",
      required: true,
    });
    // 改了材料标识，行标识不变，输入框不会因为 key 变化而重建
    expect(draft.materials[0].rowId).toBe("mat-1");
    expect(removeMaterialRow(draft, "mat-2").materials).toHaveLength(1);
  });
});

describe("draftFromTask", () => {
  it("把后端任务还原成草稿，保留 ac_code 与 datetime-local 取值", () => {
    const draft = draftFromTask(task());
    expect(draft.acs[0].ac_code).toBe("AC-1");
    expect(draft.due_at).toBe("2026-10-20T18:30");
    expect(draft.milestone_id).toBe("m1");
    expect(draft.materials).toEqual([
      { rowId: "mat-1", key: "code", label: "代码归档", required: true },
    ]);
  });

  it("没有验收标准的旧任务也给出一行空 AC，编辑界面不会空白", () => {
    const draft = draftFromTask(
      task({ acs: [], due_at: null, milestone_id: null, required_materials: [] }),
    );
    expect(draft.acs).toHaveLength(1);
    expect(draft.acs[0].ac_code).toBe("AC-1");
    expect(draft.due_at).toBe("");
    expect(draft.milestone_id).toBe("");
    expect(draft.materials).toHaveLength(0);
  });
});

describe("validateTaskDraft", () => {
  it("完整草稿通过校验", () => {
    expect(validateTaskDraft(draftFromTask(task()))).toEqual([]);
  });

  it("缺标题、缺目的都要拦下来", () => {
    const draft = draftFromTask(task());
    draft.title = "   ";
    draft.purpose = "";
    expect(validateTaskDraft(draft)).toEqual([
      "任务必须有标题。",
      "任务必须写明为什么要做（任务目的）。",
    ]);
  });

  it("“完成算法”这种笼统标准按后端口径拒绝", () => {
    const draft = emptyTaskDraft();
    draft.title = "跑实验";
    draft.purpose = "验证想法";
    draft.acs[0].statement = "完成算法";
    expect(validateTaskDraft(draft)[0]).toContain("过于笼统");
  });

  it("ac_code 重复会被拒绝（同一版本内后端有唯一约束）", () => {
    let draft = addAcRow(draftFromTask(task()));
    draft = updateAcRow(draft, draft.acs[1].key, {
      ac_code: "AC-1",
      statement: "与基线使用相同输入和随机种子",
    });
    expect(validateTaskDraft(draft)).toContain("验收标准编号重复：AC-1。");
  });

  it("材料标识为空或重复时给出定位到行的错误", () => {
    let draft = addMaterialRow(draftFromTask(task()));
    draft = updateMaterialRow(draft, "mat-2", { key: " " });
    expect(validateTaskDraft(draft)).toContain("第 2 行提交材料缺少材料标识。");
    draft = updateMaterialRow(draft, "mat-2", { key: "code" });
    expect(validateTaskDraft(draft)).toContain("提交材料标识重复：code。");
  });
});

describe("buildTaskInput", () => {
  it("去空白、不关联阶段目标时不传 milestone_id", () => {
    const draft = draftFromTask(task());
    draft.milestone_id = "";
    draft.title = "  复现基线方法  ";
    draft.acs[0].statement = "  固定随机种子下中位定位误差小于 1 m  ";
    const input = buildTaskInput(draft);
    expect(input.title).toBe("复现基线方法");
    expect(input.acs?.[0].statement).toBe("固定随机种子下中位定位误差小于 1 m");
    expect(input.milestone_id).toBeUndefined();
    expect(input.due_at).toBe("2026-10-20T18:30");
  });

  it("清空截止时间时显式传 null，让后端把 due_at 清掉", () => {
    const draft = draftFromTask(task());
    draft.due_at = "";
    expect(buildTaskInput(draft).due_at).toBeNull();
  });
});

describe("acRowsChanged", () => {
  it("原样保存不算改动，不触发升版本提示", () => {
    const current = task();
    expect(acRowsChanged(current, draftFromTask(current))).toBe(false);
  });

  it("改语句或删一行都会升版本，学生看到的是新版本", () => {
    const current = task();
    const draft = draftFromTask(current);
    draft.acs[0].statement = "固定随机种子下中位定位误差小于 0.5 m";
    expect(acRowsChanged(current, draft)).toBe(true);
    expect(acRowsChanged(current, removeAcRow(draft, draft.acs[0].key))).toBe(true);
  });

  it("只改标题或说明不会改动验收标准", () => {
    const current = task();
    const draft = draftFromTask(current);
    draft.title = "复现基线方法（补充说明）";
    draft.purpose = "先有一个可比较的基线，再谈改进";
    expect(acRowsChanged(current, draft)).toBe(false);
  });
});

describe("列表过滤与截止时间文案", () => {
  const rows = [
    task({ task_id: "a", status: "active" }),
    task({ task_id: "b", status: "draft" }),
    task({ task_id: "c", status: "cancelled" }),
  ];

  it("按状态过滤，全部原样返回", () => {
    expect(filterTasks(rows, "all")).toHaveLength(3);
    expect(filterTasks(rows, "active").map((row) => row.task_id)).toEqual(["a"]);
    expect(filterTasks(rows, "archived")).toEqual([]);
  });

  it("进行中的过期任务标“已逾期”，终态任务不标", () => {
    const now = new Date("2026-10-21T12:00:00");
    expect(dueLabel(task({ due_at: "2026-10-20T18:30:00" }), now)).toBe(
      "2026-10-20 18:30 · 已逾期",
    );
    expect(
      dueLabel(task({ due_at: "2026-10-20T18:30:00", status: "completed" }), now),
    ).toBe("2026-10-20 18:30");
    expect(dueLabel(task({ due_at: null }), now)).toBe("无截止时间");
  });
});
