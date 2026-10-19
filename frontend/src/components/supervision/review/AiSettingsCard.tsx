import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import { getAiReviewSettings, setAiReviewSettings } from "@/api/supervision";
import type { ProjectDetail } from "@/api/supervision";
import { AlertPanel, errorText } from "@/components/supervision/AlertPanel";
import { Badge } from "@/components/ui/badge";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

const MODE_LABEL: Record<string, string> = {
  off: "不自动运行",
  on: "学生正式提交后自动运行",
  inherit: "继承项目设置",
};

/**
 * 自动预审开关（§6.2、P-05、P-09）：项目默认 + 任务级，任务明确设置优先。
 * 默认关闭；只有导师能改，学生只读。开关只影响后续提交，不追溯已发布任务的历史配置。
 */
export function AiSettingsCard({
  project,
  taskRef,
  scopeLabel,
}: {
  project: ProjectDetail;
  taskRef: string;
  scopeLabel: string;
}) {
  const queryClient = useQueryClient();
  const isMentor = project.my_role === "mentor";

  const settings = useQuery({
    queryKey: ["supervision", "ai-review", "settings", project.project_id, taskRef],
    queryFn: () => getAiReviewSettings(project.project_id, taskRef),
  });

  const save = useMutation({
    mutationFn: (mode: "off" | "on" | "inherit") =>
      setAiReviewSettings(project.project_id, { task_ref: taskRef, mode }),
    onSuccess: (saved) => {
      toast.success(`自动预审已设为：${MODE_LABEL[saved.effective_mode] ?? saved.effective_mode}`);
      queryClient.invalidateQueries({ queryKey: ["supervision", "ai-review", "settings"] });
    },
    onError: (error) => toast.error(errorText(error)),
  });

  if (settings.isLoading) return <p className="text-sm text-muted-foreground">设置加载中…</p>;
  if (settings.isError || !settings.data) {
    return (
      <AlertPanel tone="error" title="自动预审设置读不到" description={errorText(settings.error)} />
    );
  }

  const data = settings.data;
  const modes = taskRef === "" ? ["off", "on"] : ["inherit", "on", "off"];
  const current = taskRef === "" ? data.project_mode : data.task_mode;

  return (
    <div className="space-y-2 rounded-md border p-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm font-medium">提交后自动 AI 预审（{scopeLabel}）</span>
        <Badge variant={data.auto_review_enabled ? "secondary" : "outline"}>
          当前生效：{MODE_LABEL[data.effective_mode] ?? data.effective_mode}
        </Badge>
        {data.default_is_off && <Badge variant="outline">默认关闭</Badge>}
      </div>

      <div className="text-xs text-muted-foreground">
        项目默认：{MODE_LABEL[data.project_mode] ?? data.project_mode} · 本任务：
        {MODE_LABEL[data.task_mode] ?? data.task_mode}
      </div>
      <div className="text-xs text-muted-foreground">{data.priority_note}</div>

      {isMentor ? (
        <div className="flex flex-wrap items-center gap-2">
          <Select
            value={current}
            onValueChange={(mode) => save.mutate(mode as "off" | "on" | "inherit")}
            disabled={save.isPending}
          >
            <SelectTrigger className="h-9 w-[260px]">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {modes.map((mode) => (
                <SelectItem key={mode} value={mode}>
                  {MODE_LABEL[mode]}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <span className="text-xs text-muted-foreground">
            自动预审只在正式提交后创建低优先级排队任务，排队或失败都不会回滚提交。
          </span>
        </div>
      ) : (
        <AlertPanel
          title="学生看不到设置入口"
          description="自动预审开关由导师配置，这里只读显示当前生效值。"
        />
      )}
    </div>
  );
}
