import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { toast } from "sonner";

import { createProject, listProjects } from "@/api/supervision";
import type { SupervisionProject } from "@/api/supervision";
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
import { Textarea } from "@/components/ui/textarea";

function errorText(error: unknown): string {
  return error instanceof Error && error.message ? error.message : "操作失败";
}

const STATUS_LABEL: Record<string, string> = {
  draft: "草稿",
  active: "进行中",
  archived: "已归档",
};

const ROLE_LABEL: Record<string, string> = {
  student: "学生",
  mentor: "导师",
};

/** 师生协作项目列表：我参与的项目 + 新建项目。 */
export function SupervisionProjectsPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [createOpen, setCreateOpen] = useState(false);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");

  const projects = useQuery({
    queryKey: ["supervision", "projects"],
    queryFn: listProjects,
  });

  const create = useMutation({
    mutationFn: createProject,
    onSuccess: (project) => {
      toast.success(`已创建项目 ${project.code}`);
      setCreateOpen(false);
      setName("");
      setDescription("");
      queryClient.invalidateQueries({ queryKey: ["supervision", "projects"] });
      navigate(`/supervision/${project.project_id}`);
    },
    onError: (error) => toast.error(errorText(error)),
  });

  const rows = projects.data ?? [];

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold">科研协作</h1>
          <p className="text-sm text-muted-foreground">
            导师与学生围绕同一个项目下发任务、提交成果、逐条审核验收标准。
          </p>
        </div>
        <Button onClick={() => setCreateOpen(true)}>新建项目</Button>
      </div>

      {projects.isLoading && <p className="text-sm text-muted-foreground">加载中…</p>}
      {projects.isError && (
        <p className="text-sm text-destructive">{errorText(projects.error)}</p>
      )}
      {!projects.isLoading && rows.length === 0 && (
        <div className="rounded-md border border-dashed p-6 text-sm text-muted-foreground">
          还没有项目。新建一个项目后，导师可以在项目里绑定学生并下发第一个任务。
        </div>
      )}

      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
        {rows.map((project) => (
          <ProjectCard
            key={project.project_id}
            project={project}
            onOpen={() => navigate(`/supervision/${project.project_id}`)}
          />
        ))}
      </div>

      <Dialog open={createOpen} onOpenChange={setCreateOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>新建科研协作项目</DialogTitle>
          </DialogHeader>
          <form
            className="space-y-3"
            onSubmit={(event) => {
              event.preventDefault();
              create.mutate({ name: name.trim(), description: description.trim() });
            }}
          >
            <div className="space-y-1">
              <Label htmlFor="project-name">项目名称</Label>
              <Input
                id="project-name"
                value={name}
                onChange={(event) => setName(event.target.value)}
                placeholder="例如：多模态检索的评测与改进"
                required
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="project-description">项目说明（可留空）</Label>
              <Textarea
                id="project-description"
                value={description}
                onChange={(event) => setDescription(event.target.value)}
                rows={4}
              />
            </div>
            <p className="text-xs text-muted-foreground">
              新项目是草稿：只有草稿项目可以删除；绑定学生与导师后才能启动。
            </p>
            <div className="flex justify-end gap-2">
              <Button type="button" variant="outline" onClick={() => setCreateOpen(false)}>
                取消
              </Button>
              <Button type="submit" disabled={create.isPending || !name.trim()}>
                创建并进入
              </Button>
            </div>
          </form>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function ProjectCard({
  project,
  onOpen,
}: {
  project: SupervisionProject;
  onOpen: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onOpen}
      className="rounded-md border bg-card p-4 text-left transition hover:bg-accent/40"
    >
      <div className="mb-1 flex items-center justify-between gap-2">
        <span className="font-medium">{project.name}</span>
        <Badge variant="secondary">{STATUS_LABEL[project.status] ?? project.status}</Badge>
      </div>
      <div className="mb-2 text-xs text-muted-foreground">
        {project.code}
        {project.my_role && ` · 我是${ROLE_LABEL[project.my_role] ?? project.my_role}`}
      </div>
      {project.description && (
        <p className="line-clamp-2 text-sm text-muted-foreground">{project.description}</p>
      )}
    </button>
  );
}
