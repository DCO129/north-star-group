'''Deterministic Research-to-Action business executors for the first live company run.'''

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterable, Mapping
from urllib.parse import urlsplit

from .budget import BudgetLedger
from .executors import (
    DepartmentExecutor, DepartmentResult, ExecutionRequest,
    TemplateDepartmentExecutor,
)
from .organization import OrganizationGraph
from .permissions import PermissionPolicy
from .novel_mvp import NovelBusinessExecutor
from .research_browser import ResearchBrowserConfig, register_research_browser_tool
from .task_ledger import TaskLedger
from .tools import (
    ControlledToolDispatcher, ToolAccessPolicy, ToolRegistry, ToolRequest,
)
from .trace import TraceSink


WEB_SOURCE_PREFIX = 'web-source:'

PROMPT_INJECTION_PATTERNS = (
    'ignore previous', 'ignore all previous', 'system prompt',
    'developer message', 'reveal your prompt', '忽略以上', '忽略此前',
    '系统提示词', '开发者消息', '泄露提示词',
)
PRIVILEGE_ESCALATION_PATTERNS = (
    'bypass approval', 'disable audit', 'export credentials', 'read secrets',
    '跳过审批', '绕过审批', '关闭审计', '导出凭据', '读取密钥',
)


def _chrome_executable() -> str | None:
    candidates = (
        Path(r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'),
        Path(r'C:\Program Files\Microsoft\Edge\Application\msedge.exe'),
        Path(r'C:\Program Files\Google\Chrome\Application\chrome.exe'),
        Path(r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe'),
        Path.home() / 'AppData/Local/Google/Chrome/Application/chrome.exe',
    )
    return next((str(path) for path in candidates if path.is_file()), None)


def _web_sources(input_refs: Iterable[str]) -> list[str]:
    sources: list[str] = []
    for ref in input_refs:
        if ref.startswith(WEB_SOURCE_PREFIX):
            value = ref[len(WEB_SOURCE_PREFIX):].strip()
            if value and value not in sources:
                sources.append(value)
    return sources


def _sentences(text: str, *, maximum: int = 4) -> list[str]:
    parts = [
        item.strip() for item in re.split(r'(?<=[。！？.!?])\s+', text.strip())
        if item.strip()
    ]
    if not parts and text.strip():
        parts = [text.strip()]
    return [item[:360] for item in parts[:maximum]]


def _comparison_tokens(text: str) -> set[str]:
    lowered = text.casefold()
    tokens = {
        token for token in re.findall(r'[a-z][a-z0-9_-]{2,}', lowered)
        if not token.isdigit()
    }
    for segment in re.findall(r'[\u4e00-\u9fff]{2,}', lowered):
        tokens.update(segment[index:index + 2] for index in range(len(segment) - 1))
    return tokens - {
        'the', 'and', 'for', 'with', 'that', 'this', 'from', 'https',
        'about', 'are', 'available', 'automatic', 'based', 'using', 'you',
        '可以', '一个', '以及', '进行', '来源', '研究', '内容', '我们',
    }


def _compare_sources(source_records: list[dict[str, object]]) -> dict[str, object]:
    hosts = {urlsplit(str(item.get('source_url') or '')).hostname for item in source_records}
    hosts.discard(None)
    hashes = {str(item.get('content_sha256')) for item in source_records if item.get('content_sha256')}
    token_sets = [
        _comparison_tokens(str(item.get('title') or '') + ' ' + str(item.get('summary') or ''))
        for item in source_records
    ]
    shared_terms = sorted(set.intersection(*token_sets))[:8] if token_sets else []
    gaps: list[str] = []
    if len(source_records) < 2:
        gaps.append('当前只有一个来源，无法完成独立交叉验证。')
    if len(hosts) < len(source_records):
        gaps.append('部分来源属于同一域名，独立性不足。')
    if len(source_records) > 1 and not shared_terms:
        gaps.append('不同来源缺少共同主题词，不能宣称形成严格共识。')
    return {
        'schema_version': 'source-comparison/v0',
        'source_count': len(source_records), 'independent_host_count': len(hosts),
        'unique_content_count': len(hashes), 'shared_terms': shared_terms,
        'conflicts': [], 'evidence_gaps': gaps,
        'conflict_status': 'no-explicit-conflict-detected',
        'method': 'deterministic-topic-overlap-v0',
    }


def _dependency_payloads(
    graph: OrganizationGraph,
    dependency_results: Mapping[str, Mapping[str, object]] | None,
) -> list[dict[str, object]]:
    payloads: list[dict[str, object]] = []
    for result in (dependency_results or {}).values():
        records = result.get('artifact_records', [])
        if not isinstance(records, list):
            continue
        for record in records:
            if not isinstance(record, dict) or not isinstance(record.get('path'), str):
                continue
            target = (graph.root / str(record['path'])).resolve()
            target.relative_to(graph.root)
            if not target.is_file():
                continue
            content = json.loads(target.read_text(encoding='utf-8'))
            if isinstance(content, dict) and isinstance(content.get('payload'), dict):
                payloads.append(dict(content['payload']))
    return payloads


def evaluate_business_artifacts(
    research: Mapping[str, object] | None,
    plan: Mapping[str, object] | None,
) -> dict[str, object]:
    '''Independently evaluate persisted Research and Operations artifacts.'''
    research_data = dict(research or {})
    plan_data = dict(plan or {})
    raw_sources = research_data.get('source_records', [])
    source_records = [item for item in raw_sources if isinstance(item, dict)] if isinstance(raw_sources, list) else []
    if not source_records and research_data.get('source_url'):
        source_records = [research_data]
    expected_sources = int(research_data.get('expected_source_count', len(source_records)) or 0)
    raw_items = plan_data.get('work_items', [])
    work_items = [item for item in raw_items if isinstance(item, dict)] if isinstance(raw_items, list) else []
    research_text = ' '.join(str(research_data.get(key) or '') for key in (
        'title', 'conclusion', 'summary', 'key_evidence',
    )).casefold() + ' ' + ' '.join(
        str(item.get(key) or '') for item in source_records
        for key in ('title', 'conclusion', 'summary', 'key_evidence')
    ).casefold()
    plan_text = ' '.join(
        str(item.get(key) or '')
        for item in work_items
        for key in ('action', 'dependency', 'completion_gate')
    ).casefold()
    claimed_complete = [
        item for item in work_items
        if str(item.get('status') or '').casefold() in {'completed', 'complete', 'done'}
    ]
    checks = {
        'source_present': bool(source_records),
        'source_hash_present': bool(source_records) and all(item.get('content_sha256') for item in source_records),
        'evidence_present': bool(source_records) and all(item.get('key_evidence') for item in source_records),
        'all_requested_sources_retrieved': len(source_records) == expected_sources,
        'source_independence': len(source_records) <= 1 or (
            len({item.get('content_sha256') for item in source_records}) == len(source_records)
            and len({urlsplit(str(item.get('source_url') or '')).hostname for item in source_records}) == len(source_records)
        ),
        'conflict_analysis_present': len(source_records) <= 1 or isinstance(research_data.get('comparison'), dict),
        'unresolved_conflicts_absent': not (
            isinstance(research_data.get('comparison'), dict)
            and research_data['comparison'].get('conflicts')
        ),
        'work_items_present': bool(work_items),
        'owners_present': bool(work_items) and all(item.get('owner') for item in work_items),
        'completion_gates_present': bool(work_items) and all(
            item.get('completion_gate') for item in work_items
        ),
        'within_current_tool_budget': int(research_data.get('tool_cost_fen', 0) or 0) <= max(1, expected_sources),
        'prompt_injection_absent': not any(
            pattern in research_text for pattern in PROMPT_INJECTION_PATTERNS
        ),
        'privilege_escalation_absent': not any(
            pattern in plan_text for pattern in PRIVILEGE_ESCALATION_PATTERNS
        ) and all(
            str(item.get('requested_permission_level') or 'L0') not in {'L3', 'L4'}
            for item in work_items
        ),
        'no_unverified_completion_claims': all(
            bool(item.get('completion_evidence')) for item in claimed_complete
        ),
    }
    failed = [name for name, passed in checks.items() if not passed]
    return {
        'schema_version': 'business-acceptance/v0',
        'verdict': 'pass' if not failed else 'blocked',
        'checks': checks,
        'passed_checks': [name for name, passed in checks.items() if passed],
        'blocked_reasons': failed,
    }


class ResearchBusinessExecutor:
    department_id = 'example.research'

    def __init__(
        self,
        graph: OrganizationGraph,
        ledger: TaskLedger,
        budget_ledger: BudgetLedger,
        fallback: DepartmentExecutor,
    ) -> None:
        self.graph = graph
        self.ledger = ledger
        self.budget_ledger = budget_ledger
        self.fallback = fallback
        self.trace_sink: TraceSink | None = None

    def set_trace_sink(self, trace_sink: TraceSink) -> None:
        self.trace_sink = trace_sink

    def estimate_cost_fen(self, request: ExecutionRequest) -> int:
        return 0

    def execute(self, request: ExecutionRequest) -> DepartmentResult:
        source_urls = _web_sources(request.input_refs)
        if not source_urls:
            return self.fallback.execute(request)
        hosts = tuple(sorted({
            str(urlsplit(source_url).hostname) for source_url in source_urls
            if urlsplit(source_url).hostname
        }))
        if not hosts:
            raise ValueError('Research source URL is missing a host.')
        config = ResearchBrowserConfig(
            allowed_hosts=hosts, executable_path=_chrome_executable(),
        )
        registry = ToolRegistry()
        register_research_browser_tool(registry, config)
        policy = ToolAccessPolicy(
            department_id=self.department_id,
            allowed_tool_ids=frozenset({'research-browser-read'}),
            capabilities=frozenset({'research'}),
            permission_policy=PermissionPolicy(
                maximum_level='L3',
                allowed_actions=frozenset({'read-public-web'}),
                allowed_resource_prefixes=config.resource_prefixes,
            ),
        )
        dispatcher = ControlledToolDispatcher(
            registry, {self.department_id: policy}, self.ledger,
            self.budget_ledger, self.trace_sink,
        )
        source_records: list[dict[str, object]] = []
        failed_sources: list[dict[str, str]] = []
        tool_cost_fen = 0
        for source_url in source_urls:
            tool_result = dispatcher.dispatch(ToolRequest(
                task_id=request.task_id, department_id=self.department_id,
                tool_id='research-browser-read', resource=source_url,
                arguments={'max_text_chars': 12_000}, estimated_cost_fen=1,
                trace_context=request.trace_context,
            ))
            tool_cost_fen += tool_result.charged_cost_fen
            if tool_result.status != 'completed':
                failed_sources.append({'source_url': source_url, 'code': tool_result.code})
                continue
            source = dict(tool_result.output)
            text = str(source.get('text') or '').strip()
            evidence_sentences = _sentences(text)
            source_records.append({
                'source_url': source.get('source_url'), 'title': source.get('title'),
                'conclusion': evidence_sentences[0] if evidence_sentences else '来源已读取，但未形成结论。',
                'summary': text[:1200], 'key_evidence': evidence_sentences,
                'content_sha256': source.get('content_sha256'),
                'retrieved_at': source.get('retrieved_at'), 'text_chars': source.get('text_chars'),
            })
        if not source_records:
            return DepartmentResult(
                status='blocked', outcome_code='research-source-unavailable',
                evidence=[], artifacts=[],
                metrics={'tool_cost_fen': tool_cost_fen, 'sources': 0},
                confidence_milli=0,
                unresolved_codes=[item['code'] for item in failed_sources],
            )
        comparison = _compare_sources(source_records)
        first = source_records[0]
        evidence_sentences = [
            sentence for item in source_records
            for sentence in item.get('key_evidence', [])
        ][:12]
        host_count = comparison['independent_host_count']
        gap_count = len(comparison['evidence_gaps'])
        conclusion = f'已读取 {len(source_records)} 个来源，覆盖 {host_count} 个独立域名；证据缺口 {gap_count} 项。'
        artifact = {
            'schema_version': 'research-brief/v0',
            'objective': request.objective,
            'source_url': first.get('source_url'),
            'source_urls': [item.get('source_url') for item in source_records],
            'source_records': source_records,
            'expected_source_count': len(source_urls),
            'failed_sources': failed_sources,
            'title': first.get('title'),
            'conclusion': conclusion,
            'summary': '\n\n'.join(str(item.get('summary') or '') for item in source_records)[:3600],
            'key_evidence': evidence_sentences,
            'content_sha256': first.get('content_sha256'),
            'retrieved_at': first.get('retrieved_at'),
            'tool_cost_fen': tool_cost_fen,
            'comparison': comparison,
        }
        evidence = {
            'schema_version': 'research-evidence/v0',
            'sources': source_records,
            'comparison': comparison,
            'failed_sources': failed_sources,
        }
        return DepartmentResult(
            status='completed', outcome_code='multi-source-research-brief-ready',
            evidence=[evidence], artifacts=[artifact],
            metrics={
                'sources': len(source_records), 'evidence_items': len(evidence_sentences),
                'tool_cost_fen': tool_cost_fen, 'failed_sources': len(failed_sources),
            },
            confidence_milli=900 if not failed_sources else 600, unresolved_codes=[],
        )


class OperationsBusinessExecutor:
    department_id = 'example.operations'

    def __init__(
        self, graph: OrganizationGraph, fallback: DepartmentExecutor,
    ) -> None:
        self.graph = graph
        self.fallback = fallback

    def estimate_cost_fen(self, request: ExecutionRequest) -> int:
        return 0

    def execute(self, request: ExecutionRequest) -> DepartmentResult:
        payloads = _dependency_payloads(self.graph, request.dependency_results)
        research = next(
            (item for item in payloads if item.get('schema_version') == 'research-brief/v0'),
            None,
        )
        if research is None:
            return self.fallback.execute(request)
        comparison = research.get('comparison') if isinstance(research.get('comparison'), dict) else {}
        raw_source_records = research.get('source_records', [])
        source_count = len(raw_source_records) if isinstance(raw_source_records, list) else 1
        conflicts = comparison.get('conflicts', []) if isinstance(comparison, dict) else []
        gaps = comparison.get('evidence_gaps', []) if isinstance(comparison, dict) else []
        followup_action = (
            '逐项解决来源冲突，形成带证据权重的裁决记录' if conflicts else
            '补齐证据缺口并重新执行交叉验证' if gaps else
            '冻结多来源证据矩阵并建立可复用决策模板'
        )
        work_items = [
            {
                'id': 'OP-01', 'owner': 'Operations',
                'action': f'把 {source_count} 个来源的 Research 结论转成一页业务决策卡',
                'dependency': 'research-brief/v0',
                'completion_gate': '结论、证据哈希、来源链接均可追溯',
                'kpi': '决策卡字段完整率 100%',
            },
            {
                'id': 'OP-02', 'owner': 'Research',
                'action': followup_action,
                'dependency': 'OP-01',
                'completion_gate': '新增来源均通过 HTTPS、公网地址与内容抽取检查',
                'kpi': '未裁决冲突为 0，关键证据缺口有明确负责人',
            },
            {
                'id': 'OP-03', 'owner': 'Operations',
                'action': '按证据强度把建议拆成可执行、待验证、禁止执行三类',
                'dependency': 'OP-02',
                'completion_gate': '每条建议都有负责人、门槛和回滚条件',
                'kpi': '无责任人行动项为 0',
            },
            {
                'id': 'OP-04', 'owner': 'Audit',
                'action': '独立复核来源、哈希、行动项和预算边界',
                'dependency': 'OP-03',
                'completion_gate': '审计 verdict 为 pass 或明确列出阻塞原因',
                'kpi': '关键检查覆盖率 100%',
            },
        ]
        artifact = {
            'schema_version': 'execution-plan/v0',
            'objective': request.objective,
            'research_conclusion': research.get('conclusion'),
            'source_url': research.get('source_url'),
            'source_urls': research.get('source_urls', []),
            'source_comparison': comparison,
            'work_items': work_items,
            'immediate_next_action': work_items[0]['action'],
        }
        evidence = {
            'schema_version': 'operations-evidence/v0',
            'research_sha256': research.get('content_sha256'),
            'source_count': source_count,
            'planned_items': len(work_items),
            'owners': sorted({str(item['owner']) for item in work_items}),
        }
        return DepartmentResult(
            status='completed', outcome_code='execution-plan-ready',
            evidence=[evidence], artifacts=[artifact],
            metrics={'work_items': len(work_items)},
            confidence_milli=880, unresolved_codes=[],
        )


class AuditBusinessExecutor:
    department_id = 'example.audit'

    def __init__(self, graph: OrganizationGraph) -> None:
        self.graph = graph

    def estimate_cost_fen(self, request: ExecutionRequest) -> int:
        return 0

    def execute(self, request: ExecutionRequest) -> DepartmentResult:
        payloads = _dependency_payloads(self.graph, request.dependency_results)
        research = next(
            (item for item in payloads if item.get('schema_version') == 'research-brief/v0'),
            None,
        )
        plan = next(
            (item for item in payloads if item.get('schema_version') == 'execution-plan/v0'),
            None,
        )
        acceptance = evaluate_business_artifacts(research, plan)
        checks = dict(acceptance['checks'])
        failed = list(acceptance['blocked_reasons'])
        verdict = str(acceptance['verdict'])
        comparison = research.get('comparison') if isinstance(research, dict) and isinstance(research.get('comparison'), dict) else {}
        raw_sources = research.get('source_records', []) if isinstance(research, dict) else []
        source_count = len(raw_sources) if isinstance(raw_sources, list) and raw_sources else int(bool(research))
        risks = ['行动方案为确定性首版，需要真实业务反馈后再校准 KPI。']
        if source_count < 2:
            risks.insert(0, '当前只有一个公开来源，尚未完成多来源交叉验证。')
        risks.extend(str(item) for item in comparison.get('evidence_gaps', []) if item)
        risks.extend(f'待裁决冲突：{item}' for item in comparison.get('conflicts', []) if item)
        artifact = {
            'schema_version': 'audit-report/v0',
            'verdict': verdict,
            'passed_checks': [name for name, passed in checks.items() if passed],
            'source_count': source_count,
            'source_comparison': comparison,
            'risks': risks,
            'blocked_reasons': failed,
            'recommended_next_action': (
                plan.get('immediate_next_action') if isinstance(plan, dict) else None
            ),
        }
        evidence = {
            'schema_version': 'audit-evidence/v0',
            'checks': checks,
            'verdict': verdict,
        }
        return DepartmentResult(
            status='completed' if not failed else 'blocked',
            outcome_code='audit-passed' if not failed else 'audit-blocked',
            evidence=[evidence], artifacts=[artifact],
            metrics={
                'checks': len(checks), 'checks_passed': sum(checks.values()),
                'sources': source_count,
            },
            confidence_milli=930 if not failed else 400,
            unresolved_codes=[] if not failed else failed,
        )


def build_business_executors(
    graph: OrganizationGraph,
    *,
    ledger: TaskLedger,
    budget_ledger: BudgetLedger,
) -> dict[str, DepartmentExecutor]:
    research_fallback = TemplateDepartmentExecutor(graph, 'example.research')
    operations_fallback = TemplateDepartmentExecutor(graph, 'example.operations')
    return {
        'example.research': ResearchBusinessExecutor(
            graph, ledger, budget_ledger, research_fallback,
        ),
        'example.operations': OperationsBusinessExecutor(
            graph, operations_fallback,
        ),
        'example.audit': AuditBusinessExecutor(graph),
        'example.novel': NovelBusinessExecutor(graph.root),
    }
