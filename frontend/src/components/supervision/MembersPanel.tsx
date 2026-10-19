import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import {
  activateProject,
  addProjectMember,
  decideArchive,
  deleteProject,
  removeProjectMember,
  requestArchive,
  searchUsers,
} from "@/api/supervision";
import type { ProjectDetail, ProjectRole, UserHint } from "@/api/supervision";
import { AlertPanel, errorText } from "@/components/supervision/AlertPanel";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

const ROLE_LABEL: Record<string, string> = {
  student: "学生",
  mentor: "导师",
};

/**
 * 成员与项目设置：绑成员、启动项目、申请/决定归档、删除草稿项目。
 *
 * 用户查找只返回 user_id / username / display_name 三个字段（后端刻意不给
 * 邮箱之类的信息），关键字至少两个字符。绑定与归档只有导师能操作，
 * 学生看到的是只读名单 + 自己发起归档申请。
 */
export function MembersPanel({
  project,
  refresh,
}: {
  project: ProjectDetail;
  refresh: () => void;
}) {
  const isMentor = project.my_role === "mentor";
  const queryClient = useQueryClient();
  const [keyword, setKeyword] = useState("");
  const [role, setRole] = useState<ProjectRole>("student");
  const [reason, setReason] = useState("");

  const users = useQuery({
    queryKey: ["supervision", "users", keyword],
    queryFn: () => searchUsers(keyword),
    enabled: keyword.trim().length >= 2,
  });

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["supervision", "project"] });
    queryClient.invalidateQueries({ queryKey: ["supervision", "projects"] });
    refresh();
  };

  const add = useMutation({
    mutationFn: (user: UserHint) =>
      addProjectMember(project.project_id, { user_id: user.user_id, role }),
    onSuccess: () => {
      toast.success("已添加成员");
      setKeyword("");
      invalidate();
    },
    onError: (error) => toast.error(errorText(error)),
  });

  const remove = useMutation({
    mutationFn: (userId: string) => removeProjectMember(project.project_id, userId),
    onSuccess: () => {
      toast.success("已移除成员（历史记录保留）");
      invalidate();
    },
    onError: (error) => toast.error(errorText(error)),
  });

  const activate = useMutation({
    mutationFn: () => activateProject(project.project_id),
    onSuccess: () => {
      toast.success("项目已启动");
      invalidate();
    },
    onError: (error) => toast.error(errorText(error)),
  });

  const archive = useMutation({
    mutationFn: () => requestArchive(project.project_id, reason),
    onSuccess: () => {
      toast.success("已发起归档申请，等待对方确认");
      setReason("");
      invalidate();
    },
    onError: (error) => toast.error(errorText(error)),
  });

  const decide = useMutation({
    mutationFn: (approve: boolean) =>
      decideArchive(project.project_id, { approve, reason }),
    onSuccess: () => {
      toast.success("归档决定已记录");
      setReason("");
      invalidate();
    },
    onError: (error) => toast.error(errorText(error)),
  });

  const destroy = useMutation({
    mutationFn: () => deleteProject(project.project_id, reason),
    onSuccess: () => {
      toast.success("草稿项目已删除");
      setReason("");
      invalidate();
    },
    onError: (error) => toast.error(errorText(error)),
  });

  const activeMembers = project.members.filter((member) => member.status === "active");
  const pendingArchive = Boolean(project.archive_requested_at);

  return (
    <div className="space-y-4">
      <div className="space-y-2">
        <div className="text-sm font-medium">在册成员</div>
        <ul className="space-y-2">
          {activeMembers.map((member) => (
            <li key={member.user_id} className="flex items-center gap-2 rounded-md border p-3">
              <div className="min-w-0 flex-1">
                <div className="truncate text-sm">{member.user_id}</div>
                <div className="text-xs text-muted-foreground">
                  加入于 {(member.joined_at ?? "").replace("T", " ").slice(0, 19)}
                </div>
              </div>
              <Badge variant="secondary">{ROLE_LABEL[member.role] ?? member.role}</Badge>
              {isMentor && (
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => remove.mutate(member.user_id)}
                  disabled={remove.isPending}
                >
                  移除
                </Button>
              )}
            </li>
          ))}
        </ul>
      </div>

      {isMentor && (
        <div className="space-y-2 rounded-md border p-3">
          <div className="text-sm font-medium">添加成员</div>
          <div className="flex flex-wrap gap-2">
            <Input
              className="max-w-64"
              value={keyword}
              onChange={(event) => setKeyword(event.target.value)}
              placeholder="按用户名或显示名搜索（至少两个字符）"
            />
            <Select value={role} onValueChange={(value) => setRole(value as ProjectRole)}>
              <SelectTrigger className="w-32">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="student">学生</SelectItem>
                <SelectItem value="mentor">导师</SelectItem>
              </SelectContent>
            </Select>
          </div>
          {keyword.trim().length >= 2 && users.isLoading && (
            <p className="text-xs text-muted-foreground">搜索中…</p>
          )}
          {users.isError && (
            <p className="text-xs text-destructive">{errorText(users.error)}</p>
          )}
          {!users.isLoading && keyword.trim().length >= 2 && (users.data?.length ?? 0) === 0 && (
            <p className="text-xs text-muted-foreground">没有匹配的启用用户。</p>
          )}
          <ul className="space-y-1">
            {(users.data ?? []).map((user) => (
              <li key={user.user_id} className="flex items-center gap-2 text-sm">
                <span className="min-w-0 flex-1 truncate">
                  {user.display_name || user.username}
                  <span className="ml-2 text-xs text-muted-foreground">@{user.username}</span>
                </span>
                <Button size="sm" onClick={() => add.mutate(user)} disabled={add.isPending}>
                  添加为{ROLE_LABEL[role]}
                </Button>
              </li>
            ))}
          </ul>
          <p className="text-xs text-muted-foreground">
            每个项目首期只允许一名在册学生和一名在册主导师；替换成员请先移除。
          </p>
        </div>
      )}

      <div className="space-y-2 rounded-md border p-3">
        <div className="text-sm font-medium">项目状态</div>
        <div className="text-sm">
          当前状态：
          <Badge variant="secondary" className="ml-1">
            {{ draft: "草稿", active: "进行中", archived: "已归档" }[project.status] ?? project.status}
          </Badge>
        </div>
        {project.status === "draft" && isMentor && (
          <Button size="sm" onClick={() => activate.mutate()} disabled={activate.isPending}>
            启动项目
          </Button>
        )}
        {project.status === "active" && (
          <>
            <Input
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              placeholder="归档原因"
            />
            <div className="flex gap-2">
              <Button size="sm" variant="outline" onClick={() => archive.mutate()} disabled={archive.isPending || !reason.trim()}>
                申请归档
              </Button>
              {pendingArchive && isMentor && (
                <>
                  <Button size="sm" onClick={() => decide.mutate(true)} disabled={decide.isPending}>
                    同意归档
                  </Button>
                  <Button size="sm" variant="ghost" onClick={() => decide.mutate(false)} disabled={decide.isPending}>
                    拒绝归档
                  </Button>
                </>
              )}
              {pendingArchive && !isMentor && (
                <span className="text-xs text-muted-foreground">已发起归档申请，等待导师确认。</span>
              )}
            </div>
          </>
        )}
        {project.status === "draft" && (
          <>
            <Input
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              placeholder="删除原因（只有草稿项目可删除）"
            />
            <Button
              size="sm"
              variant="destructive"
              onClick={() => destroy.mutate()}
              disabled={destroy.isPending || !reason.trim()}
            >
              删除草稿项目
            </Button>
          </>
        )}
        <AlertPanel
          title="注意"
          description="归档与删除都不会清除历史任务、提交与审核记录；删除只对从未下发任务的草稿项目开放。"
          tone="warning"
        />
      </div>
    </div>
  );
}
