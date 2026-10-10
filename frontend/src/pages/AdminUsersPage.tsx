import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { KeyRound, Plus } from "lucide-react";
import { toast } from "sonner";

import { createUser, listUsers, resetUserPassword, setUserStatus } from "@/api/admin";
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
import { useAuth } from "@/stores/auth";

export function AdminUsersPage() {
  const queryClient = useQueryClient();
  const me = useAuth((s) => s.user);
  const [createOpen, setCreateOpen] = useState(false);
  const [username, setUsername] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<"admin" | "user">("user");
  const [resetFor, setResetFor] = useState<string | null>(null);
  const [newPassword, setNewPassword] = useState("");

  const { data: users = [] } = useQuery({
    queryKey: ["admin-users"],
    queryFn: listUsers,
  });

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: ["admin-users"] });

  const createMutation = useMutation({
    mutationFn: () =>
      createUser({ username, display_name: displayName, password, role }),
    onSuccess: () => {
      toast.success("用户已创建（首次登录需修改密码）");
      setCreateOpen(false);
      setUsername("");
      setDisplayName("");
      setPassword("");
      invalidate();
    },
    onError: (err) => toast.error(err instanceof Error ? err.message : "创建失败"),
  });

  const statusMutation = useMutation({
    mutationFn: ({ id, status }: { id: string; status: "active" | "disabled" }) =>
      setUserStatus(id, status),
    onSuccess: invalidate,
    onError: (err) => toast.error(err instanceof Error ? err.message : "状态更新失败"),
  });

  const resetMutation = useMutation({
    mutationFn: ({ id, password: pw }: { id: string; password: string }) =>
      resetUserPassword(id, pw),
    onSuccess: () => {
      toast.success("密码已重置（该用户下次登录必须改密）");
      setResetFor(null);
      setNewPassword("");
    },
    onError: (err) => toast.error(err instanceof Error ? err.message : "重置失败"),
  });

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-semibold">用户管理</h1>
        <Button size="sm" onClick={() => setCreateOpen(true)}>
          <Plus className="h-4 w-4" />
          新建用户
        </Button>
      </div>

      <table className="w-full text-sm">
        <thead>
          <tr className="border-b text-left text-xs text-muted-foreground">
            <th className="py-2">用户名</th>
            <th className="py-2">显示名</th>
            <th className="py-2">角色</th>
            <th className="py-2">状态</th>
            <th className="py-2">改密</th>
            <th className="py-2 text-right">操作</th>
          </tr>
        </thead>
        <tbody>
          {users.map((u) => (
            <tr key={u.id} className="border-b last:border-0">
              <td className="py-2">{u.username}</td>
              <td>{u.display_name || "-"}</td>
              <td>
                <Badge variant={u.role === "admin" ? "default" : "secondary"}>
                  {u.role}
                </Badge>
              </td>
              <td>
                <Badge variant={u.status === "active" ? "outline" : "destructive"}>
                  {u.status === "active" ? "正常" : "已禁用"}
                </Badge>
              </td>
              <td className="text-xs text-muted-foreground">
                {u.must_change_password ? "待改密" : "-"}
              </td>
              <td className="text-right">
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => setResetFor(u.id)}
                  title="重置密码"
                >
                  <KeyRound className="h-4 w-4" />
                </Button>
                {u.id !== me?.id && (
                  <Button
                    variant="ghost"
                    size="sm"
                    className={u.status === "active" ? "text-destructive" : ""}
                    onClick={() =>
                      statusMutation.mutate({
                        id: u.id,
                        status: u.status === "active" ? "disabled" : "active",
                      })
                    }
                  >
                    {u.status === "active" ? "禁用" : "启用"}
                  </Button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <Dialog open={createOpen} onOpenChange={setCreateOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>新建用户</DialogTitle>
          </DialogHeader>
          <div className="space-y-3">
            <div className="space-y-1">
              <Label>用户名</Label>
              <Input value={username} onChange={(e) => setUsername(e.target.value)} />
            </div>
            <div className="space-y-1">
              <Label>显示名</Label>
              <Input
                value={displayName}
                onChange={(e) => setDisplayName(e.target.value)}
              />
            </div>
            <div className="space-y-1">
              <Label>初始密码</Label>
              <Input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </div>
            <div className="space-y-1">
              <Label>角色</Label>
              <Select value={role} onValueChange={(v) => setRole(v as "admin" | "user")}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="user">user</SelectItem>
                  <SelectItem value="admin">admin</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <Button
              className="w-full"
              onClick={() => createMutation.mutate()}
              disabled={createMutation.isPending || !username.trim() || !password}
            >
              创建
            </Button>
          </div>
        </DialogContent>
      </Dialog>

      <Dialog open={resetFor !== null} onOpenChange={(open) => !open && setResetFor(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>重置密码</DialogTitle>
          </DialogHeader>
          <div className="space-y-3">
            <Label>新密码</Label>
            <Input
              type="password"
              value={newPassword}
              onChange={(e) => setNewPassword(e.target.value)}
            />
            <Button
              className="w-full"
              disabled={!newPassword || resetMutation.isPending}
              onClick={() => resetFor && resetMutation.mutate({ id: resetFor, password: newPassword })}
            >
              重置
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
