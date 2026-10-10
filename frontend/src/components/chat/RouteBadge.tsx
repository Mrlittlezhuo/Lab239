import { Badge } from "@/components/ui/badge";
import type { UiMessage } from "@/hooks/useChatStream";

const MODE_STYLES: Record<string, string> = {
  direct_answer: "bg-sky-100 text-sky-800 border-sky-200",
  knowledge_task: "bg-emerald-100 text-emerald-800 border-emerald-200",
  single_tool_task: "bg-amber-100 text-amber-800 border-amber-200",
  multi_step_agent: "bg-violet-100 text-violet-800 border-violet-200",
};

export function RouteBadge({ message }: { message: UiMessage }) {
  const route = message.route;
  if (!route) return null;
  const mode = route.executionMode ?? "";
  const label =
    route.executionModeLabel ??
    route.route.path_label ??
    mode ??
    "已路由";
  const confidence =
    typeof route.route.confidence === "number"
      ? ` ${(route.route.confidence * 100).toFixed(0)}%`
      : "";
  return (
    <div className="mb-2 flex flex-wrap items-center gap-2 text-xs">
      <Badge className={MODE_STYLES[mode] ?? ""}>{label}</Badge>
      {route.route.intent_label && (
        <span className="text-muted-foreground">
          意图：{route.route.intent_label}
          {confidence}
        </span>
      )}
      {route.route.fallback_reason && (
        <span className="text-amber-600">回退：{route.route.fallback_reason}</span>
      )}
    </div>
  );
}
