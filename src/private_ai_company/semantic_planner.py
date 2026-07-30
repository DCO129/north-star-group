'''P0-1A CEO semantic planner: natural-language intent to validated planning decision.

This module implements the contract
``research/ai-company/contracts/20260724-p0-1a-ceo-semantic-planner-contract.md``.

Flow (planning only, never execution):

    natural-language intent
    -> candidate-command/v1
    -> Schema / Capability / Acceptance / Permission / Budget / Risk validation
    -> planning-decision/v1

The planner is injectable. Tests use a fake/stub planner; the runtime uses a
small deterministic rule spike (``RuleBasedPlanner``). When the spike cannot
reliably classify an intent it refuses to guess: it returns a candidate with no
registered capability, which the validator routes to ``blocked``.
'''

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Protocol, Sequence

from .budget import BudgetPolicy
from .contracts import has_errors, load_json
from .organization import load_organization_graph


INTENT_SCHEMA = 'ceo-intent-request/v1'
CANDIDATE_SCHEMA = 'candidate-command/v1'
DECISION_SCHEMA = 'planning-decision/v1'

PLANNER_ID = 'rule-based-planner'
PLANNER_VERSION = '0.1.0'

PERMISSION_LEVELS = ('L0', 'L1', 'L2', 'L3', 'L4')

TASK_ID_PATTERN = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$')

INTENT_ALLOWED_FIELDS = {'schema_version', 'instruction', 'task_id'}


class PlanningError(ValueError):
    '''Raised when an intent request fails schema validation.'''


class ValidationError(PlanningError):
    def __init__(self, field: str, message: str) -> None:
        super().__init__(f'{field}: {message}')
        self.field = field
        self.message = message


# --------------------------------------------------------------------------- #
# Data structures
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class IntentRequest:
    instruction: str
    schema_version: str = INTENT_SCHEMA
    task_id: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            'schema_version': self.schema_version,
            'instruction': self.instruction,
            'task_id': self.task_id,
        }


@dataclass(frozen=True)
class CandidateCommand:
    candidate_id: str
    objective: str
    schema_version: str = CANDIDATE_SCHEMA
    capabilities: tuple[str, ...] = ()
    acceptance_criteria: tuple[str, ...] = ()
    estimated_budget_fen: int = 0
    risk_level: str = 'L1'
    external_effect: bool = False
    reversible: bool = True
    task_id: str | None = None
    assumptions: tuple[str, ...] = ()
    uncertainties: tuple[str, ...] = ()
    planner: Mapping[str, str] = field(
        default_factory=lambda: {'id': PLANNER_ID, 'version': PLANNER_VERSION}
    )

    def to_dict(self) -> dict[str, object]:
        return {
            'schema_version': self.schema_version,
            'candidate_id': self.candidate_id,
            'task_id': self.task_id,
            'objective': self.objective,
            'capabilities': list(self.capabilities),
            'acceptance_criteria': list(self.acceptance_criteria),
            'estimated_budget_fen': self.estimated_budget_fen,
            'risk_level': self.risk_level,
            'external_effect': self.external_effect,
            'reversible': self.reversible,
            'assumptions': list(self.assumptions),
            'uncertainties': list(self.uncertainties),
            'planner': dict(self.planner),
        }


@dataclass(frozen=True)
class PlanningCheck:
    gate: str
    passed: bool
    code: str

    def to_dict(self) -> dict[str, object]:
        return {'id': self.gate, 'passed': self.passed, 'code': self.code}


@dataclass(frozen=True)
class PlanningDecision:
    status: str
    candidate: CandidateCommand
    checks: tuple[PlanningCheck, ...]
    request_sha256: str
    candidate_sha256: str
    blocking_codes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            'schema_version': DECISION_SCHEMA,
            'status': self.status,
            'candidate': self.candidate.to_dict(),
            'checks': [check.to_dict() for check in self.checks],
            'evidence': {
                'request_sha256': self.request_sha256,
                'candidate_sha256': self.candidate_sha256,
            },
            'blocking_codes': list(self.blocking_codes),
        }


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _sha256_canonical(obj: object) -> str:
    payload = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def _skipped_check(gate_id: str) -> PlanningCheck:
    return PlanningCheck(gate_id, False, 'gate-blocked-by-prior')


