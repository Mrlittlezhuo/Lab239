import { useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { compareBriefVersions, listBriefVersions } from "@/api/supervision";
import type { ProjectDetail, ResearchBrief } from "@/api/supervision";
import { AlertPanel, errorText } from "@/components/supervision/AlertPanel";
import {
  briefRefOf,
  comparisonRows,
  entryTypeLabel,
  formatTime,
  isNotFound,
  sharedVersionNote,
  splitComparisonRows,
  statusLabel,
  versionChoices,
} from "@/components/supervision/research/briefModel";
import { Badge } from "@/components/ui/badge";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

/**
 * 版本历史与逐字段差异（§16.3 第4条）。
 *
 * 后端只对创建者开放版本历史与比较（另一方一律 404），并且已确认的版本
 * 不会被原地改写：改动生成新版本，`shared_version` 仍指向对外生效的旧版，
 * 所以界面必须提醒「另一方可能仍在读旧版本」。
 */
export function BriefVersionHistory({
  project,
  brief,
}: {
  project: ProjectDetail;
  brief: ResearchBrief;
}) {
  const ref = briefRefOf(brief);
  const [choice, setChoice] = useState<{ from?: number; to?: number }>({});

  const versions = useQuery({
    queryKey: ["supervision", "brief-versions", project.project_id, ref],
    queryFn: () => listBriefVersions(project.project_id, ref),
  });

  const rows = versions.data ?? [];
  const defaults = versionChoices(rows);
  const from = choice.from ?? defaults?.from;
  const to = choice.to ?? defaults?.to;

  const compare = useQuery({
    queryKey: ["supervision", "brief-compare", project.project_id, ref, from, to],
    queryFn: () =>
      compareBriefVersions(project.project_id, ref, from as number, to as number),
    enabled: from !== undefined && to !== undefined,
  });

  if (versions.isError) {
    return (
      <AlertPanel
        tone={isNotFound(versions.error) ? "info" : "error"}
        title={isNotFound(versions.error) ? "版本历史只对创建者开放" : "版本历史加载失败"}
        description={
          isNotFound(versions.error)
            ? "你读到的是对方已确认共享的那一版；对方的版本历史与草稿改动不共享。"
            : errorText(versions.error)
        }
      />
    );
  }

  const changes = compare.data ? comparisonRows(compare.data) : [];
  const grouped = splitComparisonRows(changes);
  const note = sharedVersionNote(brief);

  return (
    <div className="space-y-3 rounded-md border p-3">
      <div className="text-sm font-medium">版本历史与差异</div>

      {versions.isLoading && <p className="text-sm text-muted-foreground">读取版本历史…</p>}
      {!versions.isLoading && rows.length === 0 && (
        <AlertPanel title="还没有版本记录" description="确认共享或重新提取后才会出现版本。" />
      )}

      <ul className="space-y-1">
        {rows.map((row) => (
          <li key={row.version} className="flex flex-wrap items-center gap-2 text-sm">
            <span className="font-medium">v{row.version}</span>
            <Badge variant="secondary">{statusLabel(row.status)}</Badge>
            <span className="text-xs text-muted-foreground">{entryTypeLabel(row.entry_type)}</span>
            {row.edited_fields.length > 0 && (
              <span className="text-xs text-muted-foreground">
                改动：{row.edited_fields.join("、")}
              </span>
            )}
            {row.confirmed_at && (
              <span className="text-xs text-muted-foreground">
                确认于 {formatTime(row.confirmed_at)}
              </span>
            )}
            {row.superseded_at && (
              <span className="text-xs text-muted-foreground">
                被替代于 {formatTime(row.superseded_at)}
              </span>
            )}
          </li>
        ))}
      </ul>

      {rows.length >= 2 && (
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <span className="text-xs text-muted-foreground">比较</span>
          <Select
            value={String(from ?? "")}
            onValueChange={(value) => setChoice((current) => ({ ...current, from: Number(value) }))}
          >
            <SelectTrigger className="h-8 w-28">
              <SelectValue placeholder="起始版本" />
            </SelectTrigger>
            <SelectContent>
              {rows.map((row) => (
                <SelectItem key={`from-${row.version}`} value={String(row.version)}>
                  v{row.version}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <span className="text-xs text-muted-foreground">→</span>
          <Select
            value={String(to ?? "")}
            onValueChange={(value) => setChoice((current) => ({ ...current, to: Number(value) }))}
          >
            <SelectTrigger className="h-8 w-28">
              <SelectValue placeholder="目标版本" />
            </SelectTrigger>
            <SelectContent>
              {rows.map((row) => (
                <SelectItem key={`to-${row.version}`} value={String(row.version)}>
                  v{row.version}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      )}

      {compare.isError && (
        <AlertPanel tone="error" title="版本比较失败" description={errorText(compare.error)} />
      )}
      {compare.isLoading && from !== undefined && to !== undefined && (
        <p className="text-sm text-muted-foreground">比较中…</p>
      )}

      {compare.data && (
        <div className="space-y-2">
          <div className="text-xs text-muted-foreground">
            v{compare.data.from_version}（{statusLabel(compare.data.from_status)}）→ v
            {compare.data.to_version}（{statusLabel(compare.data.to_status)}）
          </div>
          {changes.length === 0 && (
            <AlertPanel title="这两个版本的结构化内容没有差异" />
          )}

          {grouped.textRows.map((row) => (
            <div key={row.field} className="rounded-md border p-2 text-sm">
              <div className="text-xs font-medium text-muted-foreground">{row.label}</div>
              <div className="mt-1 grid gap-2 md:grid-cols-2">
                <div className="whitespace-pre-wrap break-words text-muted-foreground line-through decoration-muted-foreground/40">
                  {row.fromText || "（空）"}
                </div>
                <div className="whitespace-pre-wrap break-words">{row.toText || "（空）"}</div>
              </div>
            </div>
          ))}

          {grouped.listRows.map((row) => (
            <div key={row.field} className="rounded-md border p-2 text-sm">
              <div className="text-xs font-medium text-muted-foreground">{row.label}</div>
              <div className="mt-1 flex flex-wrap gap-1">
                {row.removed.map((item) => (
                  <span key={`rm-${item}`} className="rounded bg-destructive/10 px-2 py-0.5 text-xs text-destructive line-through">
                    {item}
                  </span>
                ))}
                {row.added.map((item) => (
                  <span key={`add-${item}`} className="rounded bg-emerald-500/15 px-2 py-0.5 text-xs">
                    + {item}
                  </span>
                ))}
                {row.added.length === 0 && row.removed.length === 0 && (
                  <span className="text-xs text-muted-foreground">
                    {row.fromText} → {row.toText}
                  </span>
                )}
              </div>
            </div>
          ))}
        </div>
      )}

      <AlertPanel
        tone="warning"
        title="另一方可能仍在读旧版本"
        description={
          note ??
          "版本差异只说明你这一侧的改动；已共享给对方的那一版不会因为新草稿而改变，" +
          "引用旧版本的检索任务与提交也不会追溯改写。"
        }
      />
    </div>
  );
}
