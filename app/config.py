"""Application configuration — loaded from environment variables with defaults."""
import os

# ── Authentication ────────────────────────────────────────────────────────────
AUTH_USERNAME: str = os.environ.get("AUTH_USERNAME", "stu")
AUTH_PASSWORD: str = os.environ.get("AUTH_PASSWORD", "changeme")

# ── Session ───────────────────────────────────────────────────────────────────
SESSION_SECRET: str = os.environ.get(
    "SESSION_SECRET", "campground-monitor-secret-key-change-in-prod"
)
SESSION_MAX_AGE: int = int(os.environ.get("SESSION_MAX_AGE", str(7 * 24 * 60 * 60)))

# ── Email / SMTP ──────────────────────────────────────────────────────────────
SMTP_SERVER: str = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT: int = int(os.environ.get("SMTP_PORT", "587"))
EMAIL_FROM: str = os.environ.get("EMAIL_FROM", "")
EMAIL_PASSWORD: str = os.environ.get("EMAIL_PASSWORD", "")

# ── Data directories ──────────────────────────────────────────────────────────
DATA_DIR: str = os.environ.get("DATA_DIR", "./data")
RIDB_DIR: str = os.environ.get("RIDB_DIR", "./RIDBFullExport_V1_CSV")

# ── Recreation.gov API ────────────────────────────────────────────────────────
RECGOV_BASE_URL: str = (
    "https://www.recreation.gov/api/camps/availability/campground"
)

RECGOV_HEADERS: dict = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.recreation.gov/",
}
