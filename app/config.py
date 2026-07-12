"""Runtime configuration for cb-crm-inbound.

All values come from environment variables (Coolify env store in
production). No secrets are ever hardcoded here.
"""
from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Database (CB Neon, shared pooled endpoint — search_path is pinned
    # in db.py per the cb-crm hard-won lesson) ---
    crm_database_url: str = Field(alias="CRM_DATABASE_URL")

    # --- Resend (Aguia account) ---
    resend_api_key: str = Field(alias="RESEND_API_KEY")
    # Signing secret for the email.received webhook (whsec_...). Empty means
    # the inbound webhook endpoint rejects everything (fail closed).
    resend_webhook_secret: str = Field(default="", alias="RESEND_WEBHOOK_SECRET")
    # Signing secret for the outbound events webhook (bounce/complaint).
    resend_events_webhook_secret: str = Field(
        default="", alias="RESEND_EVENTS_WEBHOOK_SECRET"
    )

    # --- Telegram (fleet bot, same token notify.sh uses) ---
    telegram_bot_token: str = Field(default="", alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str = Field(default="5138814159", alias="TELEGRAM_CHAT_ID")

    # --- Reply routing ---
    # Domain whose mail we accept on the SMTP listener.
    inbound_domain: str = Field(
        default="reply.culturabuilder.com", alias="INBOUND_DOMAIN"
    )
    # Copy of every real reply is forwarded here so Bruno keeps seeing
    # replies in his inbox.
    forward_replies_to: str = Field(
        default="bruno@culturabuilder.com", alias="FORWARD_REPLIES_TO"
    )
    # Must be on the Resend-verified sending domain (@brunopessoa.com).
    forward_from: str = Field(
        default="Cultura Builder CRM <crm@brunopessoa.com>", alias="FORWARD_FROM"
    )

    # --- SMTP listener ---
    smtp_listen_host: str = Field(default="0.0.0.0", alias="SMTP_LISTEN_HOST")
    smtp_listen_port: int = Field(default=2525, alias="SMTP_LISTEN_PORT")
    smtp_max_message_bytes: int = Field(
        default=10 * 1024 * 1024, alias="SMTP_MAX_MESSAGE_BYTES"
    )

    # --- IMAP poller (fallback variant; disabled until credentials exist).
    # Activates automatically when IMAP_HOST and IMAP_PASSWORD are both set.
    imap_host: str = Field(default="", alias="IMAP_HOST")
    imap_port: int = Field(default=993, alias="IMAP_PORT")
    imap_user: str = Field(default="", alias="IMAP_USER")
    imap_password: str = Field(default="", alias="IMAP_PASSWORD")
    imap_folder: str = Field(default="INBOX", alias="IMAP_FOLDER")
    imap_poll_seconds: int = Field(default=120, alias="IMAP_POLL_SECONDS")

    @property
    def imap_enabled(self) -> bool:
        return bool(self.imap_host and self.imap_password)


settings = Settings()  # type: ignore[call-arg]
