'''Independent deterministic acceptance checks for CEO task completion.'''

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AcceptanceDecision:
    passed: bool
    codes: tuple[str, ...]
    metrics: dict[str, int]


class IndependentAcceptanceValidator:
    '''Reject false completion without asking the executing model to self-grade.'''

    def __init__(self, *, minimum_confidence_milli: int = 500):
        if not 0 <= minimum_confidence_milli <= 1000:
            raise ValueError('minimum_confidence_milli must be between 0 and 1000.')
        self.minimum_confidence_milli = minimum_confidence_milli

    def validate(
        self,
        assignments: list[dict[str, object]],
        evidence: list[dict[str, object]],
        artifacts: list[dict[str, object]],
    ) -> AcceptanceDecision:
        codes: list[str] = []
        evidence_refs = {
            item.get('ref') for item in evidence if isinstance(item.get('ref'), str)
        }
        artifact_refs = {
            item.get('ref') for item in artifacts if isinstance(item.get('ref'), str)
        }
        if len(evidence_refs) != len(evidence):
            codes.append('acceptance-evidence-record-invalid')
        if len(artifact_refs) != len(artifacts):
            codes.append('acceptance-artifact-record-invalid')

        for assignment in assignments:
            assignment_id = str(assignment.get('assignment_id') or 'unknown')
            prefix = f'{assignment_id}:'
            if assignment.get('status') != 'completed':
                codes.append(prefix + 'assignment-not-completed')
                continue
            local_evidence = assignment.get('evidence_refs')
            local_artifacts = assignment.get('artifact_refs')
            if not isinstance(local_evidence, list) or not local_evidence:
                codes.append(prefix + 'evidence-missing')
            elif any(ref not in evidence_refs for ref in local_evidence):
                codes.append(prefix + 'evidence-reference-invalid')
            if not isinstance(local_artifacts, list) or not local_artifacts:
                codes.append(prefix + 'artifact-missing')
            elif any(ref not in artifact_refs for ref in local_artifacts):
                codes.append(prefix + 'artifact-reference-invalid')
            confidence = assignment.get('confidence_milli')
            if (
                not isinstance(confidence, int)
                or isinstance(confidence, bool)
                or confidence < self.minimum_confidence_milli
            ):
                codes.append(prefix + 'confidence-below-threshold')
            unresolved = assignment.get('unresolved_codes')
            if isinstance(unresolved, list) and unresolved:
                codes.append(prefix + 'unresolved-present')

        unique_codes = tuple(dict.fromkeys(codes))
        return AcceptanceDecision(
            passed=not unique_codes,
            codes=unique_codes,
            metrics={
                'assignments_checked': len(assignments),
                'evidence_checked': len(evidence),
                'artifacts_checked': len(artifacts),
            },
        )
