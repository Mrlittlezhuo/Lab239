import { useId } from "react";

import type { ResearchBrief } from "@/api/supervision";
import { AlertPanel } from "@/components/supervision/AlertPanel";
import {
  BRIEF_LIST_FIELDS,
  BRIEF_TEXT_FIELDS,
  contentFromBrief,
  fieldLabel,
  isContentEmpty,
  listToText,
  textToList,
  type BriefContentDraft,
} from "@/components/supervision/research/briefModel";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";

/**
 * 研究定义的 14 个内容字段（§4.3 卡片字段）：12 个文本卡片 + 2 个列表。
 * 只读视图与编辑器共用同一份字段清单与标签，避免两处各排一套顺序。
 */

export function BriefContentView({ brief }: { brief: ResearchBrief }) {
  if (isContentEmpty(contentFromBrief(brief))) {
    return (
      <AlertPanel
        title="这一版还没有填写结构化内容"
        description="创建者还没有把讨论整理成结构化字段；可以先看讨论记录，或等对方补充。"
      />
    );
  }
  return (
    <dl className="space-y-2">
      {BRIEF_TEXT_FIELDS.filter((field) => (brief[field] ?? "").trim()).map((field) => (
        <div key={field} className="grid gap-1 border-b pb-2 last:border-b-0 md:grid-cols-[13rem_1fr]">
          <dt className="text-xs font-medium text-muted-foreground">{fieldLabel(field)}</dt>
          <dd className="whitespace-pre-wrap text-sm">{brief[field]}</dd>
        </div>
      ))}
      {BRIEF_LIST_FIELDS.filter((field) => brief[field].length > 0).map((field) => (
        <div key={field} className="grid gap-1 border-b pb-2 last:border-b-0 md:grid-cols-[13rem_1fr]">
          <dt className="text-xs font-medium text-muted-foreground">{fieldLabel(field)}</dt>
          <dd className="flex flex-wrap gap-1">
            {brief[field].map((item) => (
              <span key={item} className="rounded bg-muted px-2 py-0.5 text-xs">
                {item}
              </span>
            ))}
          </dd>
        </div>
      ))}
    </dl>
  );
}

/**
 * 结构化内容编辑器。列表字段用「一行一条」的文本框，提交前拆成数组
 * （后端也接受字符串数组，并按顿号接成文本存库）。
 */
export function BriefContentEditor({
  content,
  onChange,
  disabled = false,
}: {
  content: BriefContentDraft;
  onChange: (next: BriefContentDraft) => void;
  disabled?: boolean;
}) {
  const base = useId();

  function setText(field: (typeof BRIEF_TEXT_FIELDS)[number], value: string) {
    onChange({ ...content, [field]: value });
  }

  function setList(field: (typeof BRIEF_LIST_FIELDS)[number], value: string) {
    onChange({ ...content, [field]: textToList(value) });
  }

  return (
    <div className="space-y-3">
      {BRIEF_TEXT_FIELDS.map((field) => (
        <div key={field} className="space-y-1">
          <Label htmlFor={`${base}-${field}`} className="text-xs text-muted-foreground">
            {fieldLabel(field)}
            {field === "question" ? "（确认共享前必填）" : ""}
          </Label>
          <Textarea
            id={`${base}-${field}`}
            rows={field === "question" ? 3 : 2}
            disabled={disabled}
            value={content[field]}
            onChange={(event) => setText(field, event.target.value)}
          />
        </div>
      ))}
      {BRIEF_LIST_FIELDS.map((field) => (
        <div key={field} className="space-y-1">
          <Label htmlFor={`${base}-${field}`} className="text-xs text-muted-foreground">
            {fieldLabel(field)}（一行一条）
          </Label>
          <Textarea
            id={`${base}-${field}`}
            rows={2}
            disabled={disabled}
            value={listToText(content[field])}
            onChange={(event) => setList(field, event.target.value)}
          />
        </div>
      ))}
    </div>
  );
}
