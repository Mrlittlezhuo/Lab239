"""`ChatPlatformAI` 必须把真密钥交给 openai 客户端。

`get_from_dict_or_env` 会对取到的值做 `str()`，而 SecretStr 的 `str()` 是掩码
`**********`，密钥当场丢失。ollama/xinference 收 `EMPTY`、不校验 key，所以这个
bug 一直没被发现；接上真正鉴权的平台（本机 Strata 网关）才暴露成 401。
"""
import pytest

from langchain_chatchat.chat_models import ChatPlatformAI

API_BASE = "http://127.0.0.1:9/v1"  # 构造客户端不发请求，端口不可达无所谓


def test_api_key_reaches_the_openai_client():
    model = ChatPlatformAI(
        model="probe-llm", api_key="real-key-not-a-mask", api_base=API_BASE
    )
    assert model.chatchat_api_key.get_secret_value() == "real-key-not-a-mask"
    assert model.client._client.api_key == "real-key-not-a-mask"


def test_secret_str_mask_is_never_used_as_the_key():
    model = ChatPlatformAI(
        model="probe-llm", api_key="real-key-not-a-mask", api_base=API_BASE
    )
    sent = model.client._client.api_key
    assert "**********" not in sent
    assert isinstance(sent, str)


def test_env_var_still_fills_the_key(monkeypatch):
    monkeypatch.setenv("CHATCHAT_API_KEY", "from-env")
    model = ChatPlatformAI(model="probe-llm", api_base=API_BASE)
    assert model.client._client.api_key == "from-env"


def test_missing_key_without_env_still_raises(monkeypatch):
    monkeypatch.delenv("CHATCHAT_API_KEY", raising=False)
    with pytest.raises(ValueError):
        ChatPlatformAI(model="probe-llm", api_base=API_BASE)
