"""Settings of the push gateway (docs/push.md §2).

Secrets come as files, like the onboarding service's: under Compose /run/secrets/apns_key (the
.p8 key from Apple, PEM) and /run/secrets/fcm_service_account (the Firebase service account,
JSON). A missing or empty one turns that platform off: the gateway still starts and answers
503 ``not_configured`` for it.
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

SECRETS_DIR = os.environ.get("SECRETS_DIR", "/run/secrets")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    # the address belfries sign in their NIP-98 ``u`` tag (…/v1/send is appended)
    public_url: str = Field(default="https://push.belfry.sh", alias="PUBLIC_URL")
    apns_key_id: str = Field(default="", alias="APNS_KEY_ID")
    apns_team_id: str = Field(default="", alias="APNS_TEAM_ID")
    apns_topic: str = Field(default="sh.belfry.app", alias="APNS_TOPIC")
    # the files; APNS_KEY / FCM_SERVICE_ACCOUNT (the contents) win when set (tests)
    apns_key_path: str = Field(default=f"{SECRETS_DIR}/apns_key", alias="APNS_KEY_PATH")
    fcm_service_account_path: str = Field(
        default=f"{SECRETS_DIR}/fcm_service_account", alias="FCM_SERVICE_ACCOUNT_PATH"
    )
    apns_key: str = Field(default="", alias="APNS_KEY")
    fcm_service_account: str = Field(default="", alias="FCM_SERVICE_ACCOUNT")
    # limits (per hour)
    per_token_hour: int = Field(default=120, alias="LIMIT_PER_TOKEN")
    per_belfry_hour: int = Field(default=10_000, alias="LIMIT_PER_BELFRY")
    log_level: str = Field(default="info", alias="LOG_LEVEL")

    def apns_pem(self) -> str:
        return self.apns_key.strip() or _read(self.apns_key_path)

    def fcm_json(self) -> str:
        return self.fcm_service_account.strip() or _read(self.fcm_service_account_path)

    @property
    def send_url(self) -> str:
        return self.public_url.rstrip("/") + "/v1/send"


def _read(path: str) -> str:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ""
