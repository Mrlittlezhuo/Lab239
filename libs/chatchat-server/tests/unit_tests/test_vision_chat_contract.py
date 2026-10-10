"""视觉输入契约离线测试（前端重构：图片解析路径）。

结论性事实（钉住，避免前端再次接错接口 / 后端再次丢字段）：

- `/v1/chat/completions` 是代理路由：`body.model_dump(exclude_unset=True)`
  的结果直接交给 openai SDK，因此多模态 `content` 必须在 dump 之后仍然是
  可 JSON 序列化的普通结构，图片片段才会真正到达模型平台；
- 用 openai SDK 的 `ChatCompletionMessageParam` 标注 messages 会破坏这一点
  （content 声明为 `Union[str, Iterable[...]]`，校验后是惰性
  ValidatorIterator，`model_dump()` 变成 SerializationIterator，
  `mode="json"` 下甚至变成 `[]`），所以 `OpenAIChatInput.messages` 用宽松
  的 dict 结构；
- `/chat/chat/completions` 不能收多模态 `content`：它把最后一条消息的
  `content` 当字符串用于会话标题、`add_message(query=...)` 落库和 prompt
  模板，传数组会在写库时失败。

测试不联网、不调模型，只验证请求模型的解析与透传形状。
"""
import json

from chatchat.server.api_server.api_schemas import OpenAIChatInput

MULTIMODAL_CONTENT = [
    {"type": "text", "text": "这张图里有什么"},
    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
]


def _dump(body: OpenAIChatInput) -> dict:
    """代理路由实际交给 SDK 的参数形状。"""
    return body.model_dump(exclude_unset=True)


def test_multimodal_content_survives_dump():
    body = OpenAIChatInput(
        model="any-vision-model",
        messages=[{"role": "user", "content": MULTIMODAL_CONTENT}],
        stream=False,
    )
    dumped = _dump(body)
    # 必须可 JSON 序列化（openai SDK 之后要 json.dumps）
    json.dumps(dumped)
    assert dumped["messages"][0]["content"] == MULTIMODAL_CONTENT


def test_multimodal_content_survives_json_mode_dump():
    body = OpenAIChatInput(
        model="any-vision-model",
        messages=[{"role": "user", "content": MULTIMODAL_CONTENT}],
        stream=False,
    )
    dumped = body.model_dump(mode="json", exclude_unset=True)
    assert dumped["messages"][0]["content"] == MULTIMODAL_CONTENT


def test_plain_text_content_stays_string():
    body = OpenAIChatInput(
        model="any-model",
        messages=[{"role": "user", "content": "普通文本"}],
        stream=False,
    )
    assert _dump(body)["messages"][0]["content"] == "普通文本"


def test_history_messages_keep_order_and_roles():
    body = OpenAIChatInput(
        model="any-model",
        messages=[
            {"role": "system", "content": "s"},
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "q2"},
        ],
        stream=False,
    )
    messages = _dump(body)["messages"]
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]
    # /chat 路由依赖最后一条消息（str 场景）
    assert messages[-1]["content"] == "q2"


def test_multimodal_content_is_not_a_string_for_chat_route():
    """提醒：/chat/chat/completions 的 query 取 content 当字符串用，
    多模态轮必须走 /v1，不能走 /chat。"""
    body = OpenAIChatInput(
        model="any-vision-model",
        messages=[{"role": "user", "content": MULTIMODAL_CONTENT}],
        stream=False,
    )
    content = _dump(body)["messages"][-1]["content"]
    assert not isinstance(content, str)
    # /chat 路由把 content 当字符串用（会话标题、落库 query、prompt 模板）；
    # 数组在这里只会得到一串 Python 源码，模型拿不到图片。
    assert str(content)[:50].startswith("[{")
