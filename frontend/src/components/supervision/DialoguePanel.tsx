import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import { toast } from "sonner";

import {
  createDialogue,
  extractBrief,
  getDialogueMessages,
  getProjectContext,
  listDialogues,
  regenerateDialogueReply,
  sendDialogueMessage,
} from "@/api/supervision";
import type { ProjectDetail } from "@/api/supervision";
import { AlertPanel, errorText } from "@/components/supervision/AlertPanel";
import {
  MODEL_UNAVAILABLE_HINT,
  formatTime,
  isConflict,
  isModelUnavailable,
  isNotFound,
  roleLabel,
} from "@/components/supervision/research/briefModel";
import { MarkdownBlock } from "@/components/supervision/research/MarkdownBlock";
import {
  MAX_MESSAGE_CHARS,
  defaultDialogueTitle,
  dialogueLabel,
  dialogueTurns,
  dialogueUpdatedText,
  messageExcerpt,
  messageLengthIssue,
  selectableMessageIds,
} from "@/components/supervision/research/dialogueModel";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";

/** 讨论回复是同步返回的（没有 SSE），这里只轮询列表补上别人的改动。 */
const POLL_INTERVAL_MS = 5000;

/**
 * 项目内研究讨论（《学生研究与导师审核功能重新设计方案》里程碑2）。
 *
 * 必须照后端语义显示的几条口径：
 * 1. 会话绑定本项目、按所有者隔离——列表里只有当前用户自己的会话，
 *    另一方的原始讨论读不到（后端按 owner + 成员双重校验）；
 * 2. 发言先落库、再生成回复。模型未接入时接口 503，但发言本身已保存
 *    （`metadata.pending_reply`），界面显示成「已保存，尚未回复」+ 补回复，
 *    不能显示成发送失败；
 * 3. 已有回复的发言再补回复会 409（不重复烧模型），文案原样显示；
 * 4. 提取只生成研究定义草稿，绝不自动共享。
 */
