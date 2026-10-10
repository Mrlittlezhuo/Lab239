import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, Trash2 } from "lucide-react";
import { toast } from "sonner";

import {
  createMcpConnection,
  deleteMcpConnection,
  disableMcpConnection,
  enableMcpConnection,
  listMcpConnections,
} from "@/api/mcp";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
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

export function McpPage() {
  const queryClient = useQueryClient();
  const [createOpen, setCreateOpen] = useState(false);
  const [name, setName] = useState("");
  const [command, setCommand] = useState("");
  const [transport, setTransport] = useState("stdio");
  const [description, setDescription] = useState("");

  const { data: connections = [] } = useQuery({
    queryKey: ["mcp-connections"],
    queryFn: listMcpConnections,
  });

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: ["mcp-connections"] });

  const createMutation = useMutation({
    mutationFn: () =>
      createMcpConnection({
        server_name: name,
        args: command.trim() ? command.trim().split(/\s+/) : [],
        transport,
        description,
      }),
    onSuccess: (res) => {
      toast.success(`已创建 MCP 连接 ${res.server_name}`);
      setCreateOpen(false);
      setName("");
      setCommand("");
      setDescription("");
      invalidate();
    },
    onError: (err) => toast.error(err instanceof Error ? err.message : "创建失败"),
  });

  const toggleMutation = useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) =>
      enabled ? enableMcpConnection(id) : disableMcpConnection(id),
    onSuccess: (res) => {
      if (!res.success) toast.error(res.message);
      invalidate();
    },
    onError: (err) => toast.error(err instanceof Error ? err.message : "切换失败"),
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => deleteMcpConnection(id),
    onSuccess: (res) => {
      if (res.success) toast.success("已删除");
      else toast.error(res.message);
      invalidate();
    },
    onError: (err) => toast.error(err instanceof Error ? err.message : "删除失败"),
  });

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-semibold">MCP 服务</h1>
        <Button size="sm" onClick={() => setCreateOpen(true)}>
          <Plus className="h-4 w-4" />
          新建连接
        </Button>
      </div>

      <div className="space-y-2">
        {connections.map((c) => (
          <div key={c.id} className="rounded-lg border p-3">
            <div className="flex items-center gap-2">
              <span className="font-medium">{c.server_name}</span>
              <Badge variant="secondary">{c.transport}</Badge>
              <Badge variant={c.enabled ? "default" : "outline"}>
                {c.enabled ? "已启用" : "已禁用"}
              </Badge>
              <div className="ml-auto flex items-center gap-2">
                <Switch
                  checked={c.enabled}
                  onCheckedChange={(v) =>
                    toggleMutation.mutate({ id: c.id, enabled: v === true })
                  }
                />
                <button
                  type="button"
                  className="text-muted-foreground hover:text-destructive"
                  onClick={() => deleteMutation.mutate(c.id)}
                  title="删除"
                >
                  <Trash2 className="h-4 w-4" />
                </button>
              </div>
            </div>
            <div className="mt-1 text-xs text-muted-foreground">
              <code>{[c.server_name, ...c.args].join(" ")}</code>
              {c.description && <span className="ml-2">{c.description}</span>}
            </div>
          </div>
        ))}
        {connections.length === 0 && (
          <div className="py-10 text-center text-muted-foreground">
            暂无 MCP 连接
          </div>
        )}
      </div>

      <Dialog open={createOpen} onOpenChange={setCreateOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>新建 MCP 连接</DialogTitle>
          </DialogHeader>
          <div className="space-y-3">
            <div className="space-y-1">
              <Label>服务器名称</Label>
              <Input value={name} onChange={(e) => setName(e.target.value)} />
            </div>
            <div className="space-y-1">
              <Label>命令与参数（空格分隔）</Label>
              <Input
                value={command}
                onChange={(e) => setCommand(e.target.value)}
                placeholder="npx -y @modelcontextprotocol/server-filesystem /tmp"
              />
            </div>
            <div className="space-y-1">
              <Label>传输方式</Label>
              <Select value={transport} onValueChange={setTransport}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="stdio">stdio</SelectItem>
                  <SelectItem value="sse">sse</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1">
              <Label>描述</Label>
              <Input value={description} onChange={(e) => setDescription(e.target.value)} />
            </div>
            <Button
              className="w-full"
              onClick={() => createMutation.mutate()}
              disabled={createMutation.isPending || !name.trim()}
            >
              创建
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
