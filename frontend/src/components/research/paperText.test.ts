/**
 * 摘要清洗的单元测试（纯函数，全部离线）。
 *
 * 钉住现场：候选论文摘要里出现 ``targets\text{unicode}(x2014)$a malicious``
 * 这种 LaTeX 残留。清洗只允许去掉排版噪声，不能改写内容。
 */
import { describe, expect, it } from "vitest";

import { dehyphenate, prettifyAbstract } from "@/components/research/paperText";

describe("prettifyAbstract", () => {
  it("还原 arXiv 里被拆坏的 unicode 转义，并去掉数学定界符", () => {
    expect(prettifyAbstract("targets \\text{unicode}(x2014)$a malicious")).toBe(
      "targets — a malicious",
    );
  });

  it("定界符换成空格而不是直接删掉，公式不会和相邻单词粘在一起", () => {
    expect(prettifyAbstract("the value $x_i$ of the metric")).toBe(
      "the value x_i of the metric",
    );
  });

  it("常见数学命令换成 Unicode 字符", () => {
    expect(prettifyAbstract("where $\\alpha \\approx 0.5$ and $d \\to \\infty$")).toBe(
      "where α ≈ 0.5 and d → ∞",
    );
  });

  it("排版命令只去掉命令本身，保留文字（含嵌套）", () => {
    expect(prettifyAbstract("\\textbf{Results} show \\emph{a} \\text{gap}")).toBe(
      "Results show a gap",
    );
    expect(prettifyAbstract("\\textbf{\\text{nested}}")).toBe("nested");
  });

  it("还原 LaTeX 转义的普通符号", () => {
    expect(prettifyAbstract("50\\% of 3 \\& 4")).toBe("50% of 3 & 4");
  });

  it("折叠换行与多余空白", () => {
    expect(prettifyAbstract("  多行\n摘要   含空白  ")).toBe("多行 摘要 含空白");
  });

  it("不认识的命令原样保留，绝不猜测语义", () => {
    expect(prettifyAbstract("\\cite{smith2024} 保持原样")).toBe(
      "\\cite{smith2024} 保持原样",
    );
  });

  it("代理区码位不还原，避免造出乱码字符", () => {
    expect(prettifyAbstract("unicode(xd800)")).toBe("unicode(xd800)");
  });

  it("空摘要返回空串（调用方据此显示空态）", () => {
    expect(prettifyAbstract("")).toBe("");
    expect(prettifyAbstract(undefined as unknown as string)).toBe("");
  });
});

describe("dehyphenate", () => {
  it("合并 PDF 换行造成的断词", () => {
    expect(dehyphenate("ISAC has recently garnered be- yond for- mulated aus- tralian")).toBe(
      "ISAC has recently garnered beyond formulated australian",
    );
  });

  it("没被换行拆开的连字符不动", () => {
    expect(dehyphenate("well-known low-altitude 6G")).toBe("well-known low-altitude 6G");
  });

  it("无法判断原词是否自带连字符：一律直接接上（已知取舍）", () => {
    // 没有词典可依，"well- known" 只能接成 "wellknown"；
    // 相对 PDF 里成片的 "be- yond/for- mulated"，这个代价小得多
    expect(dehyphenate("a well- known system")).toBe("a wellknown system");
  });

  it("带空格的破折号不会被误合并", () => {
    expect(dehyphenate("Index Terms—Integrated, cell - the network")).toBe(
      "Index Terms—Integrated, cell - the network",
    );
  });

  it("空文本返回空串", () => {
    expect(dehyphenate("")).toBe("");
  });
});
