import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Layers } from "lucide-react";
import { toast } from "sonner";

import {
  editSessionContext,
  getSessionContext,
  refreshSessionContext,
  resetSessionContext,
  type SessionContextEdit,
} from "@/api/sessionContext";
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
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { useChatPrefs } from "@/stores/chatPrefs";

/** 抽屉里的编辑表单；列表按"一行一条"编辑（与服务端按整组替换一致）。 */
interface EditForm {
  topic: string;
  goal: string;
  confirmed: string;
  constraints: string;
  open_questions: string;
  summary: string;
}

function toLines(text: string): string[] {
  return text
    .split("\n")
    .map((s) => s.trim())
    .filter(Boolean);
}

function messageOf(err: unknown, fallback: string): string {
  return err instanceof Error ? err.message : fallback;
}

/**
 * 聊天/知识库对话顶部的「本会话上下文」入口（第二期设计方案 §4.2）。
 *
 * 内容直接来自服务端 `GET /conversations/{id}/context`——注入给模型的
 * 就是这一份，界面不会与实际注入不一致。编辑过的条目会被服务端标记为
 * 用户明确修改的结论，自动更新不会静默覆盖（P2-05）。
 */
export function SessionContextBadge({
  conversationId,
  conversationKey,
}: {
  conversationId?: string;
  /** 偏好存储键（"new" / 会话 id / "rag:new" …），与记忆开关用同一套键 */
  conversationKey: string;
}) {
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState<EditForm | null>(null);
  const [confirmReset, setConfirmReset] = useState(false);
  const prefs = useChatPrefs();
  const queryClient = useQueryClient();

  const { data, refetch } = useQuery({
    queryKey: ["session-context", conversationId],
    queryFn: () => getSessionContext(conversationId as string),
    enabled: !!conversationId,
  });

  const invalidate = () =>
    queryClient.invalidateQueries({
      queryKey: ["session-context", conversationId],
    });

  const refreshMutation = useMutation({
    mutationFn: () => refreshSessionContext(conversationId as string),
    onSuccess: () => invalidate(),
    onError: (err) => toast.error(messageOf(err, "更新摘要失败")),
  });

  const editMutation = useMutation({
    mutationFn: (patch: SessionContextEdit) =>
      editSessionContext(conversationId as string, patch),
    onSuccess: () => {
      setForm(null);
      invalidate();
      toast.success("已保存；本会话后续回答立即使用编辑后的内容");
    },
    onError: (err) => toast.error(messageOf(err, "保存失败")),
  });

  const resetMutation = useMutation({
    mutationFn: () => resetSessionContext(conversationId as string),
    onSuccess: () => {
      setConfirmReset(false);
      invalidate();
      toast.success("已重置本会话上下文；个人偏好未受影响");
    },
    onError: (err) => toast.error(messageOf(err, "重置失败")),
  });

  // 会话尚未创建时用本地偏好；已创建后以服务端标记为准
  const pendingTemporary = prefs.temporary[conversationKey] === true;
  const temporary = data ? data.temporary : pendingTemporary;
  const temporaryLocked = !!conversationId && !!data;

  const label = temporary
    ? "临时对话 · 不保存上下文"
    : "本会话上下文已开启";

  function openDialog() {
    setOpen(true);
    setConfirmReset(false);
    // 打开时取最新内容：刚聊完的轮次也要能看到
    if (conversationId) void refetch();
  }

  function startEdit() {
    if (!data) return;
    setForm({
      topic: data.topic,
      goal: data.goal,
      confirmed: data.confirmed.join("\n"),
      constraints: data.constraints.join("\n"),
      open_questions: data.open_questions.join("\n"),
      summary: data.summary,
    });
  }

  return (
    <>
      <div className="flex items-center gap-2">
        <Badge variant={temporary ? "outline" : "secondary"}>
          <Layers className="mr-1 h-3 w-3" />
          {label}
        </Badge>
        <Button variant="outline" size="sm" onClick={openDialog}>
          上下文
        </Button>
      </div>

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="max-h-[85vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>本会话上下文</DialogTitle>
          </DialogHeader>

          <div className="space-y-3">
            <p className="text-xs text-muted-foreground">
              上下文只用于当前会话，不会带入其他聊天窗口；已确认事项只来自你在
              本会话中的表述，模型回答与知识库文档不会被当作结论。本会话中的
              新要求优先于这里的旧内容。
            </p>

            <div className="flex items-center justify-between">
              <div>
                <Label>临时对话</Label>
                <p className="text-xs text-muted-foreground">
                  临时对话不创建、不保留会话上下文，也不能写入个人偏好；
                  个人偏好仍可正常使用（可在“记忆”入口关闭）。
                </p>
              </div>
              <Switch
                aria-label="临时对话"
                checked={temporary}
                disabled={temporaryLocked}
                onCheckedChange={(v) =>
                  prefs.setTemporary(conversationKey, v === true)
                }
              />
            </div>
            {temporaryLocked && (
              <p className="text-xs text-muted-foreground">
                会话已创建，临时标记不能更改；需要另一种模式请新建会话。
              </p>
            )}
            {!conversationId && (
              <p className="text-xs text-muted-foreground">
                当前还没有会话；发送第一条消息后创建，临时标记同时生效。
              </p>
            )}

            {temporary ? (
              <p className="rounded-md border p-3 text-sm text-muted-foreground">
                临时对话不保存会话上下文，因此这里没有可查看的内容。
              </p>
            ) : !conversationId ? (
              <p className="rounded-md border p-3 text-sm text-muted-foreground">
                开始对话后可查看本会话的主题、已确认事项与摘要。
              </p>
            ) : !data ? (
              <p className="rounded-md border p-3 text-sm text-muted-foreground">
                正在读取本会话上下文…
              </p>
            ) : form ? (
              <div className="space-y-2">
                <div>
                  <Label htmlFor="ctx-topic">主题</Label>
                  <Input
                    id="ctx-topic"
                    maxLength={200}
                    value={form.topic}
                    onChange={(e) => setForm({ ...form, topic: e.target.value })}
                  />
                </div>
                <div>
                  <Label htmlFor="ctx-goal">目标</Label>
                  <Input
                    id="ctx-goal"
                    maxLength={500}
                    value={form.goal}
                    onChange={(e) => setForm({ ...form, goal: e.target.value })}
                  />
                </div>
                <div>
                  <Label htmlFor="ctx-confirmed">已确认事项（每行一条）</Label>
                  <Textarea
                    id="ctx-confirmed"
                    rows={4}
                    value={form.confirmed}
                    onChange={(e) =>
                      setForm({ ...form, confirmed: e.target.value })
                    }
                  />
                </div>
                <div>
                  <Label htmlFor="ctx-constraints">限制条件（每行一条）</Label>
                  <Textarea
                    id="ctx-constraints"
                    rows={3}
                    value={form.constraints}
                    onChange={(e) =>
                      setForm({ ...form, constraints: e.target.value })
                    }
                  />
                </div>
                <div>
                  <Label htmlFor="ctx-questions">待解决问题（每行一条）</Label>
                  <Textarea
                    id="ctx-questions"
                    rows={3}
                    value={form.open_questions}
                    onChange={(e) =>
                      setForm({ ...form, open_questions: e.target.value })
                    }
                  />
                </div>
                <div>
                  <Label htmlFor="ctx-summary">较早内容摘要</Label>
                  <Textarea
                    id="ctx-summary"
                    rows={6}
                    maxLength={4000}
                    value={form.summary}
                    onChange={(e) =>
                      setForm({ ...form, summary: e.target.value })
                    }
                  />
                </div>
                <div className="flex gap-2">
                  <Button
                    size="sm"
                    disabled={editMutation.isPending}
                    onClick={() =>
                      editMutation.mutate({
                        topic: form.topic.trim(),
                        goal: form.goal.trim(),
                        confirmed: toLines(form.confirmed),
                        constraints: toLines(form.constraints),
                        open_questions: toLines(form.open_questions),
                        summary: form.summary.trim(),
                      })
                    }
                  >
                    保存编辑
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => setForm(null)}
                  >
                    取消
                  </Button>
                </div>
                <p className="text-xs text-muted-foreground">
                  你修改过的条目会作为“用户明确修改的结论”保留，自动更新不会
                  静默覆盖；本会话中更靠后的新要求仍可取代旧结论。
                </p>
              </div>
            ) : (
              <div className="space-y-3">
                <div className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-sm">
                  <span className="text-muted-foreground">主题：</span>
                  <span>{data.topic || "（尚未识别出主题）"}</span>
                  <span className="text-muted-foreground">目标：</span>
                  <span>{data.goal || "（尚未识别出目标）"}</span>
                </div>

                <ContextList title="已确认事项" items={data.confirmed} />
                <ContextList title="限制条件" items={data.constraints} />
                <ContextList title="待解决问题" items={data.open_questions} />

                <div>
                  <div className="text-sm font-medium">较早内容摘要</div>
                  {data.summary ? (
                    <p className="mt-1 whitespace-pre-wrap rounded-md border p-3 text-sm">
                      {data.summary}
                    </p>
                  ) : (
                    <p className="mt-1 rounded-md border p-3 text-sm text-muted-foreground">
                      还没有摘要。较早的内容仍以完整原文参与回答，
                      达到一定轮数后系统会自动压缩。
                    </p>
                  )}
                  {data.summarized_count > 0 && (
                    <p className="mt-1 text-xs text-muted-foreground">
                      已覆盖 {data.summarized_count} 条较早消息；最近的关键消息
                      始终以完整原文参与回答。
                    </p>
                  )}
                </div>

                <div className="flex flex-wrap gap-2">
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={refreshMutation.isPending}
                    onClick={() => refreshMutation.mutate()}
                  >
                    {refreshMutation.isPending ? "更新中…" : "更新摘要"}
                  </Button>
                  <Button size="sm" variant="outline" onClick={startEdit}>
                    编辑摘要
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    className="text-destructive"
                    onClick={() => setConfirmReset(true)}
                  >
                    重置本会话上下文
                  </Button>
                </div>

                {confirmReset && (
                  <div className="space-y-2 rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm">
                    <p>
                      将清空本会话的主题、目标、已确认事项、限制条件、待办与摘要；
                      聊天历史与个人偏好不受影响，也不会影响其他会话。
                    </p>
                    <div className="flex gap-2">
                      <Button
                        size="sm"
                        variant="destructive"
                        disabled={resetMutation.isPending}
                        onClick={() => resetMutation.mutate()}
                      >
                        确认重置
                      </Button>
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={() => setConfirmReset(false)}
                      >
                        取消
                      </Button>
                    </div>
                  </div>
                )}
              </div>
            )}
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
}

function ContextList({ title, items }: { title: string; items: string[] }) {
  return (
    <div>
      <div className="text-sm font-medium">{title}</div>
      {items.length > 0 ? (
        <ul className="mt-1 list-disc space-y-1 rounded-md border p-3 pl-6 text-sm">
          {items.map((item, i) => (
            <li key={`${i}-${item}`}>{item}</li>
          ))}
        </ul>
      ) : (
        <p className="mt-1 rounded-md border p-3 text-sm text-muted-foreground">
          （暂无）
        </p>
      )}
    </div>
  );
}
