import { useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { listAuditEvents } from "@/api/supervision";
import type { ProjectDetail } from "@/api/supervision";
import { AlertPanel, errorText } from "@/components/supervision/AlertPanel";
import { Input } from "@/components/ui/input";

function formatTime(value?: string | null): string {
  if (!value) return "";
  return value.replace("T", " ").slice(0, 19);
}

/**
 * 审计日志：项目内每个写动作都留一条 EV-NNN 事件。
 * 只显示服务端记录的事实，前端不补写、不合并事件。
 */
export function AuditPanel({ project }: { project: ProjectDetail }) {
  const [objectCode, setObjectCode] = useState("");

  const events = useQuery({
    queryKey: ["supervision", "audit", project.project_id, objectCode],
    queryFn: () =>
      listAuditEvents(project.project_id, objectCode ? { object_code: objectCode } : undefined),
  });

  return (
    <div className="space-y-3">
      <form
        className="flex items-center gap-2"
        onSubmit={(event) => {
          event.preventDefault();
          events.refetch();
        }}
      >
        <Input
          className="max-w-56"
          value={objectCode}
          onChange={(event) => setObjectCode(event.target.value.trim())}
          placeholder="按对象编号过滤，例如 T-1、S-001-v2"
        />
      </form>

      {events.isLoading && <p className="text-sm text-muted-foreground">加载中…</p>}
      {events.isError && (
        <AlertPanel tone="error" title="审计日志加载失败" description={errorText(events.error)} />
      )}
      {!events.isLoading && (events.data?.length ?? 0) === 0 && (
        <AlertPanel title="没有符合条件的审计事件" />
      )}

      <ul className="space-y-2">
        {(events.data ?? []).map((event) => (
          <li key={event.event_code} className="rounded-md border p-3 text-sm">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-medium">{event.action}</span>
              <span className="text-xs text-muted-foreground">
                {event.event_code} · {event.object_code || event.object_type} ·{" "}
                {formatTime(event.created_at)}
              </span>
            </div>
            <div className="mt-1 text-xs text-muted-foreground">
              操作人 {event.actor_id}（{event.actor_role}）
            </div>
            {event.detail && (
              <p className="mt-1 whitespace-pre-wrap">{event.detail}</p>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}
