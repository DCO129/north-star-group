'''Provider-neutral CompanyPack and model compatibility evaluation.'''

from __future__ import annotations

from dataclasses import dataclass

from .contracts import Finding

TOOL_RANK = {'none': 0, 'single': 1, 'parallel': 2}
STRUCTURED_RANK = {'none': 0, 'json': 1, 'tested_subset': 2, 'json_schema': 3}
BOOLEAN_CAPABILITIES = ('text', 'streaming', 'vision', 'audio_input', 'reasoning_controls')


@dataclass(frozen=True)
class CompatibilityReport:
    status: str
    findings: list[Finding]

    @property
    def compatible(self) -> bool:
        return self.status != 'incompatible'

    def to_dict(self) -> dict[str, object]:
        return {'status': self.status, 'compatible': self.compatible, 'findings': [item.to_dict() for item in self.findings]}


def evaluate_compatibility(company: dict[str, object], model: dict[str, object]) -> CompatibilityReport:
    findings: list[Finding] = []
    requirements = company.get('model_requirements', {})
    required = requirements.get('required', {}) if isinstance(requirements, dict) else {}
    optional = requirements.get('optional', []) if isinstance(requirements, dict) else []
    capabilities = model.get('capabilities', {})
    certification = model.get('certification', {})
    required = required if isinstance(required, dict) else {}
    capabilities = capabilities if isinstance(capabilities, dict) else {}
    status = certification.get('status') if isinstance(certification, dict) else 'untested'
    if status in {'failed', 'quarantined'}:
        findings.append(Finding('error', 'certification_block', f'Model certification status is {status}.', 'certification.status'))
    elif status == 'untested':
        findings.append(Finding('warning', 'certification_untested', 'Model has not passed behavioral certification.', 'certification.status'))

    for key in BOOLEAN_CAPABILITIES:
        if required.get(key) is True and capabilities.get(key) is not True:
            findings.append(Finding('error', 'missing_capability', f'Required capability {key} is unavailable.', f'capabilities.{key}'))
    required_tools = required.get('tools')
    if isinstance(required_tools, str) and TOOL_RANK.get(str(capabilities.get('tools')), -1) < TOOL_RANK.get(required_tools, 99):
        findings.append(Finding('error', 'insufficient_tools', f'Tools require {required_tools}.', 'capabilities.tools'))
    required_structured = required.get('structured_output')
    if isinstance(required_structured, str) and STRUCTURED_RANK.get(str(capabilities.get('structured_output')), -1) < STRUCTURED_RANK.get(required_structured, 99):
        findings.append(Finding('error', 'insufficient_structured_output', f'Structured output requires {required_structured}.', 'capabilities.structured_output'))
    minimum_context = required.get('context_tokens')
    if isinstance(minimum_context, int) and capabilities.get('context_tokens', 0) < minimum_context:
        findings.append(Finding('error', 'insufficient_context', f'At least {minimum_context} context tokens are required.', 'capabilities.context_tokens'))
    for key in optional if isinstance(optional, list) else []:
        if isinstance(key, str) and not capabilities.get(key):
            findings.append(Finding('warning', 'optional_capability_missing', f'Optional capability {key} is unavailable.', f'capabilities.{key}'))
    if any(item.severity == 'error' for item in findings):
        result = 'incompatible'
    elif findings:
        result = 'compatible_with_degradation'
    else:
        result = 'compatible'
    return CompatibilityReport(result, findings)
