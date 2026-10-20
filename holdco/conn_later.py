"""The later tier (Phase 4, docs/phases/phase-4-scale.md section 3): ComfyUI, ad platforms and accounting.

Each is a definition with available: False and a reason the connector card shows instead of a form. GET
/api/connectors lists them; connectors.save refuses them; find_tool, the health checks, the metric pipes and the
Doctor ignore them (connectors.unavailable). They carry the usual card keys so every card reads the same way.
"""
from __future__ import annotations

REASONS = {
    "comfyui": ("Local image generation shares the GPU with the language model. It will be scheduled in the quiet "
                "window once a company has traction."),
    "ads": "Ad platforms spend money per hour. They come after revenue justifies a hard daily cap per company.",
    "accounting": "QuickBooks or Xero sync comes once there are transactions to reconcile.",
}


def _later(key: str, label: str, category: str, scope: str, agents_will: list[str]) -> dict:
    reason = REASONS[key]

    def health(inst) -> tuple[str, str]:
        return "warn", f"Not yet available. {reason}"

    return {
        "label": label,
        "category": category,
        "kind": "api",
        "scope": scope,
        "available": False,
        "reason": reason,
        "setup": {"where_to_get_key": "", "permissions": [], "fields": [], "test_mode": "", "docs_url": "",
                  "agents_can": [], "agents_will": agents_will, "note": f"Not yet available. {reason}"},
        "health": health,
    }


DEFINITIONS = {
    "comfyui": _later("comfyui", "ComfyUI", "Images", "global",
                      ["Generate images for the site and posts on this PC's GPU, in the quiet window"]),
    "ads": _later("ads", "Ads", "Advertising", "company",
                  ["Run ad campaigns under a hard daily cap per company"]),
    "accounting": _later("accounting", "Accounting", "Accounting", "company",
                         ["Sync payments and purchases to QuickBooks or Xero"]),
}
