import type { LatexSnapshot } from "@/api/supervision";
import { AlertPanel } from "@/components/supervision/AlertPanel";
import {
  MAIN_STATUS_LABEL,
  countsLine,
  findingLine,
  labelOf,
  needsMainFileChoice,
  snapshotProblemLines,
} from "@/components/supervision/latex/latexModel";
import { Badge } from "@/components/ui/badge";

function when(value?: string | null): string {
  return value ? value.slice(0, 16).replace("T", " ") : "未知";
}

/**
 * 一份稿件的分析快照。
 *
 * 显示口径全部来自后端：``compiled`` 恒 false（系统绝不调用 LaTeX 编译器），
 * 主文件没判定下来时必须提示用户另选，危险命令、未读取与只读了一部分的文件
 * 都要原样列出来，不能只给一个"分析完成"。
 */
export function LatexSnapshotCard({ snapshot }: { snapshot: LatexSnapshot }) {
  const problems = snapshotProblemLines(snapshot);
  return (
    <div className="space-y-3 rounded-md border p-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium">{snapshot.code}</span>
        <Badge variant="secondary">
          {snapshot.attachment_name}（{snapshot.attachment_code} v{snapshot.attachment_version}）
        </Badge>
        <Badge variant={needsMainFileChoice(snapshot) ? "outline" : "secondary"}>
          主文件：{snapshot.main_file || "未判定"} · {labelOf(MAIN_STATUS_LABEL, snapshot.main_status)}
        </Badge>
        <span className="text-xs text-muted-foreground">
          解析于 {when(snapshot.create_time)} · 解析器 {snapshot.parser_version} · 检查器{" "}
          {snapshot.checker_version} · 归档安全 {snapshot.zip_safety_version}
        </span>
      </div>

      <div className="text-sm">{countsLine(snapshot.counts) || "没有可统计的结构"}</div>
      <div className="text-xs text-muted-foreground">
        归档内 {snapshot.file_count} 个文件 · 共 {snapshot.total_bytes} 字节 · 正文字数{" "}
        {snapshot.word_count}
      </div>

      <AlertPanel
        title="只做了静态解析，没有编译也没有运行"
        description="系统不会调用 LaTeX 编译器，也不会执行稿件里的任何命令；下面的章节、图表与引用数都是从源码结构里读出来的。"
      />

      {needsMainFileChoice(snapshot) && (
        <AlertPanel
          tone="warning"
          title="主文件没有唯一判定，差异结果不可信"
          description={
            `当前判定：${labelOf(MAIN_STATUS_LABEL, snapshot.main_status)}。` +
            "请在上面的输入框里按归档内相对路径填写主文件（例如 main.tex）后重新分析。"
          }
        />
      )}

      {problems.length > 0 && (
        <AlertPanel
          tone={snapshot.dangerous_findings.length > 0 ? "error" : "warning"}
          title={
            snapshot.dangerous_findings.length > 0
              ? "稿件里有被判定为危险的命令"
              : "有文件没有完整读取"
          }
          description={problems.join("\n")}
        />
      )}

      {snapshot.unreferenced_tex.length > 0 && (
        <AlertPanel
          title="有 .tex 文件没被主文件引用"
          description={`${snapshot.unreferenced_tex.join("、")} 不在主文件的引用链里，因此它们的内容没有计入上面的统计。`}
        />
      )}

      {snapshot.outline.length > 0 && (
        <details className="text-sm">
          <summary className="cursor-pointer text-muted-foreground">
            章节大纲（{snapshot.outline.length} 节）
          </summary>
          <ul className="mt-1 list-disc space-y-0.5 pl-5">
            {snapshot.outline.map((title) => (
              <li key={title}>{title}</li>
            ))}
          </ul>
        </details>
      )}

      {snapshot.findings.length > 0 && (
        <details className="text-sm">
          <summary className="cursor-pointer text-muted-foreground">
            静态检查发现（{snapshot.findings.length} 条）
          </summary>
          <ul className="mt-1 list-disc space-y-1 pl-5">
            {snapshot.findings.map((finding, index) => (
              <li key={`${finding.kind}-${finding.path}-${finding.line}-${index}`}>
                {findingLine(finding)}
                {finding.snippet ? (
                  <pre className="mt-1 overflow-x-auto whitespace-pre-wrap rounded bg-muted/40 p-1 text-xs">
                    {finding.snippet}
                  </pre>
                ) : null}
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}
