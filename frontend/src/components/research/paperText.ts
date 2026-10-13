/**
 * 摘要纯文本清洗：只去掉排版噪声，不增删内容。
 *
 * arXiv 的 summary 就是论文 LaTeX 源码里的原文，直接显示会露出
 * ``\text{unicode}(x2014)$a`` 这类残留（现场：候选论文摘要里出现
 * ``targets\text{unicode}(x2014)$a malicious``）。这里只做可验证的保守
 * 替换：去掉排版命令的花括号、去掉数学定界符、把常见数学命令换成
 * Unicode 字符，其余一律原样保留 —— 摘要是要拿去判断论文相关性的证据，
 * 不能被改写成另一段话。
 */

/** 只负责排版的花括号命令：去掉命令本身，保留花括号里的文字 */
const TEXT_WRAPPER_RE =
  /\\(?:text|textrm|textbf|textmd|textit|textsf|texttt|emph|mathbf|mathrm|mathit|mathsf|boldsymbol)\s*\{([^{}]*)\}/g;
/** 被拆坏的 Unicode 转义：``\text{unicode}(x2014)`` 去掉排版命令后剩 ``unicode(x2014)`` */
const UNICODE_RE = /unicode\s*\(\s*x([0-9a-fA-F]{4})\s*\)/g;
/** LaTeX 里被转义的普通符号 */
const ESCAPED_RE = /\\([&#%_$])/g;
/** 数学定界符：换成空格（直接删掉会把公式和相邻单词粘在一起），公式内容留着可读 */
const MATH_DELIM_RE = /\$|\\\(|\\\)|\\\[|\\\]/g;
/** 常见数学命令 → Unicode（只列论文摘要里真正会出现的，其余不动） */
const SYMBOLS: Record<string, string> = {
  alpha: "α",
  beta: "β",
  gamma: "γ",
  delta: "δ",
  epsilon: "ε",
  theta: "θ",
  kappa: "κ",
  lambda: "λ",
  mu: "μ",
  pi: "π",
  rho: "ρ",
  sigma: "σ",
  tau: "τ",
  phi: "φ",
  chi: "χ",
  psi: "ψ",
  omega: "ω",
  Delta: "Δ",
  Sigma: "Σ",
  Omega: "Ω",
  times: "×",
  cdot: "·",
  div: "÷",
  pm: "±",
  approx: "≈",
  neq: "≠",
  ne: "≠",
  leq: "≤",
  le: "≤",
  geq: "≥",
  ge: "≥",
  to: "→",
  rightarrow: "→",
  leftarrow: "←",
  infty: "∞",
  circ: "∘",
  degree: "°",
};
const SYMBOL_RE = new RegExp(
  `\\\\(?:${Object.keys(SYMBOLS).join("|")})(?![a-zA-Z])`,
  "g",
);
const WS_RE = /\s+/g;

/** PDF 换行处的断词：字母紧跟连字符再接空格，后面是小写字母 */
const HYPHEN_BREAK_RE = /([A-Za-z])- +([a-z])/g;

/**
 * 合并 PDF 换行造成的断词（``be- yond`` → ``beyond``），只用于显示。
 *
 * 库里存的单元文本保持原样：证据核对是拿库内原文逐字比对的，不能为了
 * 界面好看去改存储内容。要求连字符紧跟字母（前面不能有空格），
 * 这样带空格的破折号（``cell - the``）不会被误合并。
 */
export function dehyphenate(text: string): string {
  if (!text) return "";
  return text.replace(HYPHEN_BREAK_RE, "$1$2");
}

export function prettifyAbstract(text: string): string {
  if (!text) return "";
  let out = text;

  // 排版命令可能嵌套（\textbf{\emph{...}}）：反复替换到不再变化为止。
  // 不用 while (RE.test(...))，因为带 g 的 test 会推进 lastIndex。
  let previous = "";
  while (previous !== out) {
    previous = out;
    out = out.replace(TEXT_WRAPPER_RE, "$1");
  }

  out = out.replace(UNICODE_RE, (whole, hex: string) => {
    const code = Number.parseInt(hex, 16);
    // 代理区码位单独出现必然渲染成乱码，宁可留着原样让用户看得出问题
    if (Number.isNaN(code) || (code >= 0xd800 && code <= 0xdfff)) return whole;
    return String.fromCodePoint(code);
  });
  out = out.replace(ESCAPED_RE, "$1");
  out = out.replace(SYMBOL_RE, (whole) => SYMBOLS[whole.slice(1)] ?? whole);
  out = out.replace(MATH_DELIM_RE, " ");

  return out.replace(WS_RE, " ").trim();
}
