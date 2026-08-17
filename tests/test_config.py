"""Tests for app.config — verifies env var loading and defaults."""
import importlib


def test_config_loads_from_env(monkeypatch):
    """Config values are read from environment variables when set."""
    monkeypatch.setenv("AUTH_USERNAME", "testuser")
    monkeypatch.setenv("AUTH_PASSWORD", "testpass")
    monkeypatch.setenv("SESSION_SECRET", "testsecret")
    monkeypatch.setenv("SESSION_MAX_AGE", "3600")
    monkeypatch.setenv("SMTP_SERVER", "smtp.example.com")
    monkeypatch.setenv("SMTP_PORT", "465")
    monkeypatch.setenv("EMAIL_FROM", "from@example.com")
    monkeypatch.setenv("EMAIL_PASSWORD", "emailpass")
    monkeypatch.setenv("DATA_DIR", "/tmp/mydata")
    monkeypatch.setenv("RIDB_DIR", "/tmp/myridb")

    # Force reimport so env vars are picked up
    import app.config as cfg
    importlib.reload(cfg)

    assert cfg.AUTH_USERNAME == "testuser"
    assert cfg.AUTH_PASSWORD == "testpass"
    assert cfg.SESSION_SECRET == "testsecret"
    assert cfg.SESSION_MAX_AGE == 3600
    assert cfg.SMTP_SERVER == "smtp.example.com"
    assert cfg.SMTP_PORT == 465
    assert cfg.EMAIL_FROM == "from@example.com"
    assert cfg.EMAIL_PASSWORD == "emailpass"
    assert cfg.DATA_DIR == "/tmp/mydata"
    assert cfg.RIDB_DIR == "/tmp/myridb"


def test_config_has_defaults(monkeypatch):
    """Config falls back to sensible defaults when env vars are absent."""
    # Clear any vars that might be set
    for var in [
        "AUTH_USERNAME", "AUTH_PASSWORD", "SESSION_SECRET", "SESSION_MAX_AGE",
        "SMTP_SERVER", "SMTP_PORT", "EMAIL_FROM", "EMAIL_PASSWORD",
        "DATA_DIR", "RIDB_DIR",
    ]:
        monkeypatch.delenv(var, raising=False)

    import app.config as cfg
    importlib.reload(cfg)

    assert cfg.AUTH_USERNAME == "stu"
    assert cfg.AUTH_PASSWORD == "changeme"
    assert cfg.SESSION_SECRET == "campground-monitor-secret-key-change-in-prod"
    assert cfg.SESSION_MAX_AGE == 7 * 24 * 60 * 60  # 7 days in seconds
    assert cfg.SMTP_SERVER == "smtp.gmail.com"
    assert cfg.SMTP_PORT == 587
    assert cfg.EMAIL_FROM == ""
    assert cfg.EMAIL_PASSWORD == ""
    assert cfg.DATA_DIR == "./data"
    assert cfg.RIDB_DIR == "./RIDBFullExport_V1_CSV"
    # Static constants
    assert cfg.RECGOV_BASE_URL == "https://www.recreation.gov/api/camps/availability/campground"
    assert "User-Agent" in cfg.RECGOV_HEADERS
    assert "Accept" in cfg.RECGOV_HEADERS
    assert "Referer" in cfg.RECGOV_HEADERS
