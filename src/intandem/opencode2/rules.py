"""A ``Guard`` as an OpenCode 2 permission ruleset.

2.x renamed the rule fields (``{action, resource, effect}`` for 1.x's
``{permission, pattern, action}``) but kept the evaluation: in order, the last
match wins. Checked against 2.3.2: ``[allow edit *, ask edit instruction.md]``
stops an edit of ``instruction.md`` for a human, and lets every other through.
"""

from __future__ import annotations

from typing import Any, cast

from intandem.backend.guard import Guard

Rule = dict[str, str]

UNATTENDED_RULES: tuple[Rule, ...] = (
    {"action": "edit", "resource": "*", "effect": "allow"},
    {"action": "external_directory", "resource": "*", "effect": "allow"},
    {"action": "doom_loop", "resource": "*", "effect": "allow"},
)


def guard_to_rules(guard: Guard) -> list[Rule]:
    """The spec session's ruleset: unattended defaults, then one ask per pattern."""
    base = [dict(rule) for rule in UNATTENDED_RULES] if guard.unattended else []
    asks = [{"action": "edit", "resource": p, "effect": "ask"} for p in guard.protected]
    return [*base, *asks]


def subagent_rules(inherited: object, guard: Guard) -> list[Rule]:
    """A subagent's own rules, then a deny per protected pattern, last so it wins."""
    existing: list[Rule] = []
    for rule in cast(list[object], inherited) if isinstance(inherited, list) else []:
        if isinstance(rule, dict):
            fields = cast(dict[str, Any], rule)
            if all(isinstance(fields.get(key), str) for key in ("action", "resource", "effect")):
                existing.append({key: fields[key] for key in ("action", "resource", "effect")})
    denies = [{"action": "edit", "resource": p, "effect": "deny"} for p in guard.protected]
    return [*existing, *denies]