export function DialoguePanel({
  project,
  refresh,
}: {
  project: ProjectDetail;
  refresh: () => void;
}) {
  const queryClient = useQueryClient();
  const [, setSearchParams] = useSearchParams();
  const [title, setTitle] = useState("");
  const [text, setText] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const [picked, setPicked] = useState<string[]>([]);
  const [extractHint, setExtractHint] = useState<string | null>(null);
  const [modelUnavailable, setModelUnavailable] = useState(false);
  const [extractedCode, setExtractedCode] = useState<string | null>(null);
  const [sendPendingHint, setSendPendingHint] = useState<string | null>(null);
  const [sendError, setSendError] = useState<string | null>(null);
  const [replyHint, setReplyHint] = useState<string | null>(null);
  const [replyTarget, setReplyTarget] = useState<string | null>(null);

  const dialogues = useQuery({
    queryKey: ["supervision", "dialogues", project.project_id],
    queryFn: () => listDialogues(project.project_id),
  });

  const context = useQuery({
    queryKey: ["supervision", "project-context", project.project_id],
    queryFn: () => getProjectContext(project.project_id),
  });

  const rows = dialogues.data ?? [];
  const activeId = selected ?? rows[0]?.conversation_id ?? null;
  const active = rows.find((row) => row.conversation_id === activeId) ?? null;

  const messages = useQuery({
    queryKey: ["supervision", "dialogues", project.project_id, activeId, "messages"],
    queryFn: () => getDialogueMessages(project.project_id, activeId as string),
    enabled: Boolean(activeId),
    refetchInterval: POLL_INTERVAL_MS,
  });

  const turns = useMemo(() => dialogueTurns(messages.data?.messages ?? []), [messages.data]);
  const selectable = useMemo(
    () => selectableMessageIds(messages.data?.messages ?? []),
    [messages.data],
  );

  function invalidateMessages() {
    queryClient.invalidateQueries({ queryKey: ["supervision", "dialogues"] });
  }

  const lengthIssue = messageLengthIssue(text);

  const create = useMutation({
    mutationFn: () => createDialogue(project.project_id, { title: title.trim() }),
    onSuccess: (dialogue) => {
      setTitle("");
      setSelected(dialogue.conversation_id);
      toast.success(`讨论会话 ${dialogue.code} 已就绪`);
      invalidateMessages();
      refresh();
    },
    onError: (error) => toast.error(errorText(error)),
  });

  /**
   * 进入项目时自动建立当前用户自己的研究会话（§10 创建流程、§16.3 第1条）：
   * 后端 `create_dialogue` 幂等（已有则复用），所以挂载时调用是安全的，
   * 用户不必先点「新建讨论」才能开始讨论。
   */
  const autoCreated = useRef(false);
  const ensureDialogue = useMutation({
    mutationFn: () =>
      createDialogue(project.project_id, { title: defaultDialogueTitle(project.name) }),
    onSuccess: (dialogue) => {
      setSelected(dialogue.conversation_id);
      invalidateMessages();
    },
    onError: (error) => {
      toast.error(errorText(error));
    },
  });

  useEffect(() => {
    if (dialogues.isLoading || dialogues.isError) return;
    if ((dialogues.data?.length ?? 0) > 0) return;
    if (autoCreated.current) return;
    autoCreated.current = true;
    ensureDialogue.mutate();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dialogues.data?.length, dialogues.isLoading, dialogues.isError]);

  const send = useMutation({
    mutationFn: () => sendDialogueMessage(project.project_id, activeId as string, text.trim()),
    onSuccess: () => {
      setText("");
      setSendError(null);
      setSendPendingHint(null);
      toast.success("已发送，AI 回复已生成");
      invalidateMessages();
    },
    onError: (error) => {
      if (isModelUnavailable(error)) {
        // 发言本身已经落库并提交，只是回复没生成：不能显示成发送失败。
        setText("");
        setSendError(null);
        setSendPendingHint(errorText(error));
        toast.warning("发言已保存，AI 回复尚未生成");
        invalidateMessages();
        return;
      }
      setSendPendingHint(null);
      setSendError(errorText(error));
      toast.error(errorText(error));
    },
  });

  const reply = useMutation({
    mutationFn: (messageId: string) => regenerateDialogueReply(project.project_id, messageId),
    onSuccess: () => {
      setReplyTarget(null);
      setReplyHint(null);
      toast.success("AI 回复已补上");
      invalidateMessages();
    },
    onError: (error) => {
      setReplyTarget(null);
      // 409 = 这条发言已有回复，不重复烧模型；503 = 模型仍未接入，保持待回复。
      setReplyHint(errorText(error));
      if (isConflict(error)) invalidateMessages();
    },
  });

  const extract = useMutation({
    mutationFn: () =>
      extractBrief(project.project_id, {
        conversation_id: activeId as string,
        message_ids: picked,
      }),
    onSuccess: (brief) => {
      setModelUnavailable(false);
      setExtractHint(null);
      setExtractedCode(brief.display_code);
      setPicked([]);
      toast.success(`已生成研究定义草稿 ${brief.display_code}`);
      queryClient.invalidateQueries({ queryKey: ["supervision", "briefs"] });
      queryClient.invalidateQueries({ queryKey: ["supervision", "shared-briefs"] });
      refresh();
    },
    onError: (error) => {
      if (isModelUnavailable(error)) {
        setModelUnavailable(true);
        setExtractHint(null);
        return;
      }
      setModelUnavailable(false);
      setExtractHint(errorText(error));
    },
  });

  function goBriefs() {
    setSearchParams({ tab: "brief" });
  }

  return (
    <div className="space-y-4">
      <AlertPanel
        title="讨论内容属于本项目，只有项目成员能看到"
        description={
          "会话按所有者隔离：列表里只有你自己的研究讨论会话，" +
          `${roleLabel(project.my_role ?? "student")}一侧的原始讨论对方读不到，你也读不到对方的。` +
          "普通聊天不会自动成为正式研究定义；只有你确认共享的研究定义才进入项目共享区。"
        }
      />

      <div className="grid gap-4 md:grid-cols-[18rem_1fr]">
        <aside className="space-y-3">
          <form
            className="space-y-2 rounded-md border p-3"
            onSubmit={(event) => {
              event.preventDefault();
              if (!title.trim()) return;
              create.mutate();
            }}
          >
            <div className="text-sm font-medium">新建研究讨论</div>
            <div className="space-y-1">
              <Label htmlFor="dialogue-title" className="text-xs text-muted-foreground">
                标题
              </Label>
              <Input
                id="dialogue-title"
                value={title}
                onChange={(event) => setTitle(event.target.value)}
                placeholder="例如：研究问题澄清"
                maxLength={50}
              />
            </div>
            <Button type="submit" size="sm" disabled={create.isPending || !title.trim()}>
              新建讨论
            </Button>
            <p className="text-xs text-muted-foreground">
              每个成员在本项目里默认只有一条主研究会话；已有会话时后端直接复用那条，
              不会新建第二条，也不会把标题改成新写的名字。
            </p>
          </form>

          {dialogues.isLoading && <p className="text-sm text-muted-foreground">加载中…</p>}
          {dialogues.isError && (
            <AlertPanel tone="error" title="讨论会话加载失败" description={errorText(dialogues.error)} />
          )}
          {!dialogues.isLoading && !dialogues.isError && rows.length === 0 && (
            <div className="space-y-2">
              {ensureDialogue.isError ? (
                <AlertPanel
                  tone="error"
                  title="建立研究讨论会话失败"
                  description={errorText(ensureDialogue.error)}
                />
              ) : (
                <AlertPanel
                  title="正在建立你的研究讨论会话"
                  description={
                    `标题默认是「${defaultDialogueTitle(project.name)}」，之后可以改名。` +
                    "学生与导师在同一项目里各自一条会话，互不可见。"
                  }
                />
              )}
              {ensureDialogue.isError && (
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => {
                    autoCreated.current = true;
                    ensureDialogue.mutate();
                  }}
                >
                  重试建立会话
                </Button>
              )}
            </div>
          )}

          <ul className="space-y-1">
            {rows.map((dialogue) => (
              <li key={dialogue.conversation_id}>
                <button
                  type="button"
                  onClick={() => {
                    setSelected(dialogue.conversation_id);
                    setPicked([]);
                    setExtractedCode(null);
                    setSendPendingHint(null);
                    setSendError(null);
                    setReplyHint(null);
                  }}
                  className={cn(
                    "w-full rounded-md border p-2 text-left text-sm hover:bg-accent hover:text-accent-foreground",
                    dialogue.conversation_id === activeId && "border-primary bg-accent/60",
                  )}
                >
                  <div className="truncate font-medium">{dialogue.name || "未命名讨论"}</div>
                  <div className="text-xs text-muted-foreground">
                    {dialogueLabel(dialogue)} · {dialogueUpdatedText(dialogue)}
                  </div>
                </button>
              </li>
            ))}
          </ul>
        </aside>

        <section className="space-y-4">
          <div className="space-y-3 rounded-md border p-3">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-sm font-medium">
                {active ? active.name || "未命名讨论" : "讨论记录"}
              </span>
              {active && <Badge variant="secondary">{active.code}</Badge>}
              {active && (
                <Badge variant="outline">
                  我的会话 · {roleLabel(project.my_role ?? "student")} · 私有
                </Badge>
              )}
              <span className="text-xs text-muted-foreground">
                回复同步返回；列表每 {POLL_INTERVAL_MS / 1000} 秒刷新一次
              </span>
            </div>

            {!activeId && (
              <AlertPanel title="还没有可选的讨论会话" description="先在左侧新建一条研究讨论会话。" />
            )}
            {activeId && messages.isError && (
              <AlertPanel
                tone="error"
                title="打不开这条讨论的原始消息"
                description={
                  isNotFound(messages.error)
                    ? "这条会话不属于你（或已被删除）：讨论消息只按所有者返回。"
                    : errorText(messages.error)
                }
              />
            )}
            {activeId && !messages.isError && turns.length === 0 && (
              <AlertPanel
                title="这条会话还没有消息"
                description="在下面写下第一条讨论内容，或到聊天页选中同一条会话继续。"
              />
            )}

            {replyHint && (
              <AlertPanel
                tone={isModelUnavailable(reply.error) ? "warning" : "error"}
                title={isModelUnavailable(reply.error) ? "模型能力尚未接入" : "补生成回复失败"}
                description={replyHint}
              />
            )}

            <ul className="space-y-2">
              {turns.map((turn) => (
                <li
                  key={turn.key}
                  className={cn("rounded-md border p-3", turn.role === "member" ? "bg-muted/60" : "bg-background")}
                >
                  <div className="mb-1 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                    <span className="font-medium text-foreground">
                      {turn.role === "member"
                        ? `我（${roleLabel(project.my_role ?? "student")}）`
                        : "AI 回复"}
                    </span>
                    {turn.time && <span>{turn.time}</span>}
                    {turn.modelName && <span>模型 {turn.modelName}</span>}
                    {turn.truncated && <Badge variant="outline">回复被截断</Badge>}
                    {turn.pendingReply && <Badge variant="outline">AI 尚未回复</Badge>}
                  </div>
                  {turn.pendingReply ? (
                    <div className="space-y-2">
                      <p className="text-sm text-muted-foreground">
                        这条发言已经保存下来了，只是还没有 AI 回复（模型未接入，或上次生成失败）。
                        补生成回复不会重复发送发言。
                      </p>
                      <Button
                        size="sm"
                        variant="outline"
                        disabled={reply.isPending}
                        onClick={() => {
                          setReplyTarget(turn.message_id);
                          setReplyHint(null);
                          reply.mutate(turn.message_id);
                        }}
                      >
                        {replyTarget === turn.message_id && reply.isPending
                          ? "生成中…"
                          : "重新生成回复"}
                      </Button>
                    </div>
                  ) : (
                    <MarkdownBlock text={turn.text} />
                  )}
                </li>
              ))}
            </ul>

            <form
              className="space-y-2 border-t pt-3"
              onSubmit={(event) => {
                event.preventDefault();
                if (lengthIssue) {
                  setSendError(lengthIssue);
                  return;
                }
                setSendError(null);
                send.mutate();
              }}
            >
              <Label htmlFor="dialogue-composer" className="text-sm font-medium">
                接着讨论
              </Label>
              <Textarea
                id="dialogue-composer"
                rows={3}
                value={text}
                disabled={!activeId}
                onChange={(event) => setText(event.target.value)}
                placeholder="写下你的问题、对 AI 回答的回应，或下一步打算怎么验证"
              />
              <div className="flex flex-wrap items-center gap-2">
                <Button
                  type="submit"
                  size="sm"
                  disabled={!activeId || send.isPending || Boolean(lengthIssue)}
                >
                  {send.isPending ? "发送并等待回复…" : "发送并请求 AI 回复"}
                </Button>
                <span className="text-xs text-muted-foreground">
                  {text.trim().length} / {MAX_MESSAGE_CHARS} 字
                </span>
              </div>
              <p className="text-xs text-muted-foreground">
                AI 回复同步返回，没有流式输出；模型上下文只含你自己这条会话的历史与项目已确认
                共享的研究定义，不含另一方的私人聊天。
              </p>
            </form>

            {sendPendingHint && (
              <AlertPanel
                tone="warning"
                title="发言已保存，AI 尚未回复"
                description={`${sendPendingHint} 在上面的讨论记录里对这条发言点「重新生成回复」即可补上。`}
              />
            )}
            {sendError && <AlertPanel tone="error" title="发送失败" description={sendError} />}
          </div>

          <div className="space-y-2 rounded-md border p-3">
            <div className="text-sm font-medium">从这条讨论提取研究定义</div>
            <p className="text-xs text-muted-foreground">
              提取只生成草稿（例如 RB-001-v1），不会自动共享，也不会改动讨论记录；
              重新提取会生成新版本，不覆盖旧版本。默认整条会话，也可只勾选其中几条发言。
            </p>
            <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
              <Button
                size="sm"
                variant="outline"
                disabled={selectable.length === 0}
                onClick={() => setPicked(picked.length > 0 ? [] : selectable)}
              >
                {picked.length > 0 ? "清空选择" : "选择全部发言"}
              </Button>
              <span>
                {picked.length > 0 ? `已选 ${picked.length} 条发言` : "未选择：按整条会话提取"}
              </span>
            </div>
            {selectable.length > 0 && (
              <ul className="max-h-48 space-y-1 overflow-y-auto">
                {(messages.data?.messages ?? [])
                  .filter((row) => selectable.includes(row.message_id))
                  .map((row) => (
                    <li key={row.message_id} className="flex items-start gap-2 text-sm">
                      <input
                        type="checkbox"
                        className="mt-1"
                        checked={picked.includes(row.message_id)}
                        onChange={(event) =>
                          setPicked((current) =>
                            event.target.checked
                              ? [...current, row.message_id]
                              : current.filter((id) => id !== row.message_id),
                          )
                        }
                      />
                      <span className="min-w-0 flex-1 truncate text-muted-foreground">
                        {formatTime(row.create_time) ? `${formatTime(row.create_time)} · ` : ""}
                        {messageExcerpt(row)}
                      </span>
                    </li>
                  ))}
              </ul>
            )}
            <div className="flex flex-wrap gap-2">
              <Button
                size="sm"
                onClick={() => extract.mutate()}
                disabled={!activeId || extract.isPending}
              >
                提取研究定义草稿
              </Button>
              <Button size="sm" variant="outline" onClick={goBriefs}>
                手动填写研究定义
              </Button>
            </div>

            {modelUnavailable && (
              <AlertPanel tone="warning" title="模型能力尚未接入" description={MODEL_UNAVAILABLE_HINT} />
            )}
            {extractHint && (
              <AlertPanel tone="error" title="提取失败" description={extractHint} />
            )}
            {extractedCode && (
              <div className="space-y-2 rounded-md border border-emerald-500/40 bg-emerald-500/5 p-2 text-sm">
                <span>已生成草稿 {extractedCode}，只有你自己能看到。</span>
                <div>
                  <Button size="sm" variant="outline" onClick={goBriefs}>
                    去研究定义页编辑并确认
                  </Button>
                </div>
              </div>
            )}
          </div>

          <div className="space-y-2 rounded-md border p-3">
            <div className="text-sm font-medium">去聊天页继续这段讨论</div>
            <p className="text-xs text-muted-foreground">
              研究讨论会话就是聊天页里属于你自己的那条会话
              {active ? `（${active.code} · ${active.name || "未命名讨论"}）` : ""}。
              本页可以直接发言并拿到 AI 回复；要用附件、知识库引用或流式输出，
              请到聊天页选中这条会话继续，两边读的是同一份记录。
            </p>
            <Button asChild size="sm" variant="outline">
              <Link to="/chat">打开聊天页</Link>
            </Button>
            <details className="text-xs">
              <summary className="cursor-pointer text-muted-foreground">
                聊天时会注入的项目上下文（只读预览）
              </summary>
              {context.isLoading && <p className="mt-1 text-muted-foreground">读取中…</p>}
              {context.isError && <p className="mt-1 text-destructive">{errorText(context.error)}</p>}
              {context.data && !context.data.has_project_context && (
                <p className="mt-1 text-muted-foreground">
                  还没有已确认共享的研究定义，因此当前不会注入任何项目内容。
                </p>
              )}
              {context.data?.has_project_context && (
                <pre className="mt-1 max-h-64 overflow-auto whitespace-pre-wrap rounded bg-muted/40 p-2">
                  {context.data.context}
                </pre>
              )}
              <p className="mt-1 text-muted-foreground">
                注入的内容只包含已确认共享的结构化研究定义，不含任何成员的私人聊天，
                也不是个人偏好记忆。
              </p>
            </details>
          </div>
        </section>
      </div>
    </div>
  );
}
