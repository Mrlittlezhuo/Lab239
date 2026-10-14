import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import {
  getRecallSwitches,
  setRecallSwitches,
  type RecallSwitches,
} from "@/api/memory";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";

/**
 * 「我的记忆」页的召回开关（第三期设计方案 §4.1）。
 *
 * 一个总开关 + 4 个分项，默认只勾「个人偏好」与「已确认的历史决策」。
 * 关闭只停止召回，不删除任何记忆；总开关不约束个人偏好——个人偏好由第一期
 * 那条通道单独控制，关掉跨会话召回后偏好仍然生效（P3-11）。
 */
export function RecallSwitchesCard() {
  const queryClient = useQueryClient();
  const { data } = useQuery({
    queryKey: ["memory-recall-switches"],
    queryFn: getRecallSwitches,
  });

  const mutation = useMutation({
    mutationFn: (patch: Partial<RecallSwitches>) => setRecallSwitches(patch),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["memory-recall-switches"] }),
    onError: (err) =>
      toast.error(err instanceof Error ? err.message : "开关更新失败"),
  });

  const switches = data ?? {
    enabled: true,
    decisions: true,
    project: false,
    general: false,
    preferences: true,
  };

  function toggle(key: keyof RecallSwitches, value: boolean) {
    mutation.mutate({ [key]: value } as Partial<RecallSwitches>);
  }

  return (
    <section className="space-y-3 rounded-lg border p-4">
      <div>
        <h2 className="text-sm font-medium">跨会话相关记忆</h2>
        <p className="text-xs text-muted-foreground">
          决定新会话里是否参考以往会话中确认过的内容。关闭只会停止参考，
          不会删除任何记忆；每次参考都会在回答下方列出具体是哪几条。
        </p>
      </div>

      <SwitchRow
        label="跨会话相关记忆总开关"
        hint="关闭后不再参考任何历史会话结论；个人偏好仍按下面的“使用个人偏好”工作。"
        checked={switches.enabled}
        disabled={mutation.isPending}
        onChange={(v) => toggle("enabled", v)}
      />

      <div className="space-y-2 pl-4">
        <SwitchRow
          label="使用个人偏好"
          hint="回答语言、输出风格与长期背景，只影响回答方式。"
          checked={switches.preferences}
          disabled={mutation.isPending}
          onChange={(v) => toggle("preferences", v)}
        />
        <SwitchRow
          label="使用已确认的历史决策"
          hint="你在以往会话里确认过的决策与手动保存的记忆。"
          checked={switches.decisions}
          disabled={mutation.isPending}
          onChange={(v) => toggle("decisions", v)}
        />
        <SwitchRow
          label="使用项目内记忆"
          hint="默认关闭。只有选定该项目的会话、或在问题里明确提到该项目时，
            项目内的记忆才会被参考。"
          checked={switches.project}
          disabled={mutation.isPending}
          onChange={(v) => toggle("project", v)}
        />
        <SwitchRow
          label="使用一般历史会话结论"
          hint="默认关闭。这一桶是系统自动从以往会话提取、未经你确认的内容；
            开启后它们才可能被参考。"
          checked={switches.general}
          disabled={mutation.isPending}
          onChange={(v) => toggle("general", v)}
        />
      </div>
    </section>
  );
}

function SwitchRow({
  label,
  hint,
  checked,
  disabled,
  onChange,
}: {
  label: string;
  hint: string;
  checked: boolean;
  disabled?: boolean;
  onChange: (value: boolean) => void;
}) {
  return (
    <div className="flex items-start justify-between gap-4">
      <div className="min-w-0">
        <Label>{label}</Label>
        <p className="text-xs text-muted-foreground">{hint}</p>
      </div>
      <Switch
        aria-label={label}
        checked={checked}
        disabled={disabled}
        onCheckedChange={(v) => onChange(v === true)}
      />
    </div>
  );
}