def validate_intent_request(data: object) -> IntentRequest:
    '''Validate the incoming ``ceo-intent-request/v1`` payload.

    The caller must not specify capability, budget, permission or status. Extra
    fields are rejected, never silently ignored.
    '''
    if not isinstance(data, dict):
        raise ValidationError('request', 'must be a JSON object')
    extra = set(data) - INTENT_ALLOWED_FIELDS
    if extra:
        raise ValidationError('request', f'unexpected fields: {", ".join(sorted(extra))}')
    if data.get('schema_version') != INTENT_SCHEMA:
        raise ValidationError('schema_version', f'must be {INTENT_SCHEMA!r}')

    instruction = data.get('instruction')
    if not isinstance(instruction, str):
        raise ValidationError('instruction', 'must be a string')
    instruction = instruction.strip()
    if not instruction:
        raise ValidationError('instruction', 'must be non-empty')
    if len(instruction) > 4000:
        raise ValidationError('instruction', 'maximum is 4000 characters')

    task_id = data.get('task_id')
    if task_id is not None:
        if not isinstance(task_id, str):
            raise ValidationError('task_id', 'must be a string or null')
        task_id = task_id.strip() or None
        if task_id is not None and not TASK_ID_PATTERN.fullmatch(task_id):
            raise ValidationError('task_id', 'must be a portable identifier')

    return IntentRequest(instruction=instruction, task_id=task_id)


def _validate_candidate_schema(candidate: CandidateCommand) -> str | None:
    if candidate.risk_level not in PERMISSION_LEVELS:
        return 'schema-candidate-invalid'
    if not isinstance(candidate.estimated_budget_fen, int) or isinstance(
        candidate.estimated_budget_fen, bool
    ):
        return 'schema-candidate-invalid'
    if candidate.estimated_budget_fen < 0:
        return 'schema-candidate-invalid'
    if not isinstance(candidate.external_effect, bool):
        return 'schema-candidate-invalid'
    if not isinstance(candidate.reversible, bool):
        return 'schema-candidate-invalid'
    if not isinstance(candidate.capabilities, (tuple, list)) or not all(
        isinstance(item, str) for item in candidate.capabilities
    ):
        return 'schema-candidate-invalid'
    if not isinstance(candidate.acceptance_criteria, (tuple, list)) or not all(
        isinstance(item, str) for item in candidate.acceptance_criteria
    ):
        return 'schema-candidate-invalid'
    return None


# --------------------------------------------------------------------------- #
# Planner (injectable)
# --------------------------------------------------------------------------- #
class SemanticPlanner(Protocol):
    def plan(self, instruction: str) -> CandidateCommand:
        '''Turn a natural-language instruction into a structured candidate.

        The planner must NOT execute anything. When it cannot reliably classify
        the intent it must refuse to guess: return a candidate without a
        registered capability so the validator can block it.
        '''
        ...


# Keyword buckets for the deterministic rule spike.
_EXTERNAL_TOKENS = (
    '发布', '外发', '发送', '邮件', '上传', '推送', '公开', '对外',
    'publish', 'upload', 'send', 'share', 'post', 'tweet',
)
_IRREVERSIBLE_TOKENS = (
    '永久删除', '删除', '销毁', '清空', '移除', '不可恢复', '撤消不了',
    'drop', 'delete', 'purge',
)
_PAID_TOKENS = (
    '付费', '购买', '花', '支出', '扣预算', '真实付费', '下单',
    'buy', 'purchase', 'paid',
)
_ACCOUNT_TOKENS = (
    '账号', '密码', '密钥', '登录', '认证', '验证码', 'oauth',
    'secret', 'token', 'credential',
)

_SAFE_PATTERNS: tuple[tuple[Sequence[str], str, str, Sequence[str]], ...] = (
    (
        ('检查清单', '清单', 'checklist', '核验表', '启动清单'),
        'task-orchestration',
        '生成可勾选的本地检查清单',
        ('产出本地检查清单文件', '清单项可逐项验证完成'),
    ),
    (
        ('研究', '调研', '检索', '收集资料', 'research', 'search'),
        'research',
        '完成本地研究并整理证据',
        ('检索证据已记录', '来源可追溯'),
    ),
    (
        ('审计', '核对', '审查', '验证', '质检', 'audit', 'verify'),
        'audit',
        '独立审计并给出结论',
        ('独立验证完成', '结论附证据'),
    ),
    (
        ('整理', '总结', '汇总', '生成文档', '起草', '写一份', '撰写'),
        'task-orchestration',
        '整理为本地交付文档',
        ('产出本地文档', '内容可复核'),
    ),
)

