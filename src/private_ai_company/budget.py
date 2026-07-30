'''CNY-first operating budget policy and append-only usage ledger.'''

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING
from pathlib import Path

from .timebase import authoritative_timestamp


@dataclass(frozen=True)
class BudgetPolicy:
    total_fen: int
    category_caps_fen: dict[str, int]
    reserve_fen: int
    currency: str = 'CNY'
    warning_ratio_milli: int = 800

    def validate(self) -> None:
        if self.currency != 'CNY':
            raise ValueError('The operating policy must use CNY as its base currency.')
        if self.total_fen <= 0 or self.reserve_fen < 0:
            raise ValueError('Budget totals must be non-negative integers.')
        if any(value < 0 for value in self.category_caps_fen.values()):
            raise ValueError('Category caps must be non-negative.')
        if not 1 <= self.warning_ratio_milli <= 1000:
            raise ValueError('warning_ratio_milli must be between 1 and 1000.')
        allocated = sum(self.category_caps_fen.values()) + self.reserve_fen
        if allocated != self.total_fen:
            raise ValueError('Category caps plus reserve must equal total_fen.')


@dataclass(frozen=True)
class ModelRate:
    provider: str
    model: str
    input_cny_per_million: Decimal
    output_cny_per_million: Decimal

    def estimate_fen(self, input_tokens: int, output_tokens: int) -> int:
        if input_tokens < 0 or output_tokens < 0:
            raise ValueError('Token counts must not be negative.')
        cost_cny = (
            Decimal(input_tokens) * self.input_cny_per_million
            + Decimal(output_tokens) * self.output_cny_per_million
        ) / Decimal(1_000_000)
        return int((cost_cny * 100).to_integral_value(rounding=ROUND_CEILING))


@dataclass(frozen=True)
class UsageRecord:
    task_id: str
    category: str
    amount_fen: int
    source: str
    note: str = ''

    def to_dict(self) -> dict[str, object]:
        if not self.task_id.strip() or not self.category.strip():
            raise ValueError('task_id and category must be non-empty.')
        if self.amount_fen < 0:
            raise ValueError('amount_fen must not be negative.')
        if not self.source.strip():
            raise ValueError('source must be non-empty.')
        return {
            'schema_version': 'budget-usage/v0',
            'timestamp': authoritative_timestamp(),
            'task_id': self.task_id,
            'category': self.category,
            'amount_fen': self.amount_fen,
            'currency': 'CNY',
            'source': self.source,
            'note': self.note,
        }


@dataclass(frozen=True)
class BudgetDecision:
    allowed: bool
    code: str
    category: str
    estimated_amount_fen: int
    projected_category_fen: int
    category_cap_fen: int
    projected_operating_fen: int
    operating_cap_fen: int
    warning: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            'allowed': self.allowed,
            'code': self.code,
            'category': self.category,
            'estimated_amount_fen': self.estimated_amount_fen,
            'projected_category_fen': self.projected_category_fen,
            'category_cap_fen': self.category_cap_fen,
            'projected_operating_fen': self.projected_operating_fen,
            'operating_cap_fen': self.operating_cap_fen,
            'warning': self.warning,
            'currency': 'CNY',
        }


class BudgetLedger:
    def __init__(self, path: Path, policy: BudgetPolicy):
        self.path = path
        self.policy = policy
        self.policy.validate()

    def records(self) -> list[dict[str, object]]:
        if not self.path.exists():
            return []
        records = []
        for line in self.path.read_text(encoding='utf-8').splitlines():
            if line.strip():
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError('Budget ledger records must be objects.')
                records.append(value)
        return records

    def spent_fen(self, category: str | None = None) -> int:
        return sum(
            int(item['amount_fen'])
            for item in self.records()
            if category is None or item.get('category') == category
        )

    def preflight(self, category: str, estimated_amount_fen: int) -> BudgetDecision:
        operating_cap = self.policy.total_fen - self.policy.reserve_fen
        if estimated_amount_fen < 0:
            return BudgetDecision(
                False, 'budget-estimate-invalid', category, estimated_amount_fen,
                self.spent_fen(category), self.policy.category_caps_fen.get(category, 0),
                self.spent_fen(), operating_cap,
            )
        if category not in self.policy.category_caps_fen:
            return BudgetDecision(
                False, 'budget-category-unknown', category, estimated_amount_fen,
                0, 0, self.spent_fen() + estimated_amount_fen, operating_cap,
            )
        category_cap = self.policy.category_caps_fen[category]
        projected_category = self.spent_fen(category) + estimated_amount_fen
        projected_operating = self.spent_fen() + estimated_amount_fen
        if projected_category > category_cap:
            return BudgetDecision(
                False, 'budget-category-hard-stop', category, estimated_amount_fen,
                projected_category, category_cap, projected_operating, operating_cap,
            )
        if projected_operating > operating_cap:
            return BudgetDecision(
                False, 'budget-reserve-hard-stop', category, estimated_amount_fen,
                projected_category, category_cap, projected_operating, operating_cap,
            )
        threshold = self.policy.warning_ratio_milli
        warning = (
            category_cap > 0
            and projected_category * 1000 >= category_cap * threshold
        ) or (
            operating_cap > 0
            and projected_operating * 1000 >= operating_cap * threshold
        )
        return BudgetDecision(
            True, 'budget-warning' if warning else 'budget-approved', category,
            estimated_amount_fen, projected_category, category_cap,
            projected_operating, operating_cap, warning,
        )

    def append(self, record: UsageRecord) -> dict[str, object]:
        value = record.to_dict()
        decision = self.preflight(record.category, record.amount_fen)
        if not decision.allowed:
            if decision.code == 'budget-category-unknown':
                raise ValueError(f'Unknown budget category {record.category!r}.')
            if decision.code == 'budget-category-hard-stop':
                raise ValueError(
                    f'Budget cap exceeded for category {record.category!r}.'
                )
            if decision.code == 'budget-reserve-hard-stop':
                raise ValueError('Spend would consume the protected reserve.')
            raise ValueError('Budget usage record failed validation.')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
        )
        with self.path.open('a', encoding='utf-8', newline='\n') as handle:
            handle.write(encoded + '\n')
            handle.flush()
            os.fsync(handle.fileno())
        return value
