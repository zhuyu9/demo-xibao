from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", case_sensitive=False, extra="ignore"
    )
    DASHSCOPE_API_KEY: str = ""
    DASHSCOPE_WS_URL: str = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"
    DASHSCOPE_MODEL: str = "qwen3-asr-flash-realtime"
    DASHSCOPE_FORMAT: str = "pcm"
    DASHSCOPE_SAMPLE_RATE: int = 16000
    DASHSCOPE_VOCABULARY_ID: str | None = Field(default=None)


@lru_cache
def get_settings() -> Settings:
    return Settings()
