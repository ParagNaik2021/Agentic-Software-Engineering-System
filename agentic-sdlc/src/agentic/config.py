"""Global settings for the orchestrator, loaded from env / .env.

Every module that needs a path, a budget, a threshold or the LLM mode
reads it from here rather than hardcoding it, so a single override
(env var or .env) reconfigures the whole system consistently.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class LLMMode(StrEnum):
    LIVE = "live"
    REPLAY = "replay"
    MOCK = "mock"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AGENTIC_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- paths ---
    repo_root: Path = Field(default_factory=lambda: Path(__file__).resolve().parents[2])
    runs_dir: Path = Field(default_factory=lambda: Path("runs"))
    cassettes_dir: Path = Field(default_factory=lambda: Path("cassettes"))
    workspace_dir: Path = Field(default_factory=lambda: Path("workspace/urlshortener"))
    policies_dir: Path = Field(
        default_factory=lambda: Path("src/agentic/governance/policies")
    )
    prompts_dir: Path = Field(default_factory=lambda: Path("src/agentic/llm/prompts"))

    # --- LLM ---
    llm_mode: LLMMode = LLMMode.REPLAY
    llm_model: str = "claude-sonnet-5"
    llm_temperature: float = 0.2
    llm_max_tokens: int = 4096
    replay_on_miss: Literal["error", "live"] = "error"
    anthropic_api_key: str | None = None

    # --- budgets ---
    max_run_wall_clock_seconds: int = 3600
    max_run_tokens: int = 2_000_000
    max_run_cost_usd: float = 25.0
    replan_budget: int = 3

    # --- thresholds ---
    coverage_floor: float = 0.80
    change_budget_files: int = 10
    ambiguity_score_threshold: float = 0.5

    # --- concurrency ---
    max_concurrent_nodes: int = 4

    # --- retry defaults ---
    retry_max_attempts: int = 3
    retry_base_seconds: float = 2.0
    retry_cap_seconds: float = 30.0

    def run_dir(self, run_id: str) -> Path:
        return self.runs_dir / run_id


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
