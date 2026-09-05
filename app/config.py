from __future__ import annotations

from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="PAYROLL_", extra="ignore")

    bot_token: str
    database_url: str = "sqlite:///payroll.db"
    owner_ids: Annotated[set[int], NoDecode] = Field(default_factory=set)
    timezone: str = "Europe/Amsterdam"
    default_currency: str = "RUB"

    @field_validator("owner_ids", mode="before")
    @classmethod
    def parse_owner_ids(cls, value):
        if isinstance(value, str):
            value = {int(item.strip()) for item in value.split(",") if item.strip()}
        if any(item <= 0 for item in value):
            raise ValueError("owner IDs must be positive")
        return value

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("unknown IANA timezone") from exc
        return value

    @field_validator("default_currency")
    @classmethod
    def valid_currency(cls, value: str) -> str:
        value = value.strip().upper()
        if len(value) != 3 or not value.isascii() or not value.isalpha():
            raise ValueError("currency must be a 3-letter ISO code")
        return value
