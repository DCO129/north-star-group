'''Validated provider-neutral work products returned by departments.'''

from __future__ import annotations

from typing import Any


RESEARCH_DECISION_SCHEMA_VERSION = 'research-decision/v0'


def _non_empty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def validate_research_decision(value: object) -> list[str]:
    '''Return stable error codes for an invalid research decision artifact.'''
    if not isinstance(value, dict):
        return ['research-decision-not-object']
    errors: list[str] = []
    if value.get('schema_version') != RESEARCH_DECISION_SCHEMA_VERSION:
        errors.append('research-decision-schema')
    if not _non_empty_string(value.get('objective')):
        errors.append('research-decision-objective')
    if value.get('decision_status') not in {'proposed', 'blocked', 'rejected'}:
        errors.append('research-decision-status')

    recommendation = value.get('recommendation')
    if not isinstance(recommendation, dict):
        errors.append('research-decision-recommendation')
    else:
        if not _non_empty_string(recommendation.get('decision')):
            errors.append('research-decision-recommendation-decision')
        if not _non_empty_string(recommendation.get('rationale')):
            errors.append('research-decision-recommendation-rationale')

    evidence = value.get('evidence')
    if not isinstance(evidence, list) or not evidence:
        errors.append('research-decision-evidence')
    else:
        for item in evidence:
            if not isinstance(item, dict):
                errors.append('research-decision-evidence-item')
                break
            if not all(
                _non_empty_string(item.get(key))
                for key in ('source_ref', 'claim')
            ):
                errors.append('research-decision-evidence-item')
                break
            confidence = item.get('confidence_milli')
            if (
                not isinstance(confidence, int)
                or isinstance(confidence, bool)
                or not 0 <= confidence <= 1000
            ):
                errors.append('research-decision-evidence-confidence')
                break

    for key in ('alternatives', 'risks', 'assumptions', 'unresolved'):
        items = value.get(key)
        if not isinstance(items, list) or not all(
            _non_empty_string(item) for item in items
        ):
            errors.append(f'research-decision-{key}')
    return list(dict.fromkeys(errors))


def research_decision_json_contract() -> dict[str, Any]:
    '''Compact contract embedded in model prompts and golden tasks.'''
    return {
        'schema_version': RESEARCH_DECISION_SCHEMA_VERSION,
        'objective': 'non-empty string',
        'decision_status': 'proposed|blocked|rejected',
        'recommendation': {
            'decision': 'non-empty string',
            'rationale': 'non-empty string',
        },
        'evidence': [{
            'source_ref': 'traceable source identifier',
            'claim': 'claim supported by the source',
            'confidence_milli': 'integer 0..1000',
        }],
        'alternatives': ['string'],
        'risks': ['string'],
        'assumptions': ['string'],
        'unresolved': ['string'],
    }
