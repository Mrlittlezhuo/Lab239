"""`PlatformConfig.api_key` 的 `${VAR}` 环境变量引用。

`model_settings.yaml` 随仓库版本控制，密钥不能明文写进去；这里钉住引用语法、
变量未设置时的行为（保留引用 + 启动自检点名，而不是让服务起不来）、普通字面量
不受影响，以及取值只发生在加载时（磁盘上仍然是引用）。
"""
from chatchat.settings import (
    ApiModelSettings,
    PlatformConfig,
    describe_unresolved_secret_refs,
)
from chatchat.pydantic_settings_file import SettingsConfigDict


def test_api_key_expands_env_var_ref(monkeypatch):
    monkeypatch.setenv("STRATA_API_KEY", "secret-from-env")
    cfg = PlatformConfig(api_key="${STRATA_API_KEY}")
    assert cfg.api_key == "secret-from-env"
    assert describe_unresolved_secret_refs([cfg]) == []


def test_missing_env_var_keeps_the_reference_and_is_reported(monkeypatch):
    monkeypatch.delenv("NOT_SET_ANYWHERE", raising=False)
    cfg = PlatformConfig(platform_name="probe", api_key="${NOT_SET_ANYWHERE}")
    # 不炸：没跑该服务的机器照样能启动
    assert cfg.api_key == "${NOT_SET_ANYWHERE}"
    problems = describe_unresolved_secret_refs([cfg])
    assert len(problems) == 1
    assert "NOT_SET_ANYWHERE" in problems[0] and "probe" in problems[0]
    # 点名不能把值带出去（这里值就是引用本身，换成真密钥后同理）
    assert "secret" not in problems[0]


def test_literal_api_key_is_left_alone():
    assert PlatformConfig(api_key="EMPTY").api_key == "EMPTY"
    assert PlatformConfig(api_key="sk-proj-abc").api_key == "sk-proj-abc"


def test_env_var_ref_can_be_part_of_a_longer_value(monkeypatch):
    monkeypatch.setenv("KEY_SUFFIX", "tail")
    assert PlatformConfig(api_key="sk-${KEY_SUFFIX}").api_key == "sk-tail"


def test_startup_warns_but_does_not_echo_the_value(capsys, monkeypatch):
    from chatchat import startup
    from chatchat.settings import Settings

    monkeypatch.setenv("STRATA_API_KEY", "do-not-print")
    # 已解析的值不会出现在提示里；再造一个未解析的平台让自检有话说
    resolved = PlatformConfig(platform_name="resolved", api_key="${STRATA_API_KEY}")
    unresolved = PlatformConfig(platform_name="strata", api_key="${MISSING_VAR}")
    monkeypatch.setattr(
        Settings.model_settings, "MODEL_PLATFORMS", [resolved, unresolved]
    )

    assert startup.print_model_secret_warning() is True
    out = capsys.readouterr().out
    assert "strata" in out and "MISSING_VAR" in out
    assert "do-not-print" not in out


def test_startup_stays_quiet_when_every_key_resolves(capsys, monkeypatch):
    from chatchat import startup
    from chatchat.settings import Settings

    monkeypatch.setenv("STRATA_API_KEY", "fine")
    monkeypatch.setattr(
        Settings.model_settings,
        "MODEL_PLATFORMS",
        [PlatformConfig(platform_name="strata", api_key="${STRATA_API_KEY}")],
    )
    assert startup.print_model_secret_warning() is False
    assert capsys.readouterr().out == ""


def test_secret_is_resolved_at_load_time_not_on_disk(tmp_path, monkeypatch):
    """护栏：一旦有人把取到的密钥写回配置文件，明文密钥就会进入版本控制。"""
    monkeypatch.setenv("STRATA_API_KEY", "do-not-persist")
    source = tmp_path / "model_settings.yaml"
    source.write_text(
        "DEFAULT_LLM_MODEL: probe-llm\n"
        "MODEL_PLATFORMS:\n"
        "  - platform_name: probe\n"
        "    platform_type: openai\n"
        "    api_base_url: http://127.0.0.1:8081/v1\n"
        "    api_key: ${STRATA_API_KEY}\n"
        "    llm_models:\n"
        "      - probe-llm\n",
        encoding="utf-8",
    )

    class _ProbeSettings(ApiModelSettings):
        model_config = SettingsConfigDict(yaml_file=source)

    loaded = _ProbeSettings()
    assert loaded.MODEL_PLATFORMS[0].api_key == "do-not-persist"

    on_disk = source.read_text(encoding="utf-8")
    assert "${STRATA_API_KEY}" in on_disk
    assert "do-not-persist" not in on_disk
