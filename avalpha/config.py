"""Configuration: config.toml for settings, environment for secrets."""

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Config:
    db_path: Path
    digest_dir: Path
    email_recipient: str
    email_sender: str
    subreddits: list[str] = field(default_factory=list)
    model_confirm: str = "claude-haiku-4-5"
    model_scorer: str = "claude-haiku-4-5"
    model_narrative: str = "claude-sonnet-4-6"
    web_host: str = "127.0.0.1"
    web_port: int = 8000
    web_fund_name: str = "The Silo Fund"

    @property
    def anthropic_api_key(self) -> str:
        return _require_env("ANTHROPIC_API_KEY")

    @property
    def gmail_app_password(self) -> str:
        return _require_env("GMAIL_APP_PASSWORD")

    @property
    def reddit_client_id(self) -> str:
        return _require_env("REDDIT_CLIENT_ID")

    @property
    def reddit_client_secret(self) -> str:
        return _require_env("REDDIT_CLIENT_SECRET")

    @property
    def finnhub_api_key(self) -> str:
        return _require_env("FINNHUB_API_KEY")

    @property
    def fred_api_key(self) -> str:
        return _require_env("FRED_API_KEY")

    @property
    def contact_email(self) -> str:
        return _require_env("AVALPHA_CONTACT_EMAIL")

    @property
    def edgar_user_agent(self) -> str:
        from avalpha import __version__

        return f"avalpha/{__version__} {self.contact_email}"


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"environment variable {name} is not set")
    return value


def _config_path() -> Path:
    if env := os.environ.get("AVALPHA_CONFIG"):
        return Path(env).expanduser()
    return Path(__file__).resolve().parent.parent / "config.toml"


def _load_dotenv(config_path: Path) -> None:
    """Load a `.env` file sitting next to config.toml into os.environ.

    Stdlib only; existing environment variables always win, so systemd's
    EnvironmentFile and real shell exports override the file. This is a dev
    convenience — in production the secrets come from EnvironmentFile, not here.
    """
    env_path = config_path.parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def load_config(path: Path | None = None) -> Config:
    path = path or _config_path()
    if not path.exists():
        raise RuntimeError(
            f"config file not found at {path} — copy config.toml.example to "
            "config.toml or set AVALPHA_CONFIG"
        )
    _load_dotenv(path)
    with open(path, "rb") as f:
        raw = tomllib.load(f)

    storage = raw.get("storage", {})
    email = raw.get("email", {})
    reddit = raw.get("reddit", {})
    models = raw.get("models", {})
    web = raw.get("web", {})

    return Config(
        db_path=Path(storage.get("db_path", "~/avalpha-data/avalpha.db")).expanduser(),
        digest_dir=Path(storage.get("digest_dir", "~/avalpha-data/digests")).expanduser(),
        email_recipient=email.get("recipient", ""),
        email_sender=email.get("sender", ""),
        subreddits=list(reddit.get("subreddits", [])),
        model_confirm=models.get("confirm", "claude-haiku-4-5"),
        model_scorer=models.get("scorer", "claude-haiku-4-5"),
        model_narrative=models.get("narrative", "claude-sonnet-4-6"),
        web_host=web.get("host", "127.0.0.1"),
        web_port=int(web.get("port", 8000)),
        web_fund_name=web.get("fund_name", "The Silo Fund"),
    )
