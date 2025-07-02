from functools import lru_cache
from typing import List, Set

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", case_sensitive=True
    )

    # --- Project Info ---
    PROJECT_NAME: str = "AI Image Stylizer"
    VERSION: str = "1.0.0"

    # --- Server ---
    API_V1_STR: str = "/api/v1"
    ALLOWED_HOSTS: List[str] = ["*"]

    # --- Redis for ARQ ---
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379
    REDIS_DB: int = 0

    # --- Task Queue ---
    MAX_CONCURRENT_JOBS: int = 2
    JOB_TIMEOUT: int = 600  # 10 minutes

    # --- Model & Generation ---
    RESULTS_FOLDER: str = "generated_images"
    JOB_RESULT_TTL: int = 900  # 15 minutes
    MAX_QUEUE_SIZE: int = 10
    MAX_UPLOAD_MB: int = 10
    ALLOWED_EXTENSIONS: Set[str] = {"png", "jpg", "jpeg", "webp"}

    # --- Default Generation Parameters ---
    DEFAULT_WIDTH: int = 1024
    DEFAULT_HEIGHT: int = 1024
    DEFAULT_STEPS: int = 28
    DEFAULT_GUIDANCE_SCALE: float = 2.5
    DEFAULT_TRUE_CFG_SCALE: float = 1.5


@lru_cache
def get_settings() -> Settings:
    return Settings()
