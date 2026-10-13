import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Upload } from "lucide-react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { toast } from "sonner";

import { listMessages } from "@/api/conversations";
import { listKnowledgeBases, uploadTempDocs } from "@/api/kb";
import type { RagMode } from "@/api/rag";
import { getServerModels } from "@/api/server";
import { Composer } from "@/components/chat/Composer";
import { ConversationSidebar } from "@/components/chat/ConversationSidebar";
import { MessageList } from "@/components/chat/MessageList";
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
import {
  historyToUiMessages,
} from "@/hooks/useChatStream";
import { useRagStream } from "@/hooks/useRagStream";
import { useChatPrefs } from "@/stores/chatPrefs";

const MODES: { value: RagMode; label: string }[] = [
  { value: "local_kb", label: "知识库" },
  { value: "temp_kb", label: "临时文件" },
  { value: "search_engine", label: "联网搜索" },
];

export function RagPage() {
  const { conversationId } = useParams<{ conversationId?: string }>();
  const navigate = useNavigate();
  const prefs = useChatPrefs();
  const rag = useRagStream({
    conversationId,
    onConversationId: (id) => navigate(`/rag/${id}`, { replace: true }),
  });

  // 知识库管理页的「去对话」用 /rag?kb=<名字> 过来：不带会话 id，
  // 因此每次点击都是一个新会话，且已经选中那个知识库。
  const [searchParams] = useSearchParams();
  const kbParam = searchParams.get("kb");

  const [mode, setMode] = useState<RagMode>("local_kb");
  const [kbName, setKbName] = useState(() => kbParam ?? "");
  const [tempId, setTempId] = useState("");
  const [tempFileName, setTempFileName] = useState("");
  const [engine, setEngine] = useState("");
  const [topK, setTopK] = useState<number | undefined>();
  const [scoreThreshold, setScoreThreshold] = useState<number | undefined>();
  const fileRef = useRef<HTMLInputElement>(null);

  const { data: kbs = [] } = useQuery({
    queryKey: ["knowledge-bases"],
    queryFn: listKnowledgeBases,
  });
  const { data: serverModels } = useQuery({
    queryKey: ["server-models"],
    queryFn: getServerModels,
  });

  // 默认选中第一个知识库 / 搜索引擎。?kb= 指定的知识库优先；
  // 名字不存在（已删除或写错）时退回第一个，避免下拉框空选导致整轮失败。
  useEffect(() => {
    if (kbs.length === 0) return;
    setKbName((cur) => {
      if (cur && kbs.some((kb) => kb.kb_name === cur)) return cur;
      if (kbParam && kbs.some((kb) => kb.kb_name === kbParam)) return kbParam;
      return kbs[0].kb_name;
    });
  }, [kbs, kbParam]);
  useEffect(() => {
    if (!engine && serverModels?.search_engines?.length)
      setEngine(serverModels.search_engines[0]);
  }, [serverModels, engine]);

  useEffect(() => {
    if (!conversationId) {
      rag.reset();
      return;
    }
    let cancelled = false;
    listMessages(conversationId)
      .then((res) => {
        if (!cancelled) rag.setInitialMessages(historyToUiMessages(res.messages));
      })
      .catch((err) => {
        if (!cancelled)
          toast.error(err instanceof Error ? err.message : "加载历史失败");
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conversationId]);

  async function handleTempUpload(files: File[]) {
    if (files.length === 0) return;
    try {
      const res = await uploadTempDocs({
        files,
        prevId: tempId || null,
      });
      setTempId(res.knowledge_base_id);
      setTempFileName(files.map((f) => f.name).join("、"));
      setMode("temp_kb");
      toast.success(`已上传 ${files.length} 个文件`);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "上传失败");
    }
  }

  const param = mode === "local_kb" ? kbName : mode === "temp_kb" ? tempId : engine;
  // 没显式选模型时显示并发送服务端默认模型（空串会让服务端 Body 默认值失效）
  const modelValue = prefs.model || serverModels?.default_llm || "";
  const draftKey = `rag:${conversationId ?? "new"}`;
  const draft = prefs.drafts[draftKey] ?? "";

  return (
    <div className="flex h-full">
      <ConversationSidebar
        chatType="kb_chat"
        activeId={conversationId}
        onSelect={(id) => navigate(id ? `/rag/${id}` : "/rag")}
      />
      <div className="flex min-w-0 flex-1 flex-col">
        <div className="space-y-2 border-b pb-3">
          <h1 className="text-lg font-semibold">知识库对话</h1>
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <Select value={mode} onValueChange={(v) => setMode(v as RagMode)}>
              <SelectTrigger className="w-28">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {MODES.map((m) => (
                  <SelectItem key={m.value} value={m.value}>
                    {m.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>

            {mode === "local_kb" && (
              <Select value={kbName} onValueChange={setKbName}>
                <SelectTrigger className="w-48">
                  <SelectValue placeholder="选择知识库" />
                </SelectTrigger>
                <SelectContent>
                  {kbs.map((kb) => (
                    <SelectItem key={kb.kb_name} value={kb.kb_name}>
                      {kb.kb_name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}

            {mode === "temp_kb" && (
              <>
                <input
                  ref={fileRef}
                  type="file"
                  multiple
                  className="hidden"
                  onChange={(e) =>
                    handleTempUpload(Array.from(e.target.files ?? []))
                  }
                />
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => fileRef.current?.click()}
                >
                  <Upload className="h-4 w-4" />
                  {tempId ? "追加文件" : "上传文件"}
                </Button>
                {tempFileName && (
                  <span className="max-w-52 truncate text-xs text-muted-foreground">
                    {tempFileName}
                  </span>
                )}
              </>
            )}

            {mode === "search_engine" && (
              <Select value={engine} onValueChange={setEngine}>
                <SelectTrigger className="w-36">
                  <SelectValue placeholder="搜索引擎" />
                </SelectTrigger>
                <SelectContent>
                  {(serverModels?.search_engines ?? []).map((e) => (
                    <SelectItem key={e} value={e}>
                      {e}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}

            {/* 模型：与聊天设置共用同一个偏好（prefs.model）。
                没选过模型时这里显示服务端默认模型，发请求时也带上真实名字 ——
                以前发的是空串，服务端 Body 默认值不生效，整轮对话直接失败。 */}
            <div className="flex items-center gap-1">
              <Label className="text-xs text-muted-foreground">模型</Label>
              <Select value={modelValue} onValueChange={prefs.setModel}>
                <SelectTrigger className="w-44">
                  <SelectValue placeholder="服务端默认" />
                </SelectTrigger>
                <SelectContent>
                  {(serverModels?.llm_models ?? []).map((m) => (
                    <SelectItem key={m} value={m}>
                      {m}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              {!prefs.model && (
                <span className="text-xs text-muted-foreground">（服务端默认）</span>
              )}
            </div>

            <div className="flex items-center gap-1">
              <Label className="text-xs text-muted-foreground">top_k</Label>
              <Input
                type="number"
                min={1}
                className="h-8 w-16"
                placeholder={String(serverModels?.kb_defaults.vector_search_top_k ?? 3)}
                value={topK ?? ""}
                onChange={(e) =>
                  setTopK(e.target.value === "" ? undefined : Number(e.target.value))
                }
              />
            </div>
            <div className="flex items-center gap-1">
              <Label className="text-xs text-muted-foreground">阈值</Label>
              <Input
                type="number"
                min={0}
                max={1}
                step={0.05}
                className="h-8 w-16"
                placeholder={String(serverModels?.kb_defaults.score_threshold ?? 0.4)}
                value={scoreThreshold ?? ""}
                onChange={(e) =>
                  setScoreThreshold(
                    e.target.value === "" ? undefined : Number(e.target.value),
                  )
                }
              />
            </div>
          </div>
        </div>
        <div className="flex-1 overflow-y-auto py-4">
          <MessageList
            messages={rag.messages}
            onFeedback={(key, score) => rag.patchMessage(key, { feedbackScore: score })}
          />
        </div>
        <Composer
          allowImage={false}
          value={draft}
          onChange={(v) => prefs.setDraft(draftKey, v)}
          onSend={(text) => {
            prefs.clearDraft(draftKey);
            if (!param) {
              toast.error(mode === "temp_kb" ? "请先上传文件" : "请先选择检索来源");
              return;
            }
            void rag.send(text, {
              mode,
              param,
              model: modelValue,
              temperature: prefs.temperature,
              topK,
              scoreThreshold,
            });
          }}
          onStop={rag.stop}
          streaming={rag.streaming}
        />
      </div>
    </div>
  );
}
