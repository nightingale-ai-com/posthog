import re
import json

import structlog

from posthog.llm.gateway_client import Product, get_llm_client

logger = structlog.get_logger(__name__)

_PRODUCT_DEBUG_TERMS = (
    "automation",
    "destination",
    "slack destination",
    "posthog ai feedback",
    "feature flag",
    "experiment",
    "survey",
    "dashboard",
    "insight",
    "session replay",
    "recording",
    "mcp",
    "webhook",
)

_EXPLICIT_CODE_PATTERNS = (
    r"\brepository\b",
    r"\brepo\b",
    r"\bpull request\b",
    r"\bopen a pr\b",
    r"\bcreate a pr\b",
    r"\bcommit\b",
    r"\bbranch\b",
    r"\bmodify code\b",
    r"\bchange code\b",
    r"\bwrite code\b",
    r"\bimplement\b",
    r"\.py\b",
    r"\.ts\b",
    r"\.tsx\b",
    r"\.js\b",
    r"\bserializer\b",
    r"\bviewset\b",
    r"\bmigration\b",
)


def classify_task_needs_repo(
    event_text: str,
    thread_messages: list[dict[str, str]],
    *,
    product: Product = "slack_app_routing",
) -> bool:
    """Classify whether a chat conversation requires code repository access.

    Returns True if the task likely needs a repo (writing code, fixing bugs, PRs),
    False if it does not (analytics, data queries, PostHog config).
    Defaults to True on error (conservative — falls back to the discovery agent/picker).
    """
    conversation = "\n".join(f"{msg['user']}: {msg['text']}" for msg in thread_messages)
    normalized = f"{conversation}\nLatest message: {event_text}".lower()

    if any(term in normalized for term in _PRODUCT_DEBUG_TERMS) and not any(
        re.search(pattern, normalized) for pattern in _EXPLICIT_CODE_PATTERNS
    ):
        logger.info("classify_task_needs_repo_heuristic_non_repo", event_text=event_text)
        return False

    prompt = (
        "You are a task classifier. Given a chat conversation, determine whether the task "
        "requires access to a code repository (e.g. writing code, fixing bugs, creating PRs, "
        "reviewing code, modifying files) or NOT (e.g. answering questions about analytics, "
        "querying data, PostHog configuration, general knowledge questions, planning, or "
        "investigating product behavior in a PostHog workspace using MCP/tools).\n\n"
        "Return needs_repo=false for tasks that are primarily about debugging or investigating "
        "automations, destinations, feature flags, experiments, surveys, dashboards, insights, "
        "recordings, traces, or chat integrations inside PostHog, unless the user explicitly "
        "asks to change code, open a PR, edit files, or work in a specific repository.\n\n"
        "A complaint about something the team's own app, site, or SDK does (crashes, broken pages, "
        "wrong rendering, slow loads of a site they ship) is a code change in a repo they own → "
        "needs_repo. But complaints about PostHog itself as a product (its dashboards hanging, "
        "product pages loading slowly, UI bugs in PostHog screens) are SaaS product issues, not "
        "the team's code → no_repo. Important exception: 'wrong data', 'missing events', or "
        "'numbers look off' in PostHog usually means the team's tracking code is broken (wrong "
        "event names, identification logic, SDK setup) — that's a code fix in their repo → "
        "needs_repo. When in doubt, lean needs_repo=true — the discovery agent can still report "
        "there's no good match.\n\n"
        f"Conversation:\n{conversation}\n\n"
        f"Latest message: {event_text}\n\n"
        'Respond with ONLY a JSON object: {{"needs_repo": true}} or {{"needs_repo": false}}'
    )
    try:
        client = get_llm_client(product)
        response = client.chat.completions.create(
            model="claude-haiku-4-5-20251001",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=64,
            temperature=0,
        )
        content = (response.choices[0].message.content or "").strip()
        if content.startswith("```"):
            content = content.strip("`").removeprefix("json").strip()
        parsed = json.loads(content)
        return bool(parsed.get("needs_repo", True))
    except Exception:
        logger.exception("classify_task_needs_repo_failed")
        return True
