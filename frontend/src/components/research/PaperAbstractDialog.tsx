import { useState } from "react";
import { Copy, FileText } from "lucide-react";
import { toast } from "sonner";

import type { ResearchPaper } from "@/api/research";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { FULLTEXT_STATE_LABEL } from "@/components/research/labels";
import { prettifyAbstract } from "@/components/research/paperText";

/** 只有 http(s) 链接才允许在新标签页打开（本地路径或空值会让浏览器跳到奇怪的地方） */
export function isWebUrl(url: string | undefined | null): boolean {
  return /^https?:\/\//i.test((url ?? "").trim());
}

/**
 * 单篇论文的摘要视图。
 *
 * 列表里不再平铺摘要（一行卡片塞两段英文没法读），改为按需打开这个对话框：
 * 元数据成表、摘要单独一块可滚动区域，并顺手清洗 arXiv 摘要里的 LaTeX 残留。
 */
export function PaperAbstractDialog({
  open,
  onClose,
  paper,
}: {
  open: boolean;
  onClose: () => void;
  paper: ResearchPaper;
}) {
  const [copied, setCopied] = useState(false);
  const abstract = prettifyAbstract(paper.abstract);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(abstract);
      setCopied(true);
      toast.success("摘要已复制");
    } catch {
      // 非安全上下文（http 访问）下 clipboard 不可用，不能假装复制成功
      toast.error("浏览器不允许复制，请手动选中复制");
    }
  };

  return (
    <Dialog open={open} onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle className="pr-6 text-left text-base leading-snug">
            {paper.title}
          </DialogTitle>
        </DialogHeader>

        <div className="flex flex-wrap items-center gap-2">
          <Badge variant="outline">{paper.source || "未知来源"}</Badge>
          {paper.year ? <Badge variant="outline">{paper.year}</Badge> : null}
          <Badge variant="outline" title="该论文在本项目内的原文可得性">
            {FULLTEXT_STATE_LABEL[paper.fulltext_state] ?? paper.fulltext_state}
            {paper.unit_count > 0 ? ` · ${paper.unit_count} 单元` : ""}
          </Badge>
          {paper.screening_status === "included" ? <Badge>已纳入</Badge> : null}
          {paper.screening_status === "excluded" ? (
            <Badge variant="destructive">已排除</Badge>
          ) : null}
        </div>

        <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1.5 text-xs">
          <dt className="text-muted-foreground">作者</dt>
          <dd className="break-words">{paper.authors || "—"}</dd>
          {paper.arxiv_id ? (
            <>
              <dt className="text-muted-foreground">arXiv</dt>
              <dd>{paper.arxiv_id}</dd>
            </>
          ) : null}
          {paper.doi ? (
            <>
              <dt className="text-muted-foreground">DOI</dt>
              <dd className="break-all">{paper.doi}</dd>
            </>
          ) : null}
        </dl>

        <div className="max-h-[45vh] overflow-y-auto rounded-md border bg-muted/40 p-4">
          {abstract ? (
            <p className="whitespace-pre-wrap text-sm leading-7">{abstract}</p>
          ) : (
            <p className="text-sm text-muted-foreground">
              该论文没有摘要。可点「取原文」解析全文，或到原文页面查看。
            </p>
          )}
        </div>

        <div className="flex flex-wrap items-center gap-2">
          {isWebUrl(paper.url) ? (
            <Button asChild size="sm" variant="outline">
              <a href={paper.url} target="_blank" rel="noopener noreferrer">
                <FileText className="mr-1 h-3 w-3" />
                在新标签页打开原文
              </a>
            </Button>
          ) : null}
          {abstract ? (
            <Button size="sm" variant="ghost" onClick={copy}>
              <Copy className="mr-1 h-3 w-3" />
              {copied ? "已复制" : "复制摘要"}
            </Button>
          ) : null}
        </div>
      </DialogContent>
    </Dialog>
  );
}
