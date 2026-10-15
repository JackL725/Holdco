"""Business archetype playbooks (Phase 0.5, design section 4.3).

The first real company misread its own business: a roofing-lead subscription is two-sided (the hard part is
producing homeowner demand for less than roofers pay per lead), yet its charter had no price, no competitors
and no demand plan. A short playbook per business type puts the money flow, the usual riskiest assumptions,
first-week experiments that touch real customers, the metrics that matter, the common traps and the compliance
notes in front of the founder and the planner. Each one is deliberately short: it goes into prompts of a local
model with a small context window.

The archetype comes from the company setting "archetype" when the owner set one, otherwise from keyword
scoring over the idea and the charter (classify). No match means no playbook, rather than a wrong one.
"""
from __future__ import annotations

import re

SECTION_CAP = 1200        # characters; the design's cap for the prompt section
MIN_SCORE = 3             # below this nothing is confident enough to steer the company

# Each playbook: label, keywords as (regex, weight), and the content. Keywords are matched case-insensitively
# on word boundaries; each keyword counts at most 3 times so one repeated word cannot decide alone.
PLAYBOOKS: dict[str, dict] = {
    "lead_generation": {
        "label": "lead generation or marketplace",
        "keywords": [(r"leads?", 3), (r"lead[- ]?gen\w*", 3), (r"marketplace", 3), (r"per lead", 3),
                     (r"two[- ]sided", 3), (r"match(es|ing)? (home ?owners|customers|clients|buyers)", 2),
                     (r"directory", 2), (r"referrals?", 2), (r"quotes? from", 2), (r"connect\w* \w+ with", 1)],
        "money_flow": "Two sides. Providers pay per lead or a monthly fee. The business is producing customer "
                      "demand for less than providers pay per lead.",
        "riskiest": ["Customers will send inquiries through us at a cost below what providers pay per lead.",
                     "Providers will pay for leads from a new source, and how much."],
        "experiments": ["Ask 10 providers what they pay per lead today, to whom, and what makes a lead worth it.",
                        "Test whether you can get customer inquiries at all: one page with a request form for "
                        "one service in one area.",
                        "Write down the cost per inquiry you must beat (provider price per lead minus margin) "
                        "before spending anything."],
        "metrics": ["inquiries per week", "cost per inquiry", "price providers pay per lead",
                    "share of leads providers accept"],
        "traps": ["Building the provider side (dashboards, subscriptions) before producing one customer inquiry.",
                  "Selling one lead to many providers without saying so."],
        "compliance": ["Never contact consumers without consent; never buy lists.",
                       "Say on the form who will receive the request."],
    },
    "newsletter": {
        "label": "newsletter or content",
        "keywords": [(r"newsletters?", 3), (r"substack", 3), (r"beehiiv", 3), (r"weekly (email|digest|roundup)", 2),
                     (r"digest", 2), (r"blog\w*", 2), (r"podcast\w*", 2), (r"youtube", 2), (r"sponsor\w*", 2),
                     (r"readers?", 1), (r"content", 1), (r"audience", 1), (r"issues?", 1)],
        "money_flow": "Readers subscribe free. Money comes from sponsors (per issue), a paid tier or products "
                      "sold to the audience, so revenue follows subscribers who open.",
        "riskiest": ["Enough target readers subscribe and keep opening (around 40% opens or better).",
                     "A sponsor or paid tier will pay at this audience size."],
        "experiments": ["Publish 3 real issues and ask 20 people in the audience to subscribe; count signups "
                        "and replies.",
                        "Share one useful piece where these readers already gather, following the community "
                        "rules, with a signup link.",
                        "Ask 5 possible sponsors what they pay per issue in this niche."],
        "metrics": ["subscribers", "open rate", "click rate", "replies per issue", "sponsor revenue per issue"],
        "traps": ["Building a fancy site before 3 issues exist.", "Writing for everyone instead of one reader."],
        "compliance": ["Email only people who opted in; every email has an unsubscribe link and a postal "
                       "address.", "Label sponsored content as sponsored."],
    },
    "saas": {
        "label": "SaaS or software",
        "keywords": [(r"saas", 3), (r"software", 2), (r"apps?", 2), (r"api", 2), (r"(browser|chrome) extension", 3),
                     (r"plug-?ins?", 2), (r"dashboards?", 1), (r"platform", 1), (r"subscription", 1),
                     (r"automat\w*", 1), (r"tool", 1)],
        "money_flow": "Customers pay a monthly or yearly subscription per seat or by usage. Profit comes from "
                      "keeping customers, so churn matters as much as signups.",
        "riskiest": ["The problem hurts enough that people pay to solve it now, at this price.",
                     "Buyers can be reached cheaply enough (estimate: acquisition cost under a third of "
                     "first-year revenue)."],
        "experiments": ["Interview 10 target users about how they solve the problem today and what it costs them.",
                        "A landing page with the offer, the price and a waitlist; count signups per 100 visitors.",
                        "Offer 3 people a manual, done-for-you version before writing code."],
        "metrics": ["signups per 100 visitors", "trial to paid", "monthly recurring revenue", "monthly churn"],
        "traps": ["Building features nobody asked for.", "Counting free users who never pay as success."],
        "compliance": ["Privacy policy and terms before taking accounts; personal data under GDPR or local law."],
    },
    "services": {
        "label": "services or agency",
        "keywords": [(r"agency", 3), (r"consult\w*", 3), (r"freelanc\w*", 2), (r"done[- ]for[- ]you", 3),
                     (r"retainers?", 3), (r"virtual assistants?", 3), (r"bookkeeping", 2),
                     (r"(service|services) for (small )?(businesses|companies|brands)", 2), (r"clients", 1),
                     (r"we (design|build|write|manage|run)", 2),
                     (r"social media (management|managers?|plans?|content|posts)", 2),
                     (r"for (small |local )?(businesses|cafes|caf\u00e9s|restaurants|shops|salons|gyms)", 2)],
        "money_flow": "Clients pay per project or a monthly retainer for work done for them. Margin is price "
                      "minus delivery time, so scope decides profit.",
        "riskiest": ["Target clients will buy from a new, unknown provider at this price.",
                     "Delivery fits the hours planned per client (an estimate until the first job)."],
        "experiments": ["Write a one-page offer with fixed scope and price and send it to 10 well-matched "
                        "prospects, once the owner approves.",
                        "Run one small pilot to learn the real delivery time.",
                        "Collect 3 work samples, real or clearly labelled as samples."],
        "metrics": ["proposals sent", "reply rate", "clients won", "revenue per client", "hours per client"],
        "traps": ["Open-ended scope with no price.",
                  "Generic outreach to hundreds instead of tailored notes to a few."],
        "compliance": ["A written agreement with scope, price and payment terms that the owner signs.",
                       "Cold email follows CAN-SPAM; honour opt-outs at once."],
    },
    "digital_products": {
        "label": "digital products",
        "keywords": [(r"templates?", 3), (r"e-?books?", 3), (r"courses?", 3), (r"printables?", 3),
                     (r"digital products?", 3), (r"gumroad", 3), (r"downloads?", 2), (r"toolkits?", 2),
                     (r"presets", 2), (r"guides?", 1)],
        "money_flow": "One-time sales of templates, guides, courses or files. Margin is close to 100%, so the work "
                      "is reaching buyers and turning visitors into sales.",
        "riskiest": ["Buyers will pay for this over free alternatives.",
                     "Buyers can be reached where they already look (a marketplace, search, a community)."],
        "experiments": ["Find 5 similar products, their prices and reviews; note what buyers complain about.",
                        "Pre-sell: a sales page with the price and a pre-order or waitlist before making it all.",
                        "Share one free sample where buyers gather; count downloads and replies."],
        "metrics": ["sales page visitors", "conversion rate", "revenue per visitor", "refund rate"],
        "traps": ["Making the whole product before anyone signed up.", "Competing on price with free versions."],
        "compliance": ["A clear refund policy; sales tax or VAT through the payment platform.",
                       "Use only material you have the rights to."],
    },
    "ecommerce": {
        "label": "e-commerce",
        "keywords": [(r"e-?commerce", 3), (r"shopify", 3), (r"dropshipping", 3), (r"print[- ]on[- ]demand", 3),
                     (r"etsy", 3), (r"subscription box", 3), (r"online store", 3), (r"store", 2), (r"shop", 2),
                     (r"inventory", 2), (r"shipping", 2), (r"merch", 2), (r"physical products?", 2)],
        "money_flow": "Customers buy physical products. Profit is price minus product cost, shipping, fees, "
                      "returns and the cost of each sale (ads).",
        "riskiest": ["Customers buy at a price that leaves margin after product, shipping, fees and ads.",
                     "A supplier delivers on time at the quality shown."],
        "experiments": ["Price 3 suppliers and estimate the margin per order, labelled as an estimate.",
                        "A product page with photos, price and a pre-order or waitlist; count signups per 100 "
                        "visitors.",
                        "Ask 10 target buyers what they pay today and where they buy."],
        "metrics": ["visitors", "conversion rate", "average order value", "margin per order", "return rate"],
        "traps": ["Buying inventory before demand is shown.", "Leaving shipping and returns out of the price."],
        "compliance": ["Product safety and labelling rules for the category; honest delivery times.",
                       "Consumer refund rights; sales tax or VAT."],
    },
    "local_services": {
        "label": "local services",
        "keywords": [(r"local", 2), (r"near me", 2), (r"in-home", 2), (r"house ?cleaning", 3), (r"cleaning", 2),
                     (r"lawn\w*", 2), (r"landscaping", 2), (r"dog walk\w*", 3), (r"pet sitting", 3),
                     (r"handyman", 3), (r"mobile (detailing|grooming|car wash)", 3),
                     (r"grooming", 2), (r"mobile", 1), (r"tutoring", 2),
                     (r"service area", 2), (r"in (my|our|the) (city|town|area|neighbou?rhood)", 2)],
        "money_flow": "Local customers pay per visit or a recurring plan for work done in person. Profit is price "
                      "minus labour and travel, limited by the jobs that fit in a day.",
        "riskiest": ["Enough local customers want this at this price inside the service area.",
                     "Jobs can be delivered at the planned labour and travel cost (an estimate)."],
        "experiments": ["Check maps and local directories for competitors, their prices and reviews.",
                        "A page for one service in one area with a booking or quote form.",
                        "Ask 10 local customers what they pay today, in groups where that is allowed."],
        "metrics": ["quote requests per week", "booking rate", "revenue per job", "repeat customers", "reviews"],
        "traps": ["Covering a big area before winning one neighbourhood.", "Pricing below labour and travel cost."],
        "compliance": ["Licences and insurance the service needs (the owner arranges them).",
                       "Consent before texting or calling customers."],
    },
}

