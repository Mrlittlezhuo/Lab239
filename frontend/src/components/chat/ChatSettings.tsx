import { useQuery } from "@tanstack/react-query";
import { Settings2 } from "lucide-react";

import { getServerModels } from "@/api/server";
import { listTools } from "@/api/tools";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { useChatPrefs, type ToolMode } from "@/stores/chatPrefs";
import { useState } from "react";

const MODE_OPTIONS: { value: ToolMode; label: string; hint: string }[] = [
  { value: "auto", label: "智能路由", hint: "意图路由自动选择回答方式" },
  { value: "plain", label: "直接回答", hint: "仅模型，不调用工具" },
  { value: "manual", label: "指定工具", hint: "手动勾选可用工具" },
  { value: "agent_run", label: "Agent 任务", hint: "单 Agent 多步任务闭环" },
];

export function ChatSettings() {
  const [open, setOpen] = useState(false);
  const prefs = useChatPrefs();
  const { data: models } = useQuery({
    queryKey: ["server-models"],
    queryFn: getServerModels,
  });
  const { data: tools } = useQuery({
    queryKey: ["tools"],
    queryFn: listTools,
    enabled: open,
  });

  const modelValue = prefs.model || models?.default_llm || "";
  const modeMeta = MODE_OPTIONS.find((m) => m.value === prefs.toolMode)!;

  return (
    <>
      <div className="flex items-center gap-2">
        <Badge variant="secondary">{modeMeta.label}</Badge>
        {prefs.model && <Badge variant="outline">{prefs.model}</Badge>}
        <Button variant="outline" size="sm" onClick={() => setOpen(true)}>
          <Settings2 className="h-4 w-4" />
          设置
        </Button>
      </div>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="max-h-[85vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>聊天设置</DialogTitle>
          </DialogHeader>

          <div className="space-y-2">
            <Label>模型</Label>
            <Select value={modelValue} onValueChange={prefs.setModel}>
              <SelectTrigger>
                <SelectValue placeholder="使用服务端默认模型" />
              </SelectTrigger>
              <SelectContent>
                {(models?.llm_models ?? []).map((m) => (
                  <SelectItem key={m} value={m}>
                    {m}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <div className="space-y-2">
            <Label>视觉模型（解析图片附件时使用）</Label>
            {(models?.image2text_models?.length ?? 0) > 0 ? (
              <>
                <Select
                  value={prefs.visionModel}
                  onValueChange={prefs.setVisionModel}
                >
                  <SelectTrigger>
                    <SelectValue placeholder="未选择（附件图片将无法解析）" />
                  </SelectTrigger>
                  <SelectContent>
                    {(models?.image2text_models ?? []).map((m) => (
                      <SelectItem key={m} value={m}>
                        {m}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <p className="text-xs text-muted-foreground">
                  图片解析走 /v1 直通接口：该轮不调用工具/Agent，也不写入会话历史。
                </p>
              </>
            ) : (
              <p className="text-xs text-muted-foreground">
                服务端没有配置 image2text 模型，无法解析图片附件。
              </p>
            )}
          </div>

          <div className="space-y-2">
            <Label>温度（留空用服务端默认）</Label>
            <Input
              type="number"
              min={0}
              max={2}
              step={0.1}
              value={prefs.temperature ?? ""}
              placeholder={String(models?.temperature ?? 0.7)}
              onChange={(e) =>
                prefs.setTemperature(
                  e.target.value === "" ? null : Number(e.target.value),
                )
              }
            />
          </div>

          <div className="space-y-2">
            <Label>工具模式</Label>
            <div className="grid grid-cols-2 gap-2">
              {MODE_OPTIONS.map((opt) => (
                <button
                  key={opt.value}
                  type="button"
                  className={`rounded-md border p-2 text-left text-sm ${
                    prefs.toolMode === opt.value
                      ? "border-primary bg-accent"
                      : "hover:bg-accent/50"
                  }`}
                  onClick={() => prefs.setToolMode(opt.value)}
                >
                  <div className="font-medium">{opt.label}</div>
                  <div className="text-xs text-muted-foreground">{opt.hint}</div>
                </button>
              ))}
            </div>
          </div>

          {(prefs.toolMode === "manual" || prefs.toolMode === "agent_run") && (
            <div className="space-y-2">
              <div className="flex items-center justify-between">
                <Label>可用 MCP 工具</Label>
                <Switch
                  checked={prefs.useMcp}
                  onCheckedChange={prefs.setUseMcp}
                />
              </div>
            </div>
          )}

          {prefs.toolMode === "manual" && (
            <div className="space-y-2">
              <Label>选择工具</Label>
              <div className="max-h-52 space-y-1 overflow-y-auto rounded-md border p-2">
                {Object.values(tools ?? {}).map((tool) => (
                  <label
                    key={tool.name}
                    className="flex cursor-pointer items-center gap-2 text-sm"
                  >
                    <Checkbox
                      checked={prefs.selectedTools.includes(tool.name)}
                      onCheckedChange={(checked) => {
                        const next = checked
                          ? [...prefs.selectedTools, tool.name]
                          : prefs.selectedTools.filter(
                              (t) => t !== tool.name,
                            );
                        prefs.setSelectedTools(next);
                      }}
                    />
                    <span>{tool.name}</span>
                    {tool.description && (
                      <span className="truncate text-xs text-muted-foreground">
                        {tool.description}
                      </span>
                    )}
                  </label>
                ))}
                {tools && Object.keys(tools).length === 0 && (
                  <div className="text-sm text-muted-foreground">
                    服务端没有可用工具
                  </div>
                )}
              </div>
            </div>
          )}
        </DialogContent>
      </Dialog>
    </>
  );
}
