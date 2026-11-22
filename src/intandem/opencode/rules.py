"""A ``Guard`` as OpenCode permission rules.

OpenCode evaluates a ruleset in order and the last match wins, so broad rules
come first and the narrow rules that override them come last.
"""

from __future__ import annotations

from typing import cast

from intandem.backend.guard import Guard
from intandem.opencode.models import PermissionAction, PermissionRule

# What an unattended run needs to avoid stalling on OpenCode's cautious defaults.
UNATTENDED_RULES: tuple[PermissionRule, ...] = (
    PermissionRule(permission="edit", pattern="*", action=PermissionAction.ALLOW),
    PermissionRule(permission="external_directory", pattern="*", action=PermissionAction.ALLOW),
    PermissionRule(permission="doom_loop", pattern="*", action=PermissionAction.ALLOW),
)


def guard_to_rules(guard: Guard) -> list[PermissionRule]:
    """The spec session's ruleset: unattended defaults, then one ask per pattern.

    Ask, not deny: an attempted edit stops and waits for a human with the diff
    attached, instead of failing in a way the model would route around.
    """
    base = list(UNATTENDED_RULES) if guard.unattended else []
    asks = [
        PermissionRule(permission="edit", pattern=pattern, action=PermissionAction.ASK)
        for pattern in guard.protected
    ]
    return [*base, *asks]


def subagent_rules(inherited: object, guard: Guard) -> list[PermissionRule]:
    """A subagent's own rules, then a deny per protected pattern, last so it wins.

    A subagent runs in a session of its own, created by OpenCode with its agent's
    rules, not the spec's. Deny rather than ask: ask would loosen an agent that
    already denies edits.
    """
    existing = [
        PermissionRule.model_validate(rule)
        for rule in (cast(list[object], inherited) if isinstance(inherited, list) else [])
        if isinstance(rule, dict)
    ]
    denies = [
        PermissionRule(permission="edit", pattern=pattern, action=PermissionAction.DENY)
        for pattern in guard.protected
    ]
    return [*existing, *denies]