_VAGUE_ACCEPTANCE_TERMS = (
    '效果良好', '尽量', '尽可能', '差不多', '良好', '完善', '最好',
    '尽量完成', '大体', '差不多就行', '差不多了',
)

# P0-4 novel MVP vertical closure (contract §5): an explicitly local, free,
# reversible, L1 novel chapter request maps to exactly these four capabilities.
_NOVEL_TOKENS = (
    '小说', '章节', '章回', 'novel', 'chapter', '草稿', '写一章', '写小说',
)
_NOVEL_CAPABILITIES = (
    'novel-context', 'novel-draft', 'novel-quality', 'novel-artifact',
)
_NOVEL_OBJECTIVE = '基于已审校本地知识生成小说章节草稿'
_NOVEL_ACCEPTANCE = (
    '本地生成章节且未超出字符与约束边界',
    '质量门禁通过后才登记成品',
)


class RuleBasedPlanner:
    '''Local deterministic planning spike.

    Recognises a small set of local, reversible, free, L0/L1 intents and maps
    them to a registered capability. Anything it cannot classify safely is
    returned without a registered capability (the validator blocks it). It
    never invents capabilities, budgets, or external effects.
    '''

    def _detect(self, text: str, tokens: Sequence[str]) -> bool:
        lowered = text.lower()
        return any(token in lowered for token in tokens)

    def _match_safe(self, text: str) -> tuple[str, str, tuple[str, ...]] | None:
        for tokens, capability, objective, acceptance in _SAFE_PATTERNS:
            if any(token in text for token in tokens):
                return capability, objective, tuple(acceptance)
        return None

    def plan(self, instruction: str) -> CandidateCommand:
        text = (instruction or '').strip()

        external = self._detect(text, _EXTERNAL_TOKENS)
        irreversible = self._detect(text, _IRREVERSIBLE_TOKENS)
        paid = self._detect(text, _PAID_TOKENS)
        account = self._detect(text, _ACCOUNT_TOKENS)

        match = self._match_safe(text)
        risk_level = 'L2' if account else 'L1'

        if paid:
            # A paid action cannot be planned without owner budget authorization.
            return CandidateCommand(
                candidate_id=f'candidate-{uuid.uuid4().hex[:12]}',
                objective='未规划：含付费动作',
                capabilities=(),
                acceptance_criteria=(),
                estimated_budget_fen=0,
                risk_level=risk_level,
                external_effect=external,
                reversible=not irreversible,
                uncertainties=('paid action requires owner budget authorization',),
            )

        # P0-4 novel chapter slice: explicit local, free, reversible, L1. It only
        # applies when no reviewed safe pattern matched, so an explicit
        # checklist/research/audit intent that merely mentions a novel project
        # is not misclassified as a novel chapter request.
        if match is None and self._detect(text, _NOVEL_TOKENS):
            return CandidateCommand(
                candidate_id=f'candidate-{uuid.uuid4().hex[:12]}',
                objective=_NOVEL_OBJECTIVE,
                capabilities=_NOVEL_CAPABILITIES,
                acceptance_criteria=_NOVEL_ACCEPTANCE,
                estimated_budget_fen=0,
                risk_level='L2' if account else 'L1',
                external_effect=external,
                reversible=not irreversible,
            )

        if match is None:
            return CandidateCommand(
                candidate_id=f'candidate-{uuid.uuid4().hex[:12]}',
                objective='未规划：无法可靠分类',
                capabilities=(),
                acceptance_criteria=(),
                estimated_budget_fen=0,
                risk_level=risk_level,
                external_effect=external,
                reversible=not irreversible,
                uncertainties=('instruction does not match a known local capability',),
            )

        capability, objective, acceptance = match
        return CandidateCommand(
            candidate_id=f'candidate-{uuid.uuid4().hex[:12]}',
            objective=objective,
            capabilities=(capability,),
            acceptance_criteria=acceptance,
            estimated_budget_fen=0,
            risk_level=risk_level,
            external_effect=external,
            reversible=not irreversible,
        )


