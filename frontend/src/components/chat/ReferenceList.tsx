import { useState } from "react";
import { FileText, Download } from "lucide-react";

import { apiUrl } from "@/lib/config";
import { downloadBlob } from "@/lib/download";
import { getAuthToken } from "@/stores/auth";
import type { ReferenceData } from "@/lib/sse/types";

/**
 * RAG 引用列表。url 指向需要 Bearer 的 download_doc 接口，
 * 直接 <a href> 会 401，必须 fetch 成 blob 再下载。
 */
export function ReferenceList({ references }: { references: ReferenceData[] }) {
  const [downloading, setDownloading] = useState<number | null>(null);

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
      <ul className="space-y-1">
        {references.map((ref, i) => (
          <li key={i} className="flex items-center gap-2">
            <FileText className="h-3.5 w-3.5 shrink-0" />
            <span className="min-w-0 flex-1 truncate">
              [{ref.index ?? i + 1}] {ref.file_name ?? ref.knowledge_base ?? "文档"}
            </span>
            {typeof ref.score === "number" && (
              <span className="text-muted-foreground">
                {ref.score.toFixed(2)}
              </span>
            )}
            {ref.url && (
              <button
                type="button"
                className="ml-auto text-muted-foreground hover:text-foreground"
                onClick={() => handleDownload(ref, i)}
                title="下载原文"
              >
                <Download
                  className={`h-3.5 w-3.5 ${downloading === i ? "animate-pulse" : ""}`}
                />
              </button>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}
