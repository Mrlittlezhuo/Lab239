"""空模型名必须回落到服务端默认模型（离线）。

现场（2026-10-12 19:42 日志）：知识库对话页没有选模型时，前端发的是 `model: ""`。
接口签名写的是 `model: str = Body(get_default_llm())`，但客户端显式传空串时
FastAPI 不会用默认值，于是：

  ERROR get_ChatOpenAI:265 - failed to create ChatOpenAI for model: .
  ERROR kb_chat:387 - error in knowledge chat: Expected a Runnable, callable
        or dict.Instead got an unsupported type: <class 'NoneType'>

`get_ChatOpenAI` 失败时不抛异常、只返回 None，调用方随后 `chat_prompt | None`
才崩成那句 NoneType —— 界面上完全看不出是模型名没传对。
"""
import pytest

from chatchat.server import utils


@pytest.fixture
def _default_model(monkeypatch):
    monkeypatch.setattr(utils, "get_default_llm", lambda: "test-default-llm")


@pytest.mark.parametrize("given", [None, "", "   ", "\t"])
def test_empty_model_name_falls_back_to_the_default(given, _default_model):
    assert utils.resolve_llm_model_name(given) == "test-default-llm"


def test_explicit_model_name_is_kept_and_trimmed(_default_model):
    assert utils.resolve_llm_model_name("  glm4-chat  ") == "glm4-chat"


def test_get_chat_openai_never_sees_an_empty_model_name(monkeypatch):
    """归一化放在 get_ChatOpenAI 里：任何调用方传空串都不会拿到 None。"""
    seen = {}

    class _FakeChatOpenAI:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    monkeypatch.setattr(utils, "get_default_llm", lambda: "test-default-llm")
    monkeypatch.setattr(utils, "get_model_info", lambda *a, **kw: {})
    monkeypatch.setattr(utils, "ChatOpenAI", _FakeChatOpenAI)

    llm = utils.get_ChatOpenAI(model_name="")

    assert llm is not None
    assert seen["model_name"] == "test-default-llm"


def test_get_chat_openai_keeps_an_explicit_model_name(monkeypatch):
    seen = {}

    class _FakeChatOpenAI:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    monkeypatch.setattr(utils, "get_default_llm", lambda: "test-default-llm")
    monkeypatch.setattr(utils, "get_model_info", lambda *a, **kw: {})
    monkeypatch.setattr(utils, "ChatOpenAI", _FakeChatOpenAI)

    utils.get_ChatOpenAI(model_name="qwen:7b")

    assert seen["model_name"] == "qwen:7b"
