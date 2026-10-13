import { useState } from "react";
import { ChevronDown, ChevronRight, Download, FileText } from "lucide-react";

import { apiUrl } from "@/lib/config";
import { downloadBlob } from "@/lib/download";
import { getAuthToken } from "@/stores/auth";
import type { ReferenceData } from "@/lib/sse/types";

/**
 * RAG 引用列表。
 *
 * 收起时每条只显示一行出处（文件、所属知识库、页码、相关度）；点「展开」
 * 才显示服务端带来的命中片段（content）—— 同一个文件被切成多段时
 * （[2] 与 [3] 同名），只有片段能说明各自命中了什么。
 *
 * url 指向需要 Bearer 的 download_doc 接口，直接 <a href> 会 401，
 * 必须 fetch 成 blob 再下载。
 */
export function ReferenceList({ references }: { references: ReferenceData[] }) {
  const [downloading, setDownloading] = useState<number | null>(null);
  const [expanded, setExpanded] = useState<Record<number, boolean>>({});

  async function handleDownload(ref: ReferenceData, index: number) {
    if (!ref.url) return;
    setDownloading(index);
    try {
      // ref.url 形如 /knowledge_base/download_doc?...（同源相对路径）
      const res = await fetch(apiUrl(ref.url), {
        headers: { Authorization: `Bearer ${getAuthToken() ?? ""}` },
        credentials: "omit",
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const blob = await res.blob();
      downloadBlob(blob, ref.file_name || `reference-${index + 1}`);
    } catch {
      // 下载失败时退回新窗口打开（可能提示 401）
      window.open(apiUrl(ref.url), "_blank", "noopener");
    } finally {
      setDownloading(null);
    }
  }

  if (references.length === 0) return null;

  return (
    <div className="mt-2 rounded-md border bg-muted/30 p-2 text-xs">
      <div className="mb-1 font-medium text-muted-foreground">
        参考资料（{references.length}）
      </div>
      <ul className="space-y-2">
        {references.map((ref, i) => {
          const content = (ref.content ?? "").trim();
          const open = expanded[i] === true;
          const file = ref.file_name ?? ref.knowledge_base ?? "文档";
          const details = [
            ref.knowledge_base ? `知识库 ${ref.knowledge_base}` : "",
            typeof ref.page === "number" ? `第 ${ref.page} 页` : "",
            typeof ref.score === "number" ? `相关度 ${ref.score.toFixed(3)}` : "",
          ].filter(Boolean);

          return (
            <li key={i} className="rounded-md border bg-background/60">
              <div className="flex items-center gap-2 p-1.5">
                <FileText className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                <span className="min-w-0 break-all">
                  [{ref.index ?? i + 1}] {file}
                </span>
                {details.length > 0 && (
                  <span className="shrink-0 whitespace-nowrap text-muted-foreground">
                    {details.join(" · ")}
                  </span>
                )}
                <div className="ml-auto flex shrink-0 items-center gap-1">
                  <button
                    type="button"
                    className="flex items-center gap-0.5 text-muted-foreground hover:text-foreground"
                    onClick={() => setExpanded((v) => ({ ...v, [i]: !open }))}
                    title={open ? "收起片段" : "展开查看命中片段"}
                  >
                    {open ? (
                      <ChevronDown className="h-3.5 w-3.5" />
                    ) : (
                      <ChevronRight className="h-3.5 w-3.5" />
                    )}
                    {open ? "收起" : "展开"}
                  </button>
                  {ref.url && (
                    <button
                      type="button"
                      className="text-muted-foreground hover:text-foreground"
                      onClick={() => handleDownload(ref, i)}
                      title="下载原文"
                    >
                      <Download
                        className={`h-3.5 w-3.5 ${
                          downloading === i ? "animate-pulse" : ""
                        }`}
                      />
                    </button>
                  )}
                </div>
              </div>
              {/* 片段只在点「展开」后才出现，收起时只留一行出处 */}
              {open &&
                (content ? (
                  <p className="whitespace-pre-wrap border-t px-2 py-1.5 leading-relaxed text-foreground/80">
                    {content}
                  </p>
                ) : (
                  <p className="border-t px-2 py-1.5 text-muted-foreground">
                    该引用没有返回片段内容（旧会话或联网搜索结果）。
                  </p>
                ))}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
