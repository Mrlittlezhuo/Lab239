import { useQuery } from "@tanstack/react-query";

import { getBriefSourceMessages } from "@/api/supervision";
import type { ProjectDetail, ResearchBrief } from "@/api/supervision";
import { AlertPanel, errorText } from "@/components/supervision/AlertPanel";
import { briefRefOf, isNotFound, roleLabel } from "@/components/supervision/research/briefModel";
import { dialogueTurns } from "@/components/supervision/research/dialogueModel";
import { MarkdownBlock } from "@/components/supervision/research/MarkdownBlock";
import { Badge } from "@/components/ui/badge";

/**
 * 研究定义的来源消息（§16.3 第5条）。
 *
 * 后端按所有者过滤：另一方即使能读已共享的结构化版本，也反查不到来源
 * 原始消息，一律 404。这里照常调用，把 404 显示成说明而不是错误页。
 */
export function BriefSourceMessages({
  project,
  brief,
}: {
  project: ProjectDetail;
  brief: ResearchBrief;
}) {
  const ref = briefRefOf(brief);
  const query = useQuery({
    queryKey: ["supervision", "brief-source", project.project_id, ref],
    queryFn: () => getBriefSourceMessages(project.project_id, ref),
  });

  const turns = dialogueTurns(query.data?.messages ?? []);

  return (
    <div className="space-y-2 rounded-md border p-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm font-medium">来源讨论消息</span>
        {query.data?.code && <Badge variant="outline">{query.data.code}</Badge>}
        {brief.source_message_ids.length > 0 && (
          <span className="text-xs text-muted-foreground">
            记录了 {brief.source_message_ids.length} 条来源消息
          </span>
        )}
      </div>

      {query.isLoading && <p className="text-sm text-muted-foreground">读取来源消息…</p>}
      {query.isError && (
        <AlertPanel
          tone={isNotFound(query.error) ? "info" : "error"}
          title={isNotFound(query.error) ? "来源消息仅创建者可见" : "来源消息读取失败"}
          description={
            isNotFound(query.error)
              ? `这是${roleLabel(brief.owner_role)}创建的研究定义：确认共享的是结构化版本，来源原始聊天不随版本共享。`
              : errorText(query.error)
          }
        />
      )}
      {!query.isLoading && !query.isError && turns.length === 0 && (
        <AlertPanel title="没有可显示的来源消息" description="手动创建的研究定义不绑定来源会话。" />
      )}

      <ul className="space-y-2">
        {turns.map((turn) => (
          <li key={turn.key} className="rounded-md border p-2">
            <div className="mb-1 flex flex-wrap gap-2 text-xs text-muted-foreground">
              <span className="font-medium text-foreground">
                {turn.role === "member" ? `我（${roleLabel(project.my_role ?? "student")}）` : "AI 回复"}
              </span>
              {turn.time && <span>{turn.time}</span>}
            </div>
            {turn.pendingReply ? (
              <p className="text-sm text-muted-foreground">这条发言还没有 AI 回复。</p>
            ) : (
              <MarkdownBlock text={turn.text} />
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}
