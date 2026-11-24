"""Translates ``Guard`` policies into OpenCode 1.x permission rulesets.

OpenCode applies rules in order with last-match-wins semantics, so broad
unattended rules are placed first and per-pattern restrictions last.
"""

from __future__ import annotations

from typing import cast

from intandem.backend.guard import Guard
from intandem.opencode.models import PermissionAction, PermissionRule

# Baseline allow rules so unattended sessions don't pause on routine operations.
UNATTENDED_RULES: tuple[PermissionRule, ...] = (
    PermissionRule(permission="edit", pattern="*", action=PermissionAction.ALLOW),
    PermissionRule(permission="external_directory", pattern="*", action=PermissionAction.ALLOW),
    PermissionRule(permission="doom_loop", pattern="*", action=PermissionAction.ALLOW),
)


def guard_to_rules(guard: Guard) -> list[PermissionRule]:
    """Build a session ruleset with unattended defaults and ``ask`` rules for protected globs.

    Uses ``ask`` rather than ``deny`` so attempted edits pause for user review
    with the diff attached instead of failing immediately.
    """
    base = list(UNATTENDED_RULES) if guard.unattended else []
    asks = [
        PermissionRule(permission="edit", pattern=pattern, action=PermissionAction.ASK)
        for pattern in guard.protected
    ]
    return [*base, *asks]


def subagent_rules(inherited: object, guard: Guard) -> list[PermissionRule]:
    """Append ``deny`` rules for protected patterns to a subagent's existing ruleset.

    Uses ``deny`` rather than ``ask`` so we do not weaken a subagent agent whose
    own policy already denies edits.
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
