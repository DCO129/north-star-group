'''Deterministic L0-L4 action authorization for group tasks.'''

from __future__ import annotations

from dataclasses import dataclass


LEVELS = {'L0': 0, 'L1': 1, 'L2': 2, 'L3': 3, 'L4': 4}


@dataclass(frozen=True)
class ActionProposal:
    action: str
    level: str
    resource: str
    reversible: bool
    external_effect: bool
    cost_fen: int = 0
    approval_ticket: str | None = None


@dataclass(frozen=True)
class AuthorizationDecision:
    allowed: bool
    code: str


@dataclass(frozen=True)
class PermissionPolicy:
    maximum_level: str
    allowed_actions: frozenset[str]
    allowed_resource_prefixes: tuple[str, ...]

    def authorize(self, proposal: ActionProposal) -> AuthorizationDecision:
        if self.maximum_level not in LEVELS or proposal.level not in LEVELS:
            return AuthorizationDecision(False, 'permission-level-invalid')
        if LEVELS[proposal.level] > LEVELS[self.maximum_level]:
            return AuthorizationDecision(False, 'permission-level-exceeded')
        if proposal.action not in self.allowed_actions:
            return AuthorizationDecision(False, 'permission-action-denied')
        if not any(
            proposal.resource.startswith(prefix)
            for prefix in self.allowed_resource_prefixes
        ):
            return AuthorizationDecision(False, 'permission-resource-denied')
        if proposal.cost_fen < 0:
            return AuthorizationDecision(False, 'permission-cost-invalid')
        if proposal.level in {'L0', 'L1'} and (
            proposal.external_effect or proposal.cost_fen > 0
        ):
            return AuthorizationDecision(False, 'permission-side-effect-denied')
        if proposal.level == 'L2' and (
            proposal.external_effect or not proposal.reversible
        ):
            return AuthorizationDecision(False, 'permission-l2-boundary')
        if proposal.level == 'L4' and not proposal.approval_ticket:
            return AuthorizationDecision(False, 'permission-human-approval-required')
        return AuthorizationDecision(True, 'permission-allowed')
