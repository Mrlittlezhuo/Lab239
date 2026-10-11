"""从流式 JSON 输出里增量取出正文字段。

Agent 任务的规划/判断要求模型"只输出一个 JSON 对象"，用户要看的正文
（``summary`` / ``direct_answer`` / ``answer``）就在这个对象里。模型是边
生成边返回的，但 JSON 没收完之前不能整体 ``json.loads``，所以要在字符到达
的同时把"正在写的那个字符串值"解码出来推给前端，作为生成中的预览。

这里只做旁路预览，绝不参与正确性判断：运行收敛时前端用的仍然是解析并校验
过的结构化结果（``plan_ready`` / ``run_completed``），预览写错或模型输出
不合规都不影响最终答案。

实现是一个逐字符状态机：

- 只在字符串外计数容器层级，因此值里出现的 ``{`` ``}`` ``"answer":`` 之类
  不会骗到它；
- 只取**顶层**对象的字段（规划/判断的目标字段都在顶层），嵌套对象里的同名
  字段一律忽略；
- 每个目标字段最多产出一次（模型重复输出或重试不会重复刷屏）；
- JSON 转义（``\\n`` ``\\"`` ``\\\\`` ``\\uXXXX`` 与代理对）在流上就地解码，
  半个转义序列暂存到下一个分片再继续；
- 文本按**分片**推送（不是逐字符），一个模型分片最多产生一次预览事件。
"""

from __future__ import annotations

from typing import Callable, Mapping, Optional

_HEX_DIGITS = "0123456789abcdefABCDEF"
_SIMPLE_ESCAPES = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "b": "\b",
    "f": "\f",
    '"': '"',
    "\\": "\\",
    "/": "/",
}

# 预览回调：(阶段标签, 文本增量)
PushFn = Callable[[str, str], None]


class AnswerStreamExtractor:
    """把模型输出的分片喂进来，按 ``key_phases`` 把目标字段文本推出去。

    ``key_phases`` 形如 ``{"answer": "answer"}``：键是 JSON 顶层字段名，值
    是推给 ``push`` 的阶段标签（前端据此决定预览显示在哪）。
    """

    def __init__(self, key_phases: Mapping[str, str], push: PushFn):
        self._phases = dict(key_phases)
        self._push = push
        # 结构解析
        self._stack: list[str] = []  # 容器栈，只装 "{" 与 "["
        self._expect_key = False  # 顶层对象里当前字符串位于键位置
        self._in_string = False
        self._string_is_key = False
        self._escape = False
        self._string_buf: list[str] = []  # 非捕获字符串的累积（用于认键）
        # 捕获
        self._field: Optional[str] = None  # 已认出目标键，等待/正在取它的值
        self._capturing = False
        self._cap_state = ""  # "" | "escape" | "unicode"
        self._hex: list[str] = []
        self._high_surrogate: Optional[int] = None
        self._seen: set[str] = set()
        self._buf: list[str] = []  # 当前分片内已解码的文本
        self._closed = False

    # -- 对外接口 ---------------------------------------------------------

    def feed(self, chunk: str) -> None:
        """吃下一个分片；本片确定的文本经 ``push`` 一次推出。"""
        if self._closed or not chunk:
            return
        for char in chunk:
            self._step(char)
            if self._closed:
                break
        self._flush()

    def close(self) -> None:
        """输出结束：冲掉残留文本，之后的 ``feed`` 一律忽略。"""
        if not self._closed:
            self._flush()
        self._closed = True

    # -- 状态机 -----------------------------------------------------------

    def _step(self, char: str) -> None:
        if self._capturing:
            self._capture_char(char)
        elif self._in_string:
            self._string_char(char)
        else:
            self._structure_char(char)

    def _structure_char(self, char: str) -> None:
        if char == '"':
            if self._field is not None:
                # 目标字段的值开始：进入捕获，之后的字符直接解码输出
                self._capturing = True
                self._cap_state = ""
                self._hex = []
            else:
                self._in_string = True
                self._escape = False
                self._string_buf = []
                # 只有顶层对象的键才可能命中目标字段
                self._string_is_key = (
                    self._stack == ["{"] and self._expect_key
                )
            return
        if char == "{":
            self._stack.append("{")
            self._expect_key = True
            return
        if char == "[":
            self._stack.append("[")
            return
        if char in "}]":
            if self._stack:
                self._stack.pop()
            return
        if char == ",":
            if self._stack[-1:] == ["{"]:
                self._expect_key = True
            return
        if char == ":":
            self._expect_key = False
            return
        if not char.isspace():
            # 目标字段的值不是字符串（null/数字/对象/数组）：放弃该字段
            self._field = None

    def _string_char(self, char: str) -> None:
        """非目标字符串：只为认键而累积，同时正确处理转义引号。"""
        if self._escape:
            self._escape = False
            self._string_buf.append(char)
            return
        if char == "\\":
            self._escape = True
            return
        if char == '"':
            self._in_string = False
            key = "".join(self._string_buf)
            if self._string_is_key and key in self._phases and key not in self._seen:
                self._field = key
            self._string_is_key = False
            return
        self._string_buf.append(char)

    def _capture_char(self, char: str) -> None:
        """目标字段的字符串值：边解码边输出。"""
        if self._cap_state == "unicode":
            self._hex.append(char)
            if len(self._hex) == 4:
                self._flush_hex()
            return
        if self._cap_state == "escape":
            self._cap_state = ""
            if char == "u":
                self._hex = []
                self._cap_state = "unicode"
            else:
                self._emit(_SIMPLE_ESCAPES.get(char, char))
            return
        if char == "\\":
            self._cap_state = "escape"
            return
        if char == '"':
            self._finish_capture()
            return
        self._emit(char)

    def _flush_hex(self) -> None:
        digits = "".join(self._hex)
        self._hex = []
        self._cap_state = ""
        if any(d not in _HEX_DIGITS for d in digits):
            # 不是合法 \uXXXX：原样给出，绝不吞字
            self._emit("\\u" + digits)
            return
        code = int(digits, 16)
        if 0xD800 <= code <= 0xDBFF:
            self._high_surrogate = code  # 代理对高位，等下一个 \uXXXX
            return
        if self._high_surrogate is not None:
            code = (
                0x10000
                + ((self._high_surrogate - 0xD800) << 10)
                + (code - 0xDC00)
            )
            self._high_surrogate = None
        try:
            self._emit(chr(code))
        except ValueError:
            self._emit("�")

    def _finish_capture(self) -> None:
        self._capturing = False
        self._in_string = False
        self._cap_state = ""
        self._hex = []
        if self._high_surrogate is not None:
            self._emit("�")  # 截断的代理对：给个替换符，不吞
            self._high_surrogate = None
        self._flush()  # 字段结束：先把它的文本推出去，再清状态
        if self._field is not None:
            self._seen.add(self._field)
        self._field = None
        if self._seen >= self._phases.keys():
            self._closed = True

    def _emit(self, text: str) -> None:
        if text:
            self._buf.append(text)

    def _flush(self) -> None:
        """按分片推送：一个模型分片最多产生一次预览事件。"""
        if not self._buf:
            return
        text = "".join(self._buf)
        self._buf = []
        if self._field is not None:
            self._push(self._phases[self._field], text)
