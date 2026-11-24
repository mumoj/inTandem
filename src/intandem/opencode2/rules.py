"""Translates ``Guard`` policies into OpenCode 2.x permission rulesets.

OpenCode 2.x uses ``{action, resource, effect}`` objects (replacing 1.x's
``{permission, pattern, action}``) with the same last-match-wins evaluation
order.
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
    """Build a session ruleset with unattended defaults and ``ask`` rules for protected globs."""
    base = [dict(rule) for rule in UNATTENDED_RULES] if guard.unattended else []
    asks = [{"action": "edit", "resource": p, "effect": "ask"} for p in guard.protected]
    return [*base, *asks]


def subagent_rules(inherited: object, guard: Guard) -> list[Rule]:
    """Append ``deny`` rules for protected globs after a subagent's inherited rules."""
    existing: list[Rule] = []
    for rule in cast(list[object], inherited) if isinstance(inherited, list) else []:
        if isinstance(rule, dict):
            fields = cast(dict[str, Any], rule)
            if all(isinstance(fields.get(key), str) for key in ("action", "resource", "effect")):
                existing.append({key: fields[key] for key in ("action", "resource", "effect")})
    denies = [{"action": "edit", "resource": p, "effect": "deny"} for p in guard.protected]
    return [*existing, *denies]
