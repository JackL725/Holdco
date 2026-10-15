"""Paths, default settings, approval policies and the connector catalogue."""
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
    # Owner notifications (optional): where to ping you when agents need you or report something.
    "notify_webhook_url": "",         # Slack or Discord incoming webhook, or any URL that accepts JSON
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "ntfy_topic": "",                 # ntfy.sh topic (or a full URL to your own ntfy server topic)
    "notify_events": ["approvals", "messages", "errors"],
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


def F(key: str, label: str, secret: bool = False, hint: str = "", placeholder: str = "", default: str = "",
      kind: str = "text") -> dict:
    return {"key": key, "label": label, "secret": secret, "hint": hint, "placeholder": placeholder, "default": default,
            "kind": kind}


# The connector catalogue. Each connector is a set of per-company settings plus what it enables ("capabilities"):
#   smtp / imap  company email      deploy  publish the site      forms     read signups from the site
#   payments     sales, refunds, subscriptions, balance            analytics visitors and pages
#   social:<platform>  post directly to that platform              calendar  booked meetings
# Agents never see the secrets; connectors.py makes the calls. The dashboard's Connectors tab renders this list.
CONNECTORS: dict[str, dict] = {
    "email": {
        "label": "Company email", "category": "Email", "capabilities": ["smtp", "imap"],
        "blurb": "Any mailbox with SMTP and IMAP (Gmail with an app password, Zoho, Fastmail, your own domain). "
                 "Sales and support agents send through it (after your approval) and inbound mail becomes support tasks.",
        "docs": "https://support.google.com/mail/answer/185833",
        "fields": [F("smtp_host", "SMTP server", placeholder="smtp.gmail.com"), F("smtp_port", "SMTP port", default="587", kind="number"),
                   F("smtp_user", "Username"), F("smtp_password", "Password or app password", secret=True),
                   F("smtp_from", "From address", placeholder="hello@yourcompany.com"), F("smtp_from_name", "From name"),
                   F("imap_host", "IMAP server", placeholder="imap.gmail.com"), F("imap_port", "IMAP port", default="993", kind="number"),
                   F("imap_user", "IMAP username", hint="Usually the same as above"), F("imap_password", "IMAP password", secret=True)],
        "required": ["smtp_host", "smtp_user", "smtp_password"],
    },
    "netlify": {
        "label": "Netlify (hosting + forms)", "category": "Website", "capabilities": ["deploy", "forms"],
        "blurb": "Publishes the site/ folder with one click and lets agents read waitlist and contact form submissions "
                 "(Netlify Forms). Holdco creates the site on the first approved deploy.",
        "docs": "https://app.netlify.com/user/applications#personal-access-tokens",
        "fields": [F("netlify_token", "Personal access token", secret=True),
                   F("netlify_site_id", "Site ID", hint="Leave blank: filled in after the first deploy"),
                   F("site_url", "Public address", placeholder="https://yourcompany.netlify.app", kind="url")],
        "required": ["netlify_token"],
    },
    "stripe": {
        "label": "Stripe", "category": "Payments", "capabilities": ["payments"],
        "blurb": "Balance, sales, refunds, fees and subscriptions flow into the ledger (read-only). Create a restricted "
                 "key with read access to Balance, Charges, Balance transactions and Subscriptions; never a full secret key.",
        "docs": "https://dashboard.stripe.com/apikeys/create",
        "fields": [F("stripe_secret_key", "Restricted API key (rk_live_… or rk_test_…)", secret=True)],
        "required": ["stripe_secret_key"],
    },
    "lemonsqueezy": {
        "label": "Lemon Squeezy", "category": "Payments", "capabilities": ["payments"],
        "blurb": "A merchant of record for digital products and SaaS: they handle sales tax, VAT and chargebacks. "
                 "Orders and subscriptions flow into the ledger (read-only).",
        "docs": "https://app.lemonsqueezy.com/settings/api",
        "fields": [F("lemonsqueezy_api_key", "API key", secret=True), F("lemonsqueezy_store_id", "Store ID", hint="Settings → Stores")],
        "required": ["lemonsqueezy_api_key"],
    },
    "gumroad": {
        "label": "Gumroad", "category": "Payments", "capabilities": ["payments"],
        "blurb": "Sales of digital products flow into the ledger (read-only).",
        "docs": "https://app.gumroad.com/settings/advanced",
        "fields": [F("gumroad_access_token", "Access token", secret=True)],
        "required": ["gumroad_access_token"],
    },
    "plausible": {
        "label": "Plausible Analytics", "category": "Analytics", "capabilities": ["analytics"],
        "blurb": "Privacy-friendly, cookie-free analytics. Agents read visitors, pages and sources for KPI reports.",
        "docs": "https://plausible.io/docs/stats-api",
        "fields": [F("plausible_site_id", "Site (domain as shown in Plausible)", placeholder="yourcompany.com"),
                   F("plausible_api_key", "Stats API key", secret=True),
                   F("plausible_url", "Server", default="https://plausible.io", hint="Change only if self-hosted", kind="url")],
        "required": ["plausible_site_id", "plausible_api_key"],
    },
    "goatcounter": {
        "label": "GoatCounter", "category": "Analytics", "capabilities": ["analytics"],
        "blurb": "Free, open-source, cookie-free analytics. Agents read visitors and top pages.",
        "docs": "https://www.goatcounter.com/help/api",
        "fields": [F("goatcounter_url", "Your GoatCounter address", placeholder="https://yourcompany.goatcounter.com", kind="url"),
                   F("goatcounter_api_key", "API token", secret=True)],
        "required": ["goatcounter_url", "goatcounter_api_key"],
    },
    "x": {
        "label": "X (Twitter)", "category": "Social", "capabilities": ["social:x"],
        "blurb": "Posts approved tweets directly. The free developer tier allows about 1,500 posts a month; create an "
                 "app with read and write permissions and generate user access tokens.",
        "docs": "https://developer.x.com/en/portal/dashboard",
        "fields": [F("x_api_key", "API key", secret=True), F("x_api_secret", "API key secret", secret=True),
                   F("x_access_token", "Access token", secret=True), F("x_access_secret", "Access token secret", secret=True)],
        "required": ["x_api_key", "x_api_secret", "x_access_token", "x_access_secret"],
    },
    "bluesky": {
        "label": "Bluesky", "category": "Social", "capabilities": ["social:bluesky"],
        "blurb": "Posts approved posts directly with an app password (never your main password).",
        "docs": "https://bsky.app/settings/app-passwords",
        "fields": [F("bluesky_handle", "Handle", placeholder="yourcompany.bsky.social"),
                   F("bluesky_app_password", "App password", secret=True),
                   F("bluesky_url", "Server", default="https://bsky.social", hint="Change only for another PDS", kind="url")],
        "required": ["bluesky_handle", "bluesky_app_password"],
    },
    "mastodon": {
        "label": "Mastodon", "category": "Social", "capabilities": ["social:mastodon"],
        "blurb": "Posts to any Mastodon-compatible server (Mastodon, GoToSocial, Pixelfed text posts).",
        "docs": "https://docs.joinmastodon.org/client/token/",
        "fields": [F("mastodon_url", "Server", placeholder="https://mastodon.social", kind="url"),
                   F("mastodon_access_token", "Access token (write:statuses)", secret=True)],
        "required": ["mastodon_url", "mastodon_access_token"],
    },
    "linkedin": {
        "label": "LinkedIn", "category": "Social", "capabilities": ["social:linkedin"],
        "blurb": "Posts to your personal LinkedIn profile. Needs an app with the 'Share on LinkedIn' and 'Sign In with "
                 "LinkedIn using OpenID Connect' products and a member access token (w_member_social, openid, profile).",
        "docs": "https://learn.microsoft.com/en-us/linkedin/consumer/integrations/self-serve/share-on-linkedin",
        "fields": [F("linkedin_access_token", "Member access token", secret=True),
                   F("linkedin_author_urn", "Author URN", hint="Leave blank: looked up when you test the connection",
                     placeholder="urn:li:person:…")],
        "required": ["linkedin_access_token"],
    },
    "webhook": {
        "label": "Posting webhook", "category": "Social", "capabilities": ["social:webhook"],
        "blurb": "For platforms without a direct connector: approved posts are sent here as JSON, so Zapier, Make, n8n "
                 "or a Discord webhook can publish them anywhere.",
        "docs": "https://zapier.com/apps/webhook/integrations",
        "fields": [F("webhook_url", "Webhook address", kind="url")],
        "required": ["webhook_url"],
    },
    "calcom": {
        "label": "Cal.com", "category": "Scheduling", "capabilities": ["calendar"],
        "blurb": "Sales and success agents see upcoming meetings booked through your Cal.com link.",
        "docs": "https://app.cal.com/settings/developer/api-keys",
        "fields": [F("calcom_api_key", "API key", secret=True),
                   F("calcom_url", "Server", default="https://api.cal.com", hint="Change only if self-hosted", kind="url")],
        "required": ["calcom_api_key"],
    },
}
CONNECTOR_CATEGORIES = ["Email", "Website", "Payments", "Analytics", "Social", "Scheduling"]

for _c in CONNECTORS.values():
    for _f in _c["fields"]:
        if _f["key"] not in DEFAULT_COMPANY_SETTINGS:
            DEFAULT_COMPANY_SETTINGS[_f["key"]] = _f["default"]

# Company settings that must never be shown back to the dashboard in clear text.
SECRET_COMPANY_KEYS = {f["key"] for c in CONNECTORS.values() for f in c["fields"] if f["secret"]}

WORKSPACE_DIRS = ["docs", "research", "site", "code", "marketing", "sales", "support", "ops", "ledger"]
