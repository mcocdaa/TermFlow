"""M7a optional STT configuration tests (spec §4.5, §7.2).

Freezes the ``TERMFLOW_STT_*`` settings surface: all defaults keep STT
disabled (the Null provider 503 path), enabling requires an explicit
``stt_url``, the URL must be a credential/query/fragment-free HTTP(S)
origin, the token is a ``SecretStr`` with empty-string normalization (the
compose ``${STT_API_KEY:-}`` case), and the provider timeout must stay
strictly below the 60s B-side wall clock.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError
from termflow_control_plane.config import Settings

ADMIN_TOKEN = "admin-token-that-is-long-enough-for-tests"

DEFAULT_MODEL = "Systran/faster-distil-whisper-small.en"


class TestSttDefaults:
    def test_stt_defaults_keep_stt_disabled(self) -> None:
        settings = Settings(admin_token=ADMIN_TOKEN)

        assert settings.stt_enabled is False
        assert settings.stt_url is None
        assert settings.stt_token is None
        assert settings.stt_model == DEFAULT_MODEL
        assert settings.stt_timeout_seconds == 55.0


class TestSttCombinationValidation:
    def test_enabling_stt_requires_an_explicit_url(self) -> None:
        with pytest.raises(ValidationError, match="stt_url"):
            Settings(admin_token=ADMIN_TOKEN, stt_enabled=True)

    def test_enabled_with_url_and_token_is_valid(self) -> None:
        settings = Settings(
            admin_token=ADMIN_TOKEN,
            stt_enabled=True,
            stt_url="http://stt-speaches:8000",
            stt_token="deployment-secret",
        )
        assert settings.stt_enabled is True
        assert settings.stt_url == "http://stt-speaches:8000"
        assert settings.stt_token.get_secret_value() == "deployment-secret"


class TestSttUrlValidation:
    @pytest.mark.parametrize(
        "url",
        [
            "http://user@example.com:8000",  # userinfo
            "http://user:pass@example.com",  # password
            "https://example.com/?token=1",  # query
            "https://example.com/#fragment",  # fragment
            "ftp://example.com",  # wrong scheme
            "not a url",  # no scheme/host
            "",  # empty (compose unset must not silently pass)
        ],
    )
    def test_stt_url_rejects_credentials_query_fragment_and_non_http(
        self, url: str
    ) -> None:
        with pytest.raises(ValidationError):
            Settings(admin_token=ADMIN_TOKEN, stt_url=url)

    def test_stt_url_accepts_a_plain_http_origin(self) -> None:
        settings = Settings(admin_token=ADMIN_TOKEN, stt_url="http://stt-speaches:8000")
        assert settings.stt_url == "http://stt-speaches:8000"

    def test_stt_url_accepts_https_origin(self) -> None:
        settings = Settings(admin_token=ADMIN_TOKEN, stt_url="https://stt.example.com")
        assert settings.stt_url == "https://stt.example.com"


class TestSttTokenValidation:
    def test_stt_token_is_a_secret_str_that_never_serializes(self) -> None:
        settings = Settings(admin_token=ADMIN_TOKEN, stt_token="super-secret-token")
        assert isinstance(settings.stt_token, SecretStr)
        assert settings.stt_token.get_secret_value() == "super-secret-token"
        assert "super-secret-token" not in repr(settings)
        assert "super-secret-token" not in str(settings.model_dump())

    def test_empty_stt_token_normalizes_to_none(self) -> None:
        # compose passes ${STT_API_KEY:-}, which expands to "" when the STT
        # profile is not enabled; the empty string must not be mistaken for
        # a configured credential.
        settings = Settings(admin_token=ADMIN_TOKEN, stt_token="")
        assert settings.stt_token is None


class TestSttModelValidation:
    def test_stt_model_defaults_to_the_pinned_english_distil_model(self) -> None:
        settings = Settings(admin_token=ADMIN_TOKEN)
        assert settings.stt_model == DEFAULT_MODEL

    def test_stt_model_must_be_non_empty(self) -> None:
        with pytest.raises(ValidationError):
            Settings(admin_token=ADMIN_TOKEN, stt_model="")

    def test_stt_model_is_overridable(self) -> None:
        settings = Settings(admin_token=ADMIN_TOKEN, stt_model="openai/whisper-small")
        assert settings.stt_model == "openai/whisper-small"


class TestSttTimeoutValidation:
    @pytest.mark.parametrize("timeout", [0.5, 60.0, 120.0])
    def test_stt_timeout_out_of_bounds_rejected(self, timeout: float) -> None:
        # Must stay strictly below the 60s B-side wall clock
        # (api/transcription.py TRANSCRIPTION_TIMEOUT_SECONDS).
        with pytest.raises(ValidationError):
            Settings(admin_token=ADMIN_TOKEN, stt_timeout_seconds=timeout)

    def test_stt_timeout_accepts_the_strict_bounds(self) -> None:
        for timeout in (1.0, 59.0):
            settings = Settings(admin_token=ADMIN_TOKEN, stt_timeout_seconds=timeout)
            assert settings.stt_timeout_seconds == timeout


class TestSttEnvironmentNames:
    def test_stt_environment_names_are_stable(self, monkeypatch) -> None:
        monkeypatch.setenv("TERMFLOW_ADMIN_TOKEN", ADMIN_TOKEN)
        monkeypatch.setenv("TERMFLOW_STT_ENABLED", "true")
        monkeypatch.setenv("TERMFLOW_STT_URL", "http://stt-speaches:8000")
        monkeypatch.setenv("TERMFLOW_STT_TOKEN", "env-token")
        monkeypatch.setenv("TERMFLOW_STT_MODEL", "custom/whisper-model")
        monkeypatch.setenv("TERMFLOW_STT_TIMEOUT_SECONDS", "42")

        settings = Settings(_env_file=None)

        assert settings.stt_enabled is True
        assert settings.stt_url == "http://stt-speaches:8000"
        assert settings.stt_token is not None
        assert settings.stt_token.get_secret_value() == "env-token"
        assert settings.stt_model == "custom/whisper-model"
        assert settings.stt_timeout_seconds == 42.0
