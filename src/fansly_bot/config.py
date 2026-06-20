# src/fansly_bot/config.py
"""Chargement et validation typee de la configuration.

Sources fusionnees :
  1. defauts Pydantic
  2. YAML (FANSLY_CONFIG_FILE ou ./config.yaml)
  3. variables d'environnement (.env), prefixe FANSLY_ — secrets uniquement
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Paths(BaseModel):
    media_folder: Path
    caption_folder: Path
    published_subfolder: str = "_published"
    state_db: Path
    logs_dir: Path
    artifacts_dir: Path

    @property
    def published_folder(self) -> Path:
        return self.media_folder / self.published_subfolder


class Browser(BaseModel):
    user_data_dir: Path
    headless: bool = True
    viewport_width: int = 1280
    viewport_height: int = 800
    locale: str = "en-US"
    timezone: str = "Europe/Paris"
    user_agent: str
    navigation_timeout_ms: int = 30_000
    default_action_timeout_ms: int = 15_000


class Auth(BaseModel):
    base_url: str
    home_path: str = "/home"
    interactive_login_timeout_s: int = 600
    session_check_interval_minutes: int = 60


class PauseProfile(BaseModel):
    median_s: float
    sigma: float
    min_s: float
    max_s: float


class TypingProfile(BaseModel):
    per_char_ms_median: float
    per_char_ms_sigma: float
    per_char_ms_min: float
    per_char_ms_max: float
    micro_pause_probability: float = Field(ge=0.0, le=1.0)
    micro_pause_ms_min: float
    micro_pause_ms_max: float


class Humanizer(BaseModel):
    short_pause: PauseProfile
    long_pause: PauseProfile
    typing: TypingProfile


class TimeWindow(BaseModel):
    start: str
    end: str

    @classmethod
    def from_list(cls, value: list[str]) -> "TimeWindow":
        if len(value) != 2:
            raise ValueError("daily_window_local doit etre une liste [start, end]")
        return cls(start=value[0], end=value[1])

    def as_hm(self) -> tuple[tuple[int, int], tuple[int, int]]:
        sh, sm = map(int, self.start.split(":"))
        eh, em = map(int, self.end.split(":"))
        return (sh, sm), (eh, em)


class Publishing(BaseModel):
    enabled: bool = True
    daily_window_local: TimeWindow
    interval_minutes_median: float
    interval_minutes_sigma: float
    interval_minutes_min: float
    interval_minutes_max: float
    media_extensions: list[str]

    @field_validator("daily_window_local", mode="before")
    @classmethod
    def _coerce_window(cls, v):
        if isinstance(v, list):
            return TimeWindow.from_list(v)
        return v

    @field_validator("media_extensions")
    @classmethod
    def _lowercase_ext(cls, v: list[str]) -> list[str]:
        return [e.lower() for e in v]


class Purge(BaseModel):
    enabled: bool = True
    age_threshold_days: int = Field(ge=0)
    keywords: list[str]
    keyword_match_mode: Literal["any", "all"] = "any"
    daily_window_local: TimeWindow
    max_deletions_per_run: int = Field(ge=1)
    scroll_safety_cap: int = Field(ge=1)
    dry_run: bool = False
    profile_path: str = ""

    @field_validator("daily_window_local", mode="before")
    @classmethod
    def _coerce_window(cls, v):
        if isinstance(v, list):
            return TimeWindow.from_list(v)
        return v

    @field_validator("keywords")
    @classmethod
    def _clean_keywords(cls, v: list[str]) -> list[str]:
        # Liste vide autorisee : permet une purge purement temporelle (fenetre
        # de dates sans filtre sur le contenu de la legende). Le service
        # purger traite explicitement ce cas en mettant keyword_hit=True.
        return [k.strip() for k in v if k and k.strip()]


class Retry(BaseModel):
    attempts: int = Field(ge=1, default=3)
    initial_wait_s: float = 1.5
    max_wait_s: float = 30.0


class Logging(BaseModel):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    json_to_file: bool = True
    pretty_to_console: bool = True
    rotate_max_bytes: int = 10 * 1024 * 1024
    rotate_backup_count: int = 5


class Dev(BaseModel):
    dummy_job_enabled: bool = False
    dummy_job_interval_s: int = 10


class Secrets(BaseSettings):
    """Lit les secrets et l'identite utilisateur depuis l'environnement / .env.

    Les credentials (username/password) sont SecretStr — jamais loggues en
    clair. Le profile_slug n'est pas un secret au sens strict (l'URL du
    profil est publique) mais il identifie l'utilisateur du bot et ne doit
    pas etre versionne dans le repo. On le place ici pour qu il soit lu
    depuis l environnement comme les credentials.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="FANSLY_",
        extra="ignore",
    )

    username: SecretStr
    password: SecretStr
    # Slug du profil Fansly (la partie publique de ton URL fansly.com/<slug>).
    # Si non defini, le purger tentera de fallback sur `purge.profile_path` du
    # YAML, puis sur le username (peu fiable car le username est un email).
    profile_slug: str | None = None


class Settings(BaseModel):
    paths: Paths
    browser: Browser
    auth: Auth
    humanizer: Humanizer
    publishing: Publishing
    purge: Purge
    retry: Retry
    logging: Logging
    dev: Dev = Field(default_factory=Dev)
    secrets: Secrets

    def ensure_runtime_dirs(self) -> None:
        for p in (
            self.paths.media_folder,
            self.paths.published_folder,
            self.paths.caption_folder,
            self.paths.logs_dir,
            self.paths.artifacts_dir,
            self.browser.user_data_dir,
            self.paths.state_db.parent,
        ):
            p.mkdir(parents=True, exist_ok=True)


def _resolve_config_path() -> Path:
    import os

    env_path = os.environ.get("FANSLY_CONFIG_FILE")
    if env_path:
        return Path(env_path).expanduser().resolve()
    return Path.cwd() / "config.yaml"


def load_settings() -> Settings:
    config_path = _resolve_config_path()
    if not config_path.is_file():
        raise FileNotFoundError(
            f"Fichier de configuration introuvable : {config_path}. "
            "Defini FANSLY_CONFIG_FILE ou place config.yaml dans le CWD."
        )

    with config_path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    secrets = Secrets()  # type: ignore[call-arg]
    return Settings(**raw, secrets=secrets)
