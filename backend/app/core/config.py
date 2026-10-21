from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Literal

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    groq_model: str = "openai/gpt-oss-120b"
    
    environment: str = "development"
    cors_origins: list[str] = []
    allowed_hosts: list[str] = ["*"]
    cookie_secure: bool = False
    cookie_samesite: Literal["lax", "strict", "none"] = "lax"

    database_url: str
    redis_url: str

    github_token: str | None = None
    github_api_base: str = "https://api.github.com"
    github_timeout_seconds: float = 15.0
    max_tree_paths: int = 3000
    max_listing_entries: int = 60
    max_agent_steps: int = 20
    max_file_chars: int = 8000          # size of one chunk
    max_search_file_chars: int = 400_000 # how much of a file search_in_file scans
    max_search_matches: int = 15
    max_tokens_per_investigation: int = 200_000
    llm_max_output_tokens: int = 2048
    report_max_output_tokens: int = 4000
    groq_api_key: str | None = None
    groq_model: str = "llama-3.1-8b-instant"

    cloudinary_cloud_name: str
    cloudinary_api_key: str
    cloudinary_api_secret: str

    smtp_host: str = "smtp-relay.brevo.com"
    smtp_port: int = 587
    smtp_user: str
    smtp_pass: str
    sender_email: str

    access_token_secret: str
    refresh_token_secret: str
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 7


settings = Settings()