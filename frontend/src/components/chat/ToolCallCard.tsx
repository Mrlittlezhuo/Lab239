import { useState } from "react";
import { ChevronDown, ChevronRight, Wrench } from "lucide-react";

import type { UiToolCall } from "@/hooks/useChatStream";

function formatValue(value: unknown): string {
  if (value === undefined || value === null) return "";
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

export function ToolCallCard({ call }: { call: UiToolCall }) {
  const [open, setOpen] = useState(false);
  const hasDetail =
    call.args !== undefined || call.output !== undefined || call.isError;
  return (
    <div className="my-1 rounded-md border bg-muted/40 text-xs">
      <button
        type="button"
        className="flex w-full items-center gap-2 px-2 py-1.5 text-left"
        onClick={() => hasDetail && setOpen((v) => !v)}
      >
        {hasDetail ? (
          open ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />
        ) : null}
        <Wrench className="h-3 w-3 text-amber-600" />
        <span className="font-medium">{call.name ?? "工具调用"}</span>
        <span className={call.isError ? "text-destructive" : "text-muted-foreground"}>
          {call.isError ? "失败" : call.done ? "完成" : "执行中…"}
        </span>
      </button>
      {open && (
        <div className="space-y-2 border-t px-2 py-2">
          {call.args !== undefined && (
            <div>
              <div className="mb-1 font-medium text-muted-foreground">输入</div>
              <pre className="whitespace-pre-wrap break-all">{formatValue(call.args)}</pre>
            </div>
          )}
          {call.output !== undefined && (
            <div>
              <div className="mb-1 font-medium text-muted-foreground">输出</div>
              <pre className="max-h-60 overflow-y-auto whitespace-pre-wrap break-all">
                {formatValue(call.output)}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
