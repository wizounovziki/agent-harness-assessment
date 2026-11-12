"""Server configuration, kept outside the model's context."""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from src.schemas import ExecutionLimits

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Settings:
    database_path: Path = Path("runtime/harness.sqlite3")
    trace_dir: Path = Path("runtime/traces")
    dataset_path: Path = PROJECT_ROOT / "data"
    llm_mode: str = "scripted"
    llm_model: str = ""
    llm_api_key: str = field(default="", repr=False)
    llm_base_url: str | None = None
    api_token: str = field(default="local-demo-token", repr=False)
    limits: ExecutionLimits = field(default_factory=ExecutionLimits)

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv(PROJECT_ROOT / ".env", override=False)
        values = {}
        for name, info in ExecutionLimits.model_fields.items():
            raw = os.getenv(name.upper())
            if raw is not None:
                values[name] = float(raw) if info.annotation is float else int(raw)
        mode = os.getenv("LLM_MODE", "scripted")
        if mode not in {"scripted", "real"}:
            raise ValueError("LLM_MODE must be scripted or real")
        return cls(
            database_path=Path(os.getenv("DATABASE_PATH", "runtime/harness.sqlite3")),
            trace_dir=Path(os.getenv("TRACE_DIR", "runtime/traces")),
            dataset_path=Path(os.getenv("DATASET_PATH", str(PROJECT_ROOT / "data"))),
            llm_mode=mode,
            llm_model=os.getenv("LLM_MODEL", ""),
            llm_api_key=os.getenv("OPENAI_API_KEY", os.getenv("LLM_API_KEY", "")),
            llm_base_url=os.getenv("LLM_BASE_URL") or None,
            api_token=os.getenv("API_TOKEN", "local-demo-token"),
            limits=ExecutionLimits(**values),
        )
