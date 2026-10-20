"""Settings from CT_* environment variables. Only composition roots call get_settings()."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
WEB_DIR = ROOT / "web"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CT_", env_file=".env", extra="ignore")

    mode: Literal["demo", "pilot"] = "demo"
    db: Path = ROOT / "var" / "chaarteeke.db"
    var_dir: Path = ROOT / "var"
    telephony: Literal["sim", "ari", "ari,sim"] = "sim"
    ari_url: str = "http://127.0.0.1:8088"
    ari_user: str = ""
    ari_password: str = ""
    ari_app: str = "chaar"
    ari_endpoint: str = "Quectel/sim7600-0/{number}"
    asr: Literal["indicconformer", "off"] = "indicconformer"
    asr_model_dir: Path | None = None  # CT_ASR_MODEL_DIR; the speech adapter documents its default
    secret_key: str = ""
    phone_key: str = ""
    host: str = "127.0.0.1"
    port: int = 8080
    public_url: str = ""
    ct_number: str = ""  # CT_NUMBER: the Chaar Teeke missed-call number clinics tell patients to ring
    # CT_CLINIC_LINES: JSON {"<modem name>": "<clinic id>"}, one line per clinic, so a missed call is shown only
    # to the clinic whose line it came in on. Empty: one shared number, and every clinic sees every call.
    clinic_lines: dict[str, str] = Field(default_factory=dict)
    # CT_BACKUP_DIR and CT_BACKUP_KEY: a nightly encrypted copy of the database. Both or neither; the backup
    # key is its own key (a Fernet key), not the phone key. Unset: no backup is made, and the admin page says so.
    backup_dir: Path | None = None
    backup_key: str = ""
    basemap: Literal["none", "openfreemap"] = "none"
    local_tz: str = "Asia/Kolkata"
    scheduler: bool = True
    data_dir: Path = Field(default=DATA_DIR)
    web_dir: Path = Field(default=WEB_DIR)

    @property
    def is_demo(self) -> bool:
        return self.mode == "demo"

    @property
    def sim_enabled(self) -> bool:
        return "sim" in self.telephony.split(",")

    @property
    def ari_enabled(self) -> bool:
        return "ari" in self.telephony.split(",")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