# --------------------------------------------------------------------------- #
# Budget truth provider
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class BudgetTruth:
    '''Answers whether budget truth is known and whether an amount is covered.

    For P0-1A free/local actions ``estimated_budget_fen`` is 0 and no budget
    truth is required. A paid action needs known truth; when unavailable the
    validator fails closed.
    '''

    available: bool
    total_fen: int = 0
    reserve_fen: int = 0

    def is_available(self) -> bool:
        return self.available

    def can_cover(self, amount_fen: int, category: str | None = None) -> bool:
        if not self.available:
            return False
        if amount_fen < 0:
            return False
        return amount_fen <= max(0, self.total_fen - self.reserve_fen)


# --------------------------------------------------------------------------- #
# Deterministic validator
# --------------------------------------------------------------------------- #
class DeterministicPlanningValidator:
    '''Runs the six ordered deterministic checks and produces a decision.

    Order: schema -> capability -> acceptance -> permission -> budget -> risk.
    A failing prior gate stops later gates from being faked as passed; they are
    recorded as ``gate-blocked-by-prior``.
    '''

    def __init__(
        self,
        capability_checker: Callable[[str], bool],
        budget_truth: BudgetTruth,
    ) -> None:
        self.capability_checker = capability_checker
        self.budget_truth = budget_truth

    def _check_schema(
        self, intent: IntentRequest, candidate: CandidateCommand
    ) -> PlanningCheck:
        if intent.schema_version != INTENT_SCHEMA or not intent.instruction:
            return PlanningCheck('schema', False, 'schema-intent-invalid')
        error = _validate_candidate_schema(candidate)
        if error is not None:
            return PlanningCheck('schema', False, error)
        return PlanningCheck('schema', True, 'schema-valid')

    def _check_capability(self, candidate: CandidateCommand) -> PlanningCheck:
        if not candidate.capabilities:
            return PlanningCheck('capability', False, 'capability-empty')
        for cap in candidate.capabilities:
            if not self.capability_checker(cap):
                return PlanningCheck('capability', False, 'capability-unregistered')
        return PlanningCheck('capability', True, 'capability-known')

    def _check_acceptance(self, candidate: CandidateCommand) -> PlanningCheck:
        criteria = candidate.acceptance_criteria
        if not criteria:
            return PlanningCheck('acceptance', False, 'acceptance-empty')
        for criterion in criteria:
            if not isinstance(criterion, str) or not criterion.strip():
                return PlanningCheck('acceptance', False, 'acceptance-indecidable')
            lowered = criterion.lower()
            if any(term in lowered for term in _VAGUE_ACCEPTANCE_TERMS):
                return PlanningCheck('acceptance', False, 'acceptance-indecidable')
        return PlanningCheck('acceptance', True, 'acceptance-decidable')

    def _check_permission(self, candidate: CandidateCommand) -> PlanningCheck:
        if candidate.risk_level not in ('L0', 'L1'):
            return PlanningCheck('permission', False, 'permission-level-exceeded')
        if candidate.external_effect:
            return PlanningCheck('permission', False, 'permission-external-effect')
        if not candidate.reversible:
            return PlanningCheck('permission', False, 'permission-irreversible')
        return PlanningCheck('permission', True, 'permission-within-bounds')

    def _check_budget(self, candidate: CandidateCommand) -> PlanningCheck:
        if candidate.estimated_budget_fen <= 0:
            return PlanningCheck('budget', True, 'budget-truth-ok')
        # A paid action needs budget truth; missing truth fails closed.
        if not self.budget_truth.is_available():
            return PlanningCheck('budget', False, 'budget-truth-unavailable')
        # Even with known truth, a real payment is out of P0-1A scope.
        return PlanningCheck('budget', False, 'budget-requires-payment')

    def _check_risk(self, candidate: CandidateCommand) -> PlanningCheck:
        if candidate.risk_level in ('L2', 'L3', 'L4'):
            return PlanningCheck('risk', False, 'risk-requires-owner')
        return PlanningCheck('risk', True, 'risk-within-authority')

    def validate(
        self, intent: IntentRequest, candidate: CandidateCommand
    ) -> PlanningDecision:
        checks: list[PlanningCheck] = [self._check_schema(intent, candidate)]
        if not checks[0].passed:
            for gate_id in ('capability', 'acceptance', 'permission', 'budget', 'risk'):
                checks.append(_skipped_check(gate_id))
        else:
            capability = self._check_capability(candidate)
            checks.append(capability)
            if not capability.passed:
                for gate_id in ('acceptance', 'permission', 'budget', 'risk'):
                    checks.append(_skipped_check(gate_id))
            else:
                acceptance = self._check_acceptance(candidate)
                checks.append(acceptance)
                if not acceptance.passed:
                    for gate_id in ('permission', 'budget', 'risk'):
                        checks.append(_skipped_check(gate_id))
                else:
                    permission = self._check_permission(candidate)
                    checks.append(permission)
                    if not permission.passed:
                        for gate_id in ('budget', 'risk'):
                            checks.append(_skipped_check(gate_id))
                    else:
                        budget = self._check_budget(candidate)
                        checks.append(budget)
                        if not budget.passed:
                            checks.append(_skipped_check('risk'))
                        else:
                            checks.append(self._check_risk(candidate))

        first_failure = next((check for check in checks if not check.passed), None)
        if first_failure is None:
            status = 'validated'
        elif first_failure.gate in ('schema', 'capability', 'acceptance'):
            # Input/capability/acceptance failures are hard blocks.
            status = 'blocked'
        elif first_failure.code == 'budget-truth-unavailable':
            # System truth (budget availability) is incomplete -> blocked, not
            # an owner-authorization request. Fail closed on missing truth.
            status = 'blocked'
        else:
            # Needs expanded authority / real payment / external irreversible
            # action / zero's own action.
            status = 'requires_owner'

        blocking_codes = tuple(
            check.code
            for check in checks
            if not check.passed and check.code != 'gate-blocked-by-prior'
        )

        return PlanningDecision(
            status=status,
            candidate=candidate,
            checks=tuple(checks),
            request_sha256=_sha256_canonical(intent.to_dict()),
            candidate_sha256=_sha256_canonical(candidate.to_dict()),
            blocking_codes=blocking_codes,
        )


