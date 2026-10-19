import { cn } from "@/lib/utils";

/** 面板内统一的提示块：错误、空态与只读说明共用一种样式。 */
export function AlertPanel({
  title,
  description,
  tone = "info",
  className,
}: {
  title: string;
  description?: string;
  tone?: "info" | "warning" | "error";
  className?: string;
}) {
  return (
    <div
      className={cn(
        "rounded-md border p-3 text-sm",
        tone === "error" && "border-destructive/40 bg-destructive/5 text-destructive",
        tone === "warning" && "border-amber-400/50 bg-amber-500/5",
        tone === "info" && "border-dashed bg-muted/30 text-muted-foreground",
        className,
      )}
    >
      <div className="font-medium">{title}</div>
      {description && <div className="mt-1 whitespace-pre-wrap">{description}</div>}
    </div>
  );
}

export function errorText(error: unknown): string {
  return error instanceof Error && error.message ? error.message : "操作失败";
}
