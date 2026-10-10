import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { toast } from "sonner";

import { changePassword } from "@/api/auth";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ApiError } from "@/lib/errors";
import { clearAuth, useAuth } from "@/stores/auth";

/**
 * 修改密码。服务端改密会 bump auth_version 使当前 token 失效，
 * 成功后必须重新登录。
 */
export function ChangePasswordPage() {
  const [oldPassword, setOldPassword] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const user = useAuth((s) => s.user);
  const mustChange = user?.must_change_password ?? false;
  const navigate = useNavigate();

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!oldPassword || !password) {
      toast.error("请输入旧密码和新密码");
      return;
    }
    if (password !== confirm) {
      toast.error("两次输入的新密码不一致");
      return;
    }
    setSubmitting(true);
    try {
      await changePassword(oldPassword, password);
      clearAuth();
      toast.success("密码已修改，请使用新密码重新登录");
      navigate("/login", { replace: true });
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "修改密码失败");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="flex h-screen items-center justify-center bg-muted/30">
      <Card className="w-96">
        <CardHeader>
          <CardTitle className="text-xl">修改密码</CardTitle>
          <CardDescription>
            {mustChange
              ? "首次登录必须修改初始密码后才能使用其他功能"
              : "修改密码后需要重新登录"}
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={handleSubmit} className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="old">旧密码</Label>
              <Input
                id="old"
                type="password"
                value={oldPassword}
                onChange={(e) => setOldPassword(e.target.value)}
                autoComplete="current-password"
                autoFocus
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="new">新密码</Label>
              <Input
                id="new"
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete="new-password"
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="confirm">确认新密码</Label>
              <Input
                id="confirm"
                type="password"
                value={confirm}
                onChange={(e) => setConfirm(e.target.value)}
                autoComplete="new-password"
              />
            </div>
            <Button type="submit" className="w-full" disabled={submitting}>
              {submitting ? "提交中…" : "修改密码"}
            </Button>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
