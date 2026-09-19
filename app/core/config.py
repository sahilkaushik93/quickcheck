from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    app_name: str = "UnDQ Transcript DQ Engine"
    app_version: str = "0.1.0"
    api_prefix: str = "/api/v1/undq/transcript"


settings = Settings()
