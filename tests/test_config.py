"""Settings come from the environment and the .env file; real environment variables win."""
import pytest

from app.config import DEFAULT_BASE_URL, Settings

NAMES = ["URJA_EMAIL", "URJA_PASSWORD", "URJA_BASE_URL", "CATALOG_TTL_SECONDS", "SESSION_MARGIN_SECONDS", "REQUEST_TIMEOUT_SECONDS"]


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for name in NAMES:
        monkeypatch.delenv(name, raising=False)


def write_env(tmp_path, text):
    path = tmp_path / ".env"
    path.write_text(text, encoding="utf-8")
    return path


def test_reads_values_from_the_env_file(tmp_path):
    env_file = write_env(tmp_path, "URJA_EMAIL=a@b.c\nURJA_PASSWORD=pw\nCATALOG_TTL_SECONDS=42\n")
    settings = Settings.from_env(env_file)
    assert (settings.email, settings.password) == ("a@b.c", "pw")
    assert settings.catalog_ttl_seconds == 42
    assert settings.base_url == DEFAULT_BASE_URL
    assert settings.session_margin_seconds == 300


def test_real_environment_variables_win_over_the_file(tmp_path, monkeypatch):
    env_file = write_env(tmp_path, "URJA_EMAIL=file@b.c\nURJA_PASSWORD=file-pw\n")
    monkeypatch.setenv("URJA_EMAIL", "env@b.c")
    settings = Settings.from_env(env_file)
    assert settings.email == "env@b.c"
    assert settings.password == "file-pw"


def test_reading_the_file_does_not_leak_into_the_process_environment(tmp_path):
    import os

    Settings.from_env(write_env(tmp_path, "URJA_EMAIL=a@b.c\nURJA_PASSWORD=pw\n"))
    assert "URJA_EMAIL" not in os.environ


def test_missing_credentials_give_a_clear_error(tmp_path):
    with pytest.raises(RuntimeError, match="URJA_EMAIL and URJA_PASSWORD"):
        Settings.from_env(tmp_path / "no-such-file")


def test_base_url_loses_a_trailing_slash(tmp_path):
    env_file = write_env(tmp_path, "URJA_EMAIL=a@b.c\nURJA_PASSWORD=pw\nURJA_BASE_URL=https://example.test/\n")
    assert Settings.from_env(env_file).base_url == "https://example.test"


@pytest.mark.parametrize("bad", ["abc", "0", "-5"])
def test_numbers_must_be_positive_numbers(tmp_path, bad):
    env_file = write_env(tmp_path, f"URJA_EMAIL=a@b.c\nURJA_PASSWORD=pw\nCATALOG_TTL_SECONDS={bad}\n")
    with pytest.raises(RuntimeError, match="CATALOG_TTL_SECONDS"):
        Settings.from_env(env_file)


def test_blank_optional_values_fall_back_to_defaults(tmp_path):
    env_file = write_env(tmp_path, "URJA_EMAIL=a@b.c\nURJA_PASSWORD=pw\nURJA_BASE_URL=\nCATALOG_TTL_SECONDS=\n")
    settings = Settings.from_env(env_file)
    assert settings.base_url == DEFAULT_BASE_URL
    assert settings.catalog_ttl_seconds == 600
