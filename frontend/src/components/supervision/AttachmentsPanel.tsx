import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import {
  downloadAttachment,
  listAttachments,
  removeAttachment,
  uploadAttachment,
} from "@/api/supervision";
import type { Attachment, ProjectDetail } from "@/api/supervision";
import { AlertPanel, errorText } from "@/components/supervision/AlertPanel";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

const STATUS_LABEL: Record<string, string> = {
  active: "当前版本",
  replaced: "已被替换",
  removed: "已移除",
};

function formatSize(size: number): string {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}

/**
 * 项目附件：上传、下载、移除。附件按版本保留，移除只标记不删数据。
 * 上传走 multipart，下载必须带 Bearer（后端不接受裸链接）。
 */
export function AttachmentsPanel({ project }: { project: ProjectDetail }) {
  const fileRef = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [removing, setRemoving] = useState<Attachment | null>(null);
  const [reason, setReason] = useState("");
  const queryClient = useQueryClient();

  const attachments = useQuery({
    queryKey: ["supervision", "attachments", project.project_id],
    queryFn: () => listAttachments(project.project_id),
  });

  const upload = useMutation({
    mutationFn: () => {
      const form = new FormData();
      form.append("file", file as File);
      return uploadAttachment(project.project_id, form);
    },
    onSuccess: (attachment) => {
      toast.success(`已上传 ${attachment.name}（${attachment.code} v${attachment.version}）`);
      setFile(null);
      if (fileRef.current) fileRef.current.value = "";
      queryClient.invalidateQueries({ queryKey: ["supervision", "attachments"] });
    },
    onError: (error) => toast.error(errorText(error)),
  });

  const remove = useMutation({
    mutationFn: () => removeAttachment(project.project_id, removing?.code ?? "", reason),
    onSuccess: () => {
      toast.success("已标记移除，历史版本仍保留");
      setRemoving(null);
      setReason("");
      queryClient.invalidateQueries({ queryKey: ["supervision", "attachments"] });
    },
    onError: (error) => toast.error(errorText(error)),
  });

  async function handleDownload(attachment: Attachment) {
    try {
      const blob = await downloadAttachment(project.project_id, attachment.code);
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = attachment.name;
      link.click();
      URL.revokeObjectURL(url);
    } catch (error) {
      toast.error(errorText(error));
    }
  }

  const rows = (attachments.data ?? []).filter((item) => item.status === "active");

  return (
    <div className="space-y-4">
      <form
        className="flex flex-wrap items-center gap-2 rounded-md border p-3"
        onSubmit={(event) => {
          event.preventDefault();
          upload.mutate();
        }}
      >
        <Input
          ref={fileRef}
          type="file"
          onChange={(event) => setFile(event.target.files?.[0] ?? null)}
        />
        <Button type="submit" disabled={!file || upload.isPending}>
          上传
        </Button>
        <span className="text-xs text-muted-foreground">
          同名文件会作为新版本保留，旧版本不会被覆盖。
        </span>
      </form>

      {attachments.isError && (
        <AlertPanel tone="error" title="附件加载失败" description={errorText(attachments.error)} />
      )}
      {!attachments.isLoading && rows.length === 0 && (
        <AlertPanel title="还没有附件" description="上传代码包、实验结果或截图作为验收证据。" />
      )}

      <ul className="space-y-2">
        {rows.map((attachment) => (
          <li key={attachment.attachment_id} className="flex flex-wrap items-center gap-2 rounded-md border p-3">
            <div className="min-w-0 flex-1">
              <div className="truncate font-medium">{attachment.name}</div>
              <div className="text-xs text-muted-foreground">
                {attachment.code} · v{attachment.version} · {formatSize(attachment.size)} ·
                sha256 {attachment.sha256.slice(0, 12)}…
              </div>
            </div>
            <Badge variant="secondary">{STATUS_LABEL[attachment.status] ?? attachment.status}</Badge>
            <Button size="sm" variant="outline" onClick={() => handleDownload(attachment)}>
              下载
            </Button>
            <Button
              size="sm"
              variant="ghost"
              onClick={() => {
                setRemoving(attachment);
                setReason("");
              }}
            >
              移除
            </Button>
          </li>
        ))}
      </ul>

      {removing && (
        <div className="space-y-2 rounded-md border p-3">
          <div className="text-sm font-medium">
            移除 {removing.name}？历史版本仍保留，只是不再作为当前材料。
          </div>
          <Input
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            placeholder="移除原因（必填）"
          />
          <div className="flex gap-2">
            <Button size="sm" onClick={() => remove.mutate()} disabled={remove.isPending || !reason.trim()}>
              确认移除
            </Button>
            <Button size="sm" variant="outline" onClick={() => setRemoving(null)}>
              取消
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
