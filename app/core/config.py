import os
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    app_name: str = "Jusr AI API"
    database_url: str = "postgresql+psycopg://jusr:jusr@db:5432/jusr_ai"
    secret_key: str = "change-this-development-secret"
    access_token_expire_minutes: int = 30
    cors_origins: list[str] = ["http://localhost:5173"]
    demo_mode: bool = True
    # Patient secure links (Part 2)
    patient_link_expiry_days: int = 7
    patient_link_max_attempts: int = 3
    patient_link_verify_birth_year: bool = True
    patient_link_base_url: str = "http://localhost:5173"
    patient_session_minutes: int = 30
    # Outbound messages (R-19): total attempts including the first, and minutes to wait before each retry
    message_max_attempts: int = 3
    message_retry_minutes: list[int] = [5, 15]
    # Urgent specialty routing: minutes the routed clinician has to accept, and minutes the coordinator-escalated
    # case waits before it is routed to a backup clinician. A routing request may override both per case.
    urgent_acceptance_minutes: int = 30
    urgent_escalation_minutes: int = 30
    # Background scheduler for message retries and routing deadlines. With several API workers, enable it on one
    # instance only, or disable it and run `python -m app.worker` as its own process.
    scheduler_enabled: bool = True
    scheduler_interval_seconds: int = 60
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = Settings()