_COMPILED = {key: [(re.compile(r"\b(?:" + rx + r")\b", re.I), w) for rx, w in p["keywords"]]
             for key, p in PLAYBOOKS.items()}


def scores(text: str) -> dict[str, int]:
    """Keyword score per archetype for a piece of text (each keyword counts at most 3 times)."""
    text = text or ""
    return {key: sum(min(3, len(rx.findall(text))) * w for rx, w in kws) for key, kws in _COMPILED.items()}


def classify(text: str) -> str:
    """The archetype key for an idea or charter, or '' when nothing scores high enough. Ties go to the
    archetype listed first in PLAYBOOKS."""
    best, best_score = "", 0
    for key, score in scores(text).items():
        if score > best_score:
            best, best_score = key, score
    return best if best_score >= MIN_SCORE else ""


def archetype(company: dict) -> str:
    """The owner's override (company setting "archetype") when it names a playbook, else the classification of
    the idea plus the charter."""
    from . import db   # imported here so this module stays free of database imports for pure use
    chosen = str(db.company_settings(company).get("archetype") or "").strip().lower().replace(" ", "_")
    if chosen in PLAYBOOKS:
        return chosen
    return classify(f"{company.get('idea') or ''}\n{company.get('charter') or ''}")


def experiments(company: dict, n: int = 3) -> list[str]:
    """The playbook's first-week experiments for the company ([] when no playbook applies)."""
    key = archetype(company)
    return list(PLAYBOOKS[key]["experiments"][:n]) if key else []


def render(key: str) -> str:
    """The '# Playbook' text for one archetype, capped at SECTION_CAP characters."""
    p = PLAYBOOKS.get(key)
    if not p:
        return ""
    parts = [f"# Playbook: {p['label']}",
             f"How money flows: {p['money_flow']}",
             "Riskiest assumptions:\n" + "\n".join(f"- {r}" for r in p["riskiest"]),
             "First-week experiments (touch real customers):\n" + "\n".join(f"- {e}" for e in p["experiments"]),
             "Metrics that matter: " + ", ".join(p["metrics"]) + ".",
             "Traps: " + " ".join(p["traps"]),
             "Compliance: " + " ".join(p["compliance"])]
    text = "\n".join(parts)
    if len(text) > SECTION_CAP:
        text = text[:SECTION_CAP - 1].rsplit("\n", 1)[0]
    return text


def section(company: dict) -> str:
    """'# Playbook' prompt section for the company's archetype, '' when none applies."""
    key = archetype(company)
    return render(key) if key else ""
