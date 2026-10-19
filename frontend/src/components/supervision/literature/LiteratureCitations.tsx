import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import {
  createLiteratureCitation,
  listLiteratureCitations,
  revokeLiteratureCitation,
} from "@/api/supervision";
import type { ProjectDetail } from "@/api/supervision";
import { AlertPanel, errorText } from "@/components/supervision/AlertPanel";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";

const SOURCE_KIND_LABEL: Record<string, string> = {
  project_result: "本项目的论文记录",
  conclusion: "本项目的文献结论",
  private_paper: "私人论文研究里的论文",
  private_evidence_card: "私人论文研究里的证据卡片",
};

const PURPOSE_LABEL: Record<string, string> = {
  mentor_review: "导师审核",
  research_plan: "研究方案",
  submission: "提交材料",
  progress: "进展汇报",
};

function when(value?: string | null): string {
  return value ? value.slice(0, 16).replace("T", " ") : "未";
}

/**
 * 引用快照（把某条论文/结论按当时的内容钉一份，供导师审核、方案、提交与进展引用）。
 *
 * 口径照后端：引用带内容哈希与来源版本号，撤销只标记 revoked 并写原因，
 * 快照本身保留（否则引用过的材料会凭空消失）。
 */
export function LiteratureCitations({ project }: { project: ProjectDetail }) {
  const queryClient = useQueryClient();
  const [includeRevoked, setIncludeRevoked] = useState(false);
  const [open, setOpen] = useState(false);
  const [sourceKind, setSourceKind] = useState("project_result");
  const [sourceRef, setSourceRef] = useState("");
  const [purpose, setPurpose] = useState("mentor_review");
  const [boundType, setBoundType] = useState("");
  const [boundCode, setBoundCode] = useState("");
  const [note, setNote] = useState("");
  const [revokeRef, setRevokeRef] = useState<string | null>(null);
  const [revokeReason, setRevokeReason] = useState("");

  const citations = useQuery({
    queryKey: ["supervision", "literature", "citations", project.project_id, includeRevoked],
    queryFn: () =>
      listLiteratureCitations(project.project_id, { include_revoked: includeRevoked }),
  });

  function invalidate() {
    queryClient.invalidateQueries({ queryKey: ["supervision", "literature"] });
  }

  const create = useMutation({
    mutationFn: () =>
      createLiteratureCitation(project.project_id, {
        source_kind: sourceKind,
        source_ref: sourceRef.trim(),
        purpose,
        bound_object_type: boundType.trim(),
        bound_object_code: boundCode.trim(),
        note: note.trim(),
      }),
    onSuccess: (saved) => {
      setOpen(false);
      setSourceRef("");
      setNote("");
      toast.success(
        `引用 ${saved.code} 已建立（${SOURCE_KIND_LABEL[saved.source_kind] ?? saved.source_kind} · 哈希 ${saved.content_hash.slice(0, 12)}）`,
      );
      invalidate();
    },
    onError: (error) => toast.error(errorText(error)),
  });

  const revoke = useMutation({
    mutationFn: () => revokeLiteratureCitation(project.project_id, revokeRef as string, revokeReason.trim()),
    onSuccess: (saved) => {
      setRevokeRef(null);
      setRevokeReason("");
      toast.success(`引用 ${saved.code} 已撤销；快照与哈希保留，可追溯`);
      invalidate();
    },
    onError: (error) => toast.error(errorText(error)),
  });

  const rows = citations.data ?? [];

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="text-sm font-medium">引用快照（{rows.length}）</h3>
        <label className="flex items-center gap-2 text-xs text-muted-foreground">
          <input
            type="checkbox"
            checked={includeRevoked}
            onChange={(event) => setIncludeRevoked(event.target.checked)}
          />
          显示已撤销
        </label>
        <Button size="sm" variant="outline" onClick={() => setOpen((value) => !value)}>
          新建引用
        </Button>
      </div>

      {open && (
        <div className="space-y-2 rounded-md border border-dashed p-3">
          <p className="text-xs text-muted-foreground">
            引用会把来源当时的内容钉成快照（含版本号与内容哈希），之后来源被改写也不会让
            引用过的材料变样。私人研究项目的论文与证据卡片只有本人能引用。
          </p>
          <div className="flex flex-wrap gap-2">
            <div className="space-y-1">
              <Label className="text-xs text-muted-foreground">来源种类</Label>
              <Select value={sourceKind} onValueChange={setSourceKind}>
                <SelectTrigger className="h-9 w-[240px]">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {Object.entries(SOURCE_KIND_LABEL).map(([key, text]) => (
                    <SelectItem key={key} value={key}>
                      {text}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1">
              <Label htmlFor="lit-citation-ref" className="text-xs text-muted-foreground">
                来源编号（如 P-001 / LC-001 / EC-001）
              </Label>
              <Input
                id="lit-citation-ref"
                value={sourceRef}
                onChange={(event) => setSourceRef(event.target.value)}
                className="w-[220px]"
              />
            </div>
            <div className="space-y-1">
              <Label className="text-xs text-muted-foreground">用途</Label>
              <Select value={purpose} onValueChange={setPurpose}>
                <SelectTrigger className="h-9 w-[180px]">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {Object.entries(PURPOSE_LABEL).map(([key, text]) => (
                    <SelectItem key={key} value={key}>
                      {text}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1">
              <Label htmlFor="lit-citation-bound-code" className="text-xs text-muted-foreground">
                挂到哪个对象（可选：对象类型 + 编号）
              </Label>
              <div className="flex gap-2">
                <Input
                  value={boundType}
                  onChange={(event) => setBoundType(event.target.value)}
                  placeholder="例如 research_plan"
                  className="w-[160px]"
                />
                <Input
                  id="lit-citation-bound-code"
                  value={boundCode}
                  onChange={(event) => setBoundCode(event.target.value)}
                  placeholder="例如 RP-001"
                  className="w-[140px]"
                />
              </div>
            </div>
          </div>
          <div className="space-y-1">
            <Label htmlFor="lit-citation-note" className="text-xs text-muted-foreground">
              说明（可选）
            </Label>
            <Textarea
              id="lit-citation-note"
              rows={2}
              value={note}
              onChange={(event) => setNote(event.target.value)}
            />
          </div>
          <div className="flex gap-2">
            <Button
              size="sm"
              onClick={() => create.mutate()}
              disabled={create.isPending || sourceRef.trim() === ""}
            >
              建立引用
            </Button>
            <Button size="sm" variant="outline" onClick={() => setOpen(false)}>
              取消
            </Button>
          </div>
        </div>
      )}

      {citations.isError && (
        <AlertPanel tone="error" title="引用加载失败" description={errorText(citations.error)} />
      )}
      {!citations.isLoading && !citations.isError && rows.length === 0 && (
        <AlertPanel
          title="还没有引用快照"
          description="引用用于把论文或结论按当时的内容交给导师审核、写进研究方案或进展汇报。"
        />
      )}

      <ul className="space-y-2">
        {rows.map((citation) => (
          <li key={citation.citation_id} className="space-y-1 rounded-md border p-3 text-sm">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-medium">{citation.code}</span>
              <Badge variant="outline">
                {SOURCE_KIND_LABEL[citation.source_kind] ?? citation.source_kind}
              </Badge>
              <Badge variant="secondary">{PURPOSE_LABEL[citation.purpose] ?? citation.purpose}</Badge>
              {citation.revoked_at && <Badge variant="destructive">已撤销</Badge>}
              <span className="text-xs text-muted-foreground">
                {citation.source_code}（版本 {citation.source_version || "未记录"}）· 哈希{" "}
                {citation.content_hash.slice(0, 12)} · {citation.granted_by}（{citation.granted_role}）
                引用于 {when(citation.granted_at)}
              </span>
            </div>
            {citation.bound_object_code && (
              <div className="text-xs text-muted-foreground">
                挂在 {citation.bound_object_type || "未标注对象"} {citation.bound_object_code}
              </div>
            )}
            {citation.note && <div className="text-xs">{citation.note}</div>}
            {citation.revoked_at && (
              <div className="text-xs text-muted-foreground">
                {citation.revoked_by} 于 {when(citation.revoked_at)} 撤销：{citation.revoke_reason}
              </div>
            )}
            <div className="flex flex-wrap gap-2">
              {!citation.revoked_at && (
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => {
                    setRevokeRef(citation.code);
                    setRevokeReason("");
                  }}
                >
                  撤销引用
                </Button>
              )}
              <details className="text-xs text-muted-foreground">
                <summary className="cursor-pointer">查看引用快照内容</summary>
                <pre className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap rounded bg-muted/40 p-2">
                  {JSON.stringify(citation.snapshot, null, 2)}
                </pre>
              </details>
            </div>

            {revokeRef === citation.code && (
              <div className="space-y-2 rounded-md border border-dashed p-2">
                <Input
                  value={revokeReason}
                  onChange={(event) => setRevokeReason(event.target.value)}
                  placeholder="撤销原因（必填；快照保留，只是不再作为当前引用）"
                />
                <div className="flex gap-2">
                  <Button
                    size="sm"
                    onClick={() => revoke.mutate()}
                    disabled={revoke.isPending || revokeReason.trim() === ""}
                  >
                    确认撤销
                  </Button>
                  <Button size="sm" variant="outline" onClick={() => setRevokeRef(null)}>
                    算了
                  </Button>
                </div>
              </div>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}
