"""Paths and default settings."""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("HOLDCO_DATA", BASE_DIR / "data")).resolve()
COMPANIES_DIR = DATA_DIR / "companies"
TRASH_DIR = DATA_DIR / "trash"
DB_PATH = DATA_DIR / "holdco.db"
STATIC_DIR = Path(__file__).resolve().parent / "static"

HOST = os.environ.get("HOLDCO_HOST", "127.0.0.1")
PORT = int(os.environ.get("HOLDCO_PORT", "8420"))
# Optional password. Required if you expose the dashboard beyond this PC (HOLDCO_HOST=0.0.0.0).
PASSWORD = os.environ.get("HOLDCO_PASSWORD", "")

# Global settings, editable from the Settings page (stored in the database).
DEFAULT_SETTINGS: dict = {
    # "ollama" talks to Ollama's native API (lets us set the context window).
    # "openai" talks to any OpenAI-compatible server: LM Studio, vLLM, llama.cpp, SGLang...
    "llm_backend": "ollama",
    "llm_base_url": "http://127.0.0.1:11434",
    "llm_api_key": "",
    "default_model": "gpt-oss:20b",   # agent workhorse: reliable tool calls, ~14 GB
    "ceo_model": "",                  # blank = use default_model
    "num_ctx": 32768,                 # context window per request (Ollama backend)
    "sampling": "auto",               # auto = each model maker's recommended settings; manual = use temperature
    "temperature": 0.4,
    "keep_alive": "30m",              # how long Ollama keeps the model loaded on the GPU
    "max_concurrent_runs": 2,         # agents working at the same time (match OLLAMA_NUM_PARALLEL)
    "max_steps_per_run": 14,          # tool calls per work session
    "request_timeout": 600,           # seconds to wait for one model reply
    "tool_mode": "auto",              # auto | native | json  (json = prompt-based tool calls)
    "scheduler_enabled": True,        # agents keep working on their own
    "search_provider": "duckduckgo",  # duckduckgo | searxng | brave
    "searxng_url": "",
    "brave_api_key": "",
    "owner_name": "the owner",
}

# What happens when an agent tries an outbound action.
#   approve = goes to your inbox and waits for you
#   auto    = runs immediately
#   off     = the agent is told it's not allowed
POLICY_ACTIONS = {
    "send_email": "Send an email",
    "social_post": "Publish a social post",
    "deploy_site": "Deploy the website",
    "http_post": "Send data to an outside service",
    "run_code": "Run code on this PC",
    "spend_money": "Spend money",
}
DEFAULT_POLICIES = {
    "send_email": "approve",
    "social_post": "approve",
    "deploy_site": "approve",
    "http_post": "approve",
    "run_code": "approve",
    "spend_money": "approve",
}
LOCKED_POLICIES = {"spend_money"}  # money never moves without you

DEFAULT_COMPANY_SETTINGS: dict = {
    "cycle_minutes": 60,      # how often the CEO re-plans
    "directives": "",         # standing orders every agent must follow
    "policies": {},           # per-company overrides of DEFAULT_POLICIES
    "daily_email_limit": 20,  # new domains should start around 20 cold emails a day
    # Email (optional): each company can have its own mailbox.
    "smtp_host": "", "smtp_port": 587, "smtp_user": "", "smtp_password": "",
    "smtp_from": "", "smtp_from_name": "",
    "imap_host": "", "imap_port": 993, "imap_user": "", "imap_password": "",
    # Website deploys (optional): Netlify personal access token.
    "netlify_token": "", "netlify_site_id": "", "site_url": "",
    # Social posts / outbound automation (optional): a webhook (Zapier, Make, n8n, Discord...).
    "webhook_url": "",
}


WORKSPACE_DIRS = ["docs", "research", "site", "code", "marketing", "sales", "support", "ops"]