# --------------------------------------------------------------------------- #
# Runtime planning gateway (no orchestrator, no side effects)
# --------------------------------------------------------------------------- #
class PlanningGateway(Protocol):
    def plan(self, intent: IntentRequest) -> PlanningDecision: ...


class RuntimePlanningGateway:
    '''Planning-only gateway backed by the organization graph and budget policy.

    Loads registered capabilities and budget truth. It never builds executors
    or an orchestrator, so planning cannot execute, create tasks, or write
    checkpoints.
    '''

    def __init__(self, group_root: Path, *, planner: SemanticPlanner | None = None) -> None:
        root = Path(group_root).resolve()
        manifest = load_json(root / 'group.manifest.json')
        graph, findings = load_organization_graph(root, manifest)
        if has_errors(findings):
            codes = ', '.join(
                item.code for item in findings if item.severity == 'error'
            )
            raise ValueError(f'GroupPack validation failed: {codes}')

        capabilities: set[str] = set()
        executive_caps = graph.executive.get('capabilities')
        if isinstance(executive_caps, list):
            capabilities.update(str(item) for item in executive_caps)
        for department in graph.departments.values():
            department_caps = department.get('capabilities')
            if isinstance(department_caps, list):
                capabilities.update(str(item) for item in department_caps)

        budget_data = load_json(root / 'shared/policies/rmb-4000-budget-policy.json')
        budget_truth = BudgetTruth(
            available=True,
            total_fen=int(budget_data['total_fen']),
            reserve_fen=int(budget_data['reserve_fen']),
        )

        self._capability_checker: Callable[[str], bool] = capabilities.__contains__
        self.planner: SemanticPlanner = planner or RuleBasedPlanner()
        self.validator = DeterministicPlanningValidator(
            self._capability_checker, budget_truth
        )
        self.registered_capabilities = capabilities

    def plan(self, intent: IntentRequest) -> PlanningDecision:
        candidate = self.planner.plan(intent.instruction)
        return self.validator.validate(intent, candidate)
