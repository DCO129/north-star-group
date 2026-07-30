'''Provider-neutral model contracts and the first DeepSeek adapter.'''

from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Protocol
from urllib.parse import urlparse

from .timebase import authoritative_timestamp
from .trace import TraceContext, TraceEvent, TraceSink


@dataclass(frozen=True)
class ModelMessage:
    role: str
    content: str

    def to_dict(self) -> dict[str, str]:
        if self.role not in {'system', 'user', 'assistant', 'tool'}:
            raise ValueError(f'Unsupported message role: {self.role!r}.')
        if not self.content:
            raise ValueError('Message content must not be empty.')
        return {'role': self.role, 'content': self.content}


@dataclass(frozen=True)
class ModelRequest:
    messages: tuple[ModelMessage, ...]
    max_output_tokens: int = 512
    temperature: float = 0.0
    response_format: str = 'text'
    thinking: str = 'disabled'
    tools: tuple[dict[str, Any], ...] = ()
    tool_choice: str | dict[str, Any] | None = None
    timeout_seconds: float = 60.0
    routing: 'ModelRoutingRequirements | None' = None
    trace_context: TraceContext | None = None

    def validate(self) -> None:
        if not self.messages:
            raise ValueError('At least one model message is required.')
        for message in self.messages:
            message.to_dict()
        if self.max_output_tokens < 1:
            raise ValueError('max_output_tokens must be positive.')
        if not 0 <= self.temperature <= 2:
            raise ValueError('temperature must be between 0 and 2.')
        if self.response_format not in {'text', 'json_object'}:
            raise ValueError('response_format must be text or json_object.')
        if self.thinking not in {'auto', 'enabled', 'disabled'}:
            raise ValueError('thinking must be auto, enabled, or disabled.')
        if self.timeout_seconds <= 0:
            raise ValueError('timeout_seconds must be positive.')
        for tool in self.tools:
            if not isinstance(tool, dict) or tool.get('type') != 'function':
                raise ValueError('Each tool must be an OpenAI-compatible function tool.')


@dataclass(frozen=True)
class ModelUsage:
    input_tokens: int
    output_tokens: int
    total_tokens: int


@dataclass(frozen=True)
class ModelToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any] | str


@dataclass(frozen=True)
class ModelResult:
    provider: str
    model: str
    response_id: str | None
    content: str | None
    parsed_json: dict[str, Any] | list[Any] | None
    tool_calls: tuple[ModelToolCall, ...]
    finish_reason: str
    usage: ModelUsage


class ModelProvider(Protocol):
    provider_id: str
    model: str

    def complete(self, request: ModelRequest) -> ModelResult: ...


class ProviderError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        retryable: bool,
        http_status: int | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.http_status = http_status


class ProviderAuthenticationError(ProviderError):
    pass


class ProviderRateLimitError(ProviderError):
    pass


class ProviderTimeoutError(ProviderError):
    pass


class ProviderUnavailableError(ProviderError):
    pass


class ProviderRequestError(ProviderError):
    pass


class ProviderProtocolError(ProviderError):
    pass


@dataclass(frozen=True)
class ModelRoutingRequirements:
    required_capabilities: frozenset[str] = field(default_factory=frozenset)
    approved_provider_ids: frozenset[str] = field(default_factory=frozenset)
    allowed_data_policies: frozenset[str] = field(default_factory=frozenset)
    timeout_class: str = 'standard'
    max_cost_rank: int | None = None
    excluded_provider_ids: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not self.timeout_class.strip():
            raise ValueError('timeout_class must be non-empty.')
        if self.max_cost_rank is not None and self.max_cost_rank < 0:
            raise ValueError('max_cost_rank must not be negative.')


@dataclass(frozen=True)
class ProviderHealth:
    status: str
    reason_code: str = 'healthy'
    circuit_state: str = 'closed'
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    retry_at_epoch: float | None = None
    degraded_capabilities: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if self.status not in {'healthy', 'degraded', 'unavailable'}:
            raise ValueError('Provider health must be healthy, degraded, or unavailable.')
        if not self.reason_code.strip():
            raise ValueError('Provider health reason_code must be non-empty.')
        if self.circuit_state not in {'closed', 'open', 'half_open'}:
            raise ValueError('Provider circuit_state must be closed, open, or half_open.')
        if self.consecutive_failures < 0 or self.consecutive_successes < 0:
            raise ValueError('Provider health counters must not be negative.')
        if self.retry_at_epoch is not None and self.retry_at_epoch < 0:
            raise ValueError('retry_at_epoch must not be negative.')


@dataclass(frozen=True)
class CircuitBreakerPolicy:
    failure_threshold: int = 3
    recovery_timeout_seconds: float = 30.0
    success_threshold: int = 1

    def __post_init__(self) -> None:
        if self.failure_threshold < 1:
            raise ValueError('failure_threshold must be positive.')
        if self.recovery_timeout_seconds <= 0:
            raise ValueError('recovery_timeout_seconds must be positive.')
        if self.success_threshold < 1:
            raise ValueError('success_threshold must be positive.')


@dataclass(frozen=True)
class CircuitBreakerState:
    provider_id: str
    status: str = 'healthy'
    reason_code: str = 'healthy'
    circuit_state: str = 'closed'
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    retry_at_epoch: float | None = None
    degraded_capabilities: frozenset[str] = field(default_factory=frozenset)
    updated_at: str = field(default_factory=authoritative_timestamp)

    def to_health(self) -> ProviderHealth:
        return ProviderHealth(
            status=self.status, reason_code=self.reason_code,
            circuit_state=self.circuit_state,
            consecutive_failures=self.consecutive_failures,
            consecutive_successes=self.consecutive_successes,
            retry_at_epoch=self.retry_at_epoch,
            degraded_capabilities=self.degraded_capabilities,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            'provider_id': self.provider_id, 'status': self.status,
            'reason_code': self.reason_code,
            'circuit_state': self.circuit_state,
            'consecutive_failures': self.consecutive_failures,
            'consecutive_successes': self.consecutive_successes,
            'retry_at_epoch': self.retry_at_epoch,
            'degraded_capabilities': sorted(self.degraded_capabilities),
            'updated_at': self.updated_at,
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> 'CircuitBreakerState':
        return cls(
            provider_id=str(value['provider_id']),
            status=str(value.get('status') or 'healthy'),
            reason_code=str(value.get('reason_code') or 'healthy'),
            circuit_state=str(value.get('circuit_state') or 'closed'),
            consecutive_failures=int(value.get('consecutive_failures') or 0),
            consecutive_successes=int(value.get('consecutive_successes') or 0),
            retry_at_epoch=(
                float(value['retry_at_epoch'])
                if value.get('retry_at_epoch') is not None else None
            ),
            degraded_capabilities=frozenset(
                str(item) for item in value.get('degraded_capabilities', [])
            ),
            updated_at=str(value.get('updated_at') or authoritative_timestamp()),
        )


@dataclass(frozen=True)
class ProviderRegistration:
    provider: ModelProvider
    capabilities: frozenset[str]
    health: ProviderHealth
    data_policy: str
    timeout_classes: frozenset[str]
    cost_rank: int


@dataclass(frozen=True)
class ProviderRouteDecision:
    status: str
    code: str
    provider_id: str | None = None
    model_id: str | None = None


class ProviderRegistry:
    def __init__(self) -> None:
        self._entries: dict[str, ProviderRegistration] = {}

    def register(
        self,
        provider: ModelProvider,
        *,
        capabilities: frozenset[str],
        health: ProviderHealth | None = None,
        data_policy: str = 'standard',
        timeout_classes: frozenset[str] = frozenset({'standard'}),
        cost_rank: int = 100,
    ) -> None:
        provider_id = provider.provider_id.strip()
        if not provider_id or any(char in provider_id for char in '/\\:'):
            raise ValueError('provider_id must be a portable identifier.')
        if provider_id in self._entries:
            raise ValueError(f'Duplicate provider_id {provider_id!r}.')
        if not capabilities or not all(item.strip() for item in capabilities):
            raise ValueError('Provider capabilities must be non-empty strings.')
        if not data_policy.strip():
            raise ValueError('Provider data_policy must be non-empty.')
        if not timeout_classes or not all(item.strip() for item in timeout_classes):
            raise ValueError('Provider timeout_classes must be non-empty strings.')
        if cost_rank < 0:
            raise ValueError('Provider cost_rank must not be negative.')
        self._entries[provider_id] = ProviderRegistration(
            provider=provider,
            capabilities=frozenset(capabilities),
            health=health or ProviderHealth('healthy'),
            data_policy=data_policy,
            timeout_classes=frozenset(timeout_classes),
            cost_rank=cost_rank,
        )

    def set_health(self, provider_id: str, health: ProviderHealth) -> None:
        entry = self._entries.get(provider_id)
        if entry is None:
            raise KeyError(provider_id)
        self._entries[provider_id] = ProviderRegistration(
            provider=entry.provider, capabilities=entry.capabilities,
            health=health, data_policy=entry.data_policy,
            timeout_classes=entry.timeout_classes, cost_rank=entry.cost_rank,
        )

    def resolve(self, provider_id: str) -> ProviderRegistration | None:
        return self._entries.get(provider_id)

    def entries(self) -> tuple[ProviderRegistration, ...]:
        return tuple(self._entries.values())


class ProviderHealthStore:
    '''Atomic provider-health persistence without prompts, responses, or secrets.'''

    def __init__(self, path: Path | None = None):
        self.path = path
        self._states: dict[str, CircuitBreakerState] = {}
        self._lock = RLock()
        if self.path is not None and self.path.exists():
            payload = json.loads(self.path.read_text(encoding='utf-8'))
            if not isinstance(payload, dict):
                raise ValueError('Provider health state must be a JSON object.')
            rows = payload.get('providers', [])
            if not isinstance(rows, list):
                raise ValueError('Provider health providers must be an array.')
            self._states = {
                state.provider_id: state
                for item in rows if isinstance(item, dict)
                for state in [CircuitBreakerState.from_dict(item)]
            }

    def get(self, provider_id: str) -> CircuitBreakerState | None:
        with self._lock:
            return self._states.get(provider_id)

    def values(self) -> tuple[CircuitBreakerState, ...]:
        with self._lock:
            return tuple(
                self._states[key] for key in sorted(self._states)
            )

    def put(self, state: CircuitBreakerState) -> None:
        with self._lock:
            self._states[state.provider_id] = state
            if self.path is None:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            target = {
                'schema_version': 'provider-health/v0',
                'updated_at': authoritative_timestamp(),
                'providers': [item.to_dict() for item in self.values()],
            }
            temporary = self.path.with_name(f'.{self.path.name}.{os.getpid()}.tmp')
            try:
                with temporary.open('w', encoding='utf-8', newline='\n') as handle:
                    json.dump(target, handle, ensure_ascii=False, indent=2)
                    handle.write('\n')
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.path)
            finally:
                if temporary.exists():
                    temporary.unlink()


class ProviderHealthManager:
    '''Persist circuit state and publish effective health into the registry.'''

    def __init__(
        self,
        registry: ProviderRegistry,
        *,
        store: ProviderHealthStore | None = None,
        policy: CircuitBreakerPolicy | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.registry = registry
        self.store = store or ProviderHealthStore()
        self.policy = policy or CircuitBreakerPolicy()
        self.clock = clock
        self._lock = RLock()
        for state in self.store.values():
            if self.registry.resolve(state.provider_id) is not None:
                self.registry.set_health(state.provider_id, state.to_health())

    def _state(self, provider_id: str) -> CircuitBreakerState:
        state = self.store.get(provider_id)
        if state is not None:
            return state
        registration = self.registry.resolve(provider_id)
        if registration is None:
            raise KeyError(provider_id)
        health = registration.health
        return CircuitBreakerState(
            provider_id=provider_id, status=health.status,
            reason_code=health.reason_code,
            circuit_state=health.circuit_state,
            consecutive_failures=health.consecutive_failures,
            consecutive_successes=health.consecutive_successes,
            retry_at_epoch=health.retry_at_epoch,
            degraded_capabilities=health.degraded_capabilities,
        )

    def _publish(self, state: CircuitBreakerState) -> CircuitBreakerState:
        self.store.put(state)
        self.registry.set_health(state.provider_id, state.to_health())
        return state

    def refresh_expired(self) -> tuple[CircuitBreakerState, ...]:
        '''Move expired open circuits into half-open before provider routing.'''
        with self._lock:
            now = self.clock()
            refreshed: list[CircuitBreakerState] = []
            for state in self.store.values():
                if self.registry.resolve(state.provider_id) is None:
                    continue
                if (
                    state.circuit_state == 'open'
                    and state.retry_at_epoch is not None
                    and now >= state.retry_at_epoch
                ):
                    state = self._publish(replace(
                        state, status='degraded',
                        reason_code='circuit-half-open',
                        circuit_state='half_open', consecutive_successes=0,
                        updated_at=authoritative_timestamp(),
                    ))
                refreshed.append(state)
            return tuple(refreshed)

    def before_call(
        self, provider_id: str, required_capabilities: frozenset[str],
    ) -> tuple[bool, str]:
        with self._lock:
            state = self._state(provider_id)
            now = self.clock()
            if state.circuit_state == 'open':
                if state.retry_at_epoch is not None and now < state.retry_at_epoch:
                    return False, 'circuit-open'
                state = replace(
                    state, status='degraded', reason_code='circuit-half-open',
                    circuit_state='half_open', consecutive_successes=0,
                    updated_at=authoritative_timestamp(),
                )
                self._publish(state)
            if required_capabilities & state.degraded_capabilities:
                return False, 'provider-capability-degraded'
            return True, 'provider-call-allowed'

    def record_success(self, provider_id: str) -> CircuitBreakerState:
        with self._lock:
            state = self._state(provider_id)
            successes = state.consecutive_successes + 1
            if (
                state.circuit_state == 'half_open'
                and successes < self.policy.success_threshold
            ):
                return self._publish(replace(
                    state, status='degraded', reason_code='recovery-probe-passed',
                    consecutive_successes=successes,
                    updated_at=authoritative_timestamp(),
                ))
            return self._publish(CircuitBreakerState(
                provider_id=provider_id, status='healthy', reason_code='healthy',
                circuit_state='closed', consecutive_failures=0,
                consecutive_successes=successes,
                degraded_capabilities=frozenset(),
            ))

    def record_failure(
        self,
        provider_id: str,
        *,
        code: str,
        retryable: bool,
        capabilities: frozenset[str],
    ) -> CircuitBreakerState:
        with self._lock:
            state = self._state(provider_id)
            failures = state.consecutive_failures + 1
            degraded_capabilities = state.degraded_capabilities
            if code.startswith('capability-'):
                degraded_capabilities = degraded_capabilities | capabilities
            should_open = (
                not retryable
                or state.circuit_state == 'half_open'
                or failures >= self.policy.failure_threshold
            )
            if should_open:
                return self._publish(replace(
                    state, status='unavailable', reason_code=code,
                    circuit_state='open', consecutive_failures=failures,
                    consecutive_successes=0,
                    retry_at_epoch=self.clock() + self.policy.recovery_timeout_seconds,
                    degraded_capabilities=degraded_capabilities,
                    updated_at=authoritative_timestamp(),
                ))
            return self._publish(replace(
                state, status='degraded', reason_code=code,
                circuit_state='closed', consecutive_failures=failures,
                consecutive_successes=0,
                degraded_capabilities=degraded_capabilities,
                updated_at=authoritative_timestamp(),
            ))

    def snapshot(self) -> dict[str, object]:
        states = self.store.values()
        return {
            'schema_version': 'provider-health/v0',
            'generated_at': authoritative_timestamp(),
            'providers': [state.to_dict() for state in states],
            'summary': {
                'healthy': sum(state.status == 'healthy' for state in states),
                'degraded': sum(state.status == 'degraded' for state in states),
                'unavailable': sum(state.status == 'unavailable' for state in states),
                'open_circuits': sum(state.circuit_state == 'open' for state in states),
            },
        }


class FallbackStateMachine:
    '''Bound retries to distinct registered providers; never invent a provider.'''

    def __init__(
        self, requirements: ModelRoutingRequirements, maximum_attempts: int,
    ):
        if maximum_attempts < 1:
            raise ValueError('maximum_attempts must be positive.')
        self.requirements = requirements
        self.maximum_attempts = maximum_attempts
        self.attempted: list[str] = []

    def routing_requirements(self) -> ModelRoutingRequirements:
        return replace(
            self.requirements,
            excluded_provider_ids=(
                self.requirements.excluded_provider_ids | frozenset(self.attempted)
            ),
        )

    def record_attempt(self, provider_id: str) -> None:
        if provider_id not in self.attempted:
            self.attempted.append(provider_id)

    @property
    def exhausted(self) -> bool:
        return len(self.attempted) >= self.maximum_attempts


class ProviderRouter(Protocol):
    def route(self, request: ModelRoutingRequirements) -> ProviderRouteDecision: ...


class PolicyProviderRouter:
    '''Select a compatible registered provider without vendor-specific rules.'''

    def __init__(self, registry: ProviderRegistry):
        self.registry = registry

    def route(self, request: ModelRoutingRequirements) -> ProviderRouteDecision:
        entries = [
            entry for entry in self.registry.entries()
            if entry.provider.provider_id not in request.excluded_provider_ids
        ]
        if not entries:
            return ProviderRouteDecision('blocked', 'provider-fallback-exhausted')
        if request.approved_provider_ids:
            entries = [
                entry for entry in entries
                if entry.provider.provider_id in request.approved_provider_ids
            ]
            if not entries:
                return ProviderRouteDecision('blocked', 'provider-not-approved')
        healthy = [entry for entry in entries if entry.health.status != 'unavailable']
        if not healthy:
            return ProviderRouteDecision('blocked', 'provider-unhealthy')
        capable = [
            entry for entry in healthy
            if request.required_capabilities.issubset(
                entry.capabilities - entry.health.degraded_capabilities
            )
        ]
        if not capable:
            return ProviderRouteDecision('blocked', 'provider-capability-mismatch')
        if request.allowed_data_policies:
            capable = [
                entry for entry in capable
                if entry.data_policy in request.allowed_data_policies
            ]
            if not capable:
                return ProviderRouteDecision('blocked', 'provider-data-policy-mismatch')
        capable = [
            entry for entry in capable
            if request.timeout_class in entry.timeout_classes
        ]
        if not capable:
            return ProviderRouteDecision('blocked', 'provider-timeout-class-mismatch')
        if request.max_cost_rank is not None:
            capable = [
                entry for entry in capable
                if entry.cost_rank <= request.max_cost_rank
            ]
            if not capable:
                return ProviderRouteDecision('blocked', 'provider-cost-policy-mismatch')
        selected = min(
            capable,
            key=lambda entry: (
                entry.health.status == 'degraded', entry.cost_rank,
                entry.provider.provider_id,
            ),
        )
        return ProviderRouteDecision(
            status='selected', code='provider-selected',
            provider_id=selected.provider.provider_id,
            model_id=selected.provider.model,
        )


class RoutingModelProvider:
    '''Resolve a provider at call time through a stable registry/router contract.'''

    provider_id = 'provider-router'
    model = 'dynamic'

    def __init__(
        self,
        registry: ProviderRegistry,
        router: ProviderRouter | None = None,
        trace_sink: TraceSink | None = None,
        health_manager: ProviderHealthManager | None = None,
        maximum_fallback_attempts: int | None = None,
    ):
        self.registry = registry
        self.router = router or PolicyProviderRouter(registry)
        self.trace_sink = trace_sink
        self.health_manager = health_manager
        self.maximum_fallback_attempts = maximum_fallback_attempts

    def set_trace_sink(self, trace_sink: TraceSink) -> None:
        self.trace_sink = trace_sink

    def complete(self, request: ModelRequest) -> ModelResult:
        requirements = request.routing or ModelRoutingRequirements()
        maximum_attempts = self.maximum_fallback_attempts or max(
            1, len(self.registry.entries()),
        )
        fallback = FallbackStateMachine(requirements, maximum_attempts)
        last_error: ProviderError | None = None
        while not fallback.exhausted:
            if self.health_manager is not None:
                self.health_manager.refresh_expired()
            decision = self.router.route(fallback.routing_requirements())
            if self.trace_sink is not None and request.trace_context is not None:
                self.trace_sink.emit(TraceEvent(
                    context=request.trace_context, component='router', kind='route',
                    status=(
                        'completed' if decision.status == 'selected' else 'blocked'
                    ),
                    attributes={
                        'code': decision.code,
                        'provider_id': decision.provider_id,
                        'model_id': decision.model_id,
                        'attempted_provider_ids': list(fallback.attempted),
                    },
                ))
            if decision.status != 'selected' or decision.provider_id is None:
                if last_error is not None:
                    raise last_error
                raise ProviderUnavailableError(
                    'No provider satisfied the routing policy.', code=decision.code,
                    retryable=True,
                )
            provider_id = decision.provider_id
            registration = self.registry.resolve(provider_id)
            if registration is None:
                raise ProviderUnavailableError(
                    'The selected provider is no longer registered.',
                    code='provider-registration-missing', retryable=True,
                )
            fallback.record_attempt(provider_id)
            if self.health_manager is not None:
                allowed, code = self.health_manager.before_call(
                    provider_id, requirements.required_capabilities,
                )
                if not allowed:
                    last_error = ProviderUnavailableError(
                        'Provider circuit or capability gate denied the call.',
                        code=code, retryable=True,
                    )
                    continue
            try:
                result = registration.provider.complete(request)
            except ProviderError as exc:
                last_error = exc
                if self.health_manager is not None:
                    self.health_manager.record_failure(
                        provider_id, code=exc.code, retryable=exc.retryable,
                        capabilities=requirements.required_capabilities,
                    )
                if not exc.retryable:
                    raise
                continue
            if self.health_manager is not None:
                self.health_manager.record_success(provider_id)
            return result
        if last_error is not None:
            raise last_error
        raise ProviderUnavailableError(
            'Provider fallback attempts were exhausted.',
            code='provider-fallback-exhausted', retryable=True,
        )


@dataclass(frozen=True)
class TransportResponse:
    status: int
    headers: dict[str, str]
    body: bytes


class TransportTimeout(RuntimeError):
    pass


class TransportNetworkError(RuntimeError):
    pass


class HttpTransport(Protocol):
    def send(
        self,
        url: str,
        headers: dict[str, str],
        body: bytes,
        timeout_seconds: float,
    ) -> TransportResponse: ...


def _validate_https_endpoint(url: str) -> None:
    parsed = urlparse(url)
    if (
        parsed.scheme != 'https'
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError('Provider endpoint must be a clean HTTPS URL.')


class UrllibTransport:
    def send(
        self,
        url: str,
        headers: dict[str, str],
        body: bytes,
        timeout_seconds: float,
    ) -> TransportResponse:
        _validate_https_endpoint(url)
        request = urllib.request.Request(
            url, data=body, headers=headers, method='POST',
        )
        try:
            # The URL is constrained by _validate_https_endpoint above.
            with urllib.request.urlopen(  # nosec B310
                request, timeout=timeout_seconds,
            ) as response:
                return TransportResponse(
                    status=response.status,
                    headers=dict(response.headers.items()),
                    body=response.read(),
                )
        except urllib.error.HTTPError as exc:
            return TransportResponse(
                status=exc.code,
                headers=dict(exc.headers.items()) if exc.headers else {},
                body=exc.read(),
            )
        except (TimeoutError, socket.timeout) as exc:
            raise TransportTimeout('Provider request timed out.') from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                raise TransportTimeout('Provider request timed out.') from exc
            raise TransportNetworkError('Provider network request failed.') from exc


def resolve_secret_reference(secret_ref: str) -> str:
    if not secret_ref.startswith('env:'):
        raise ValueError('This runtime currently resolves only env: secret references.')
    variable = secret_ref.removeprefix('env:')
    if not variable or not variable.replace('_', '').isalnum():
        raise ValueError('Invalid environment-variable secret reference.')
    value = os.environ.get(variable, '').strip()
    if not value:
        raise ProviderAuthenticationError(
            f'Required secret reference {secret_ref!r} is unavailable.',
            code='credential-missing', retryable=False,
        )
    if any(character.isspace() for character in value):
        raise ProviderAuthenticationError(
            f'Secret reference {secret_ref!r} resolved to malformed data.',
            code='credential-malformed', retryable=False,
        )
    return value


class DeepSeekProvider:
    provider_id = 'deepseek'

    def __init__(
        self,
        model: str = 'deepseek-v4-pro',
        *,
        secret_ref: str = 'env:DEEPSEEK_API_KEY',
        base_url: str = 'https://api.deepseek.com',
        transport: HttpTransport | None = None,
        max_attempts: int = 3,
        backoff_seconds: float = 0.25,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if not model.strip():
            raise ValueError('Model name must not be empty.')
        if max_attempts < 1:
            raise ValueError('max_attempts must be positive.')
        endpoint = f'{base_url.rstrip("/")}/chat/completions'
        _validate_https_endpoint(endpoint)
        self.model = model
        self.secret_ref = secret_ref
        self.endpoint = endpoint
        self.transport = transport or UrllibTransport()
        self.max_attempts = max_attempts
        self.backoff_seconds = backoff_seconds
        self.sleep = sleep

    def complete(self, request: ModelRequest) -> ModelResult:
        request.validate()
        credential = resolve_secret_reference(self.secret_ref)
        payload: dict[str, Any] = {
            'model': self.model,
            'messages': [message.to_dict() for message in request.messages],
            'max_tokens': request.max_output_tokens,
            'stream': False,
        }
        if request.thinking != 'enabled':
            payload['temperature'] = request.temperature
        if request.response_format == 'json_object':
            payload['response_format'] = {'type': 'json_object'}
        if request.thinking != 'auto':
            payload['thinking'] = {'type': request.thinking}
        if request.tools:
            payload['tools'] = list(request.tools)
        if request.tool_choice is not None:
            payload['tool_choice'] = request.tool_choice
        encoded = json.dumps(
            payload, ensure_ascii=False, separators=(',', ':'),
        ).encode('utf-8')
        headers = {
            'Authorization': f'Bearer {credential}',
            'Content-Type': 'application/json',
        }
        response = self._send_with_retry(
            headers, encoded, request.timeout_seconds,
        )
        return self._parse_response(response, request)

    def _send_with_retry(
        self,
        headers: dict[str, str],
        body: bytes,
        timeout_seconds: float,
    ) -> TransportResponse:
        last_error: ProviderError | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self.transport.send(
                    self.endpoint, headers, body, timeout_seconds,
                )
                if 200 <= response.status < 300:
                    return response
                error = self._http_error(response.status)
            except TransportTimeout as exc:
                error = ProviderTimeoutError(
                    'Provider request timed out.', code='timeout', retryable=True,
                )
                error.__cause__ = exc
            except TransportNetworkError as exc:
                error = ProviderUnavailableError(
                    'Provider network request failed.',
                    code='network-unavailable', retryable=True,
                )
                error.__cause__ = exc
            last_error = error
            if not error.retryable or attempt == self.max_attempts:
                raise error
            self.sleep(self.backoff_seconds * (2 ** (attempt - 1)))
        if last_error is None:
            raise AssertionError('Retry loop ended without a result.')
        raise last_error

    @staticmethod
    def _http_error(status: int) -> ProviderError:
        if status in {401, 403}:
            return ProviderAuthenticationError(
                'Provider rejected the credential.', code='authentication',
                retryable=False, http_status=status,
            )
        if status == 429:
            return ProviderRateLimitError(
                'Provider rate limit exceeded.', code='rate-limit',
                retryable=True, http_status=status,
            )
        if status >= 500:
            return ProviderUnavailableError(
                'Provider service is unavailable.', code='provider-unavailable',
                retryable=True, http_status=status,
            )
        return ProviderRequestError(
            'Provider rejected the request.', code='invalid-request',
            retryable=False, http_status=status,
        )

    def _parse_response(
        self, response: TransportResponse, request: ModelRequest,
    ) -> ModelResult:
        try:
            payload = json.loads(response.body.decode('utf-8'))
            choice = payload['choices'][0]
            message = choice['message']
            finish_reason = str(choice['finish_reason'])
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise ProviderProtocolError(
                'Provider returned an invalid response envelope.',
                code='invalid-envelope', retryable=False,
            ) from exc
        raw_tool_calls = message.get('tool_calls') or []
        tool_calls: list[ModelToolCall] = []
        for raw_call in raw_tool_calls:
            try:
                function = raw_call['function']
                raw_arguments = function.get('arguments', '')
                try:
                    arguments: dict[str, Any] | str = json.loads(raw_arguments)
                except (TypeError, json.JSONDecodeError):
                    arguments = str(raw_arguments)
                tool_calls.append(ModelToolCall(
                    call_id=str(raw_call['id']),
                    name=str(function['name']),
                    arguments=arguments,
                ))
            except (KeyError, TypeError) as exc:
                raise ProviderProtocolError(
                    'Provider returned a malformed tool call.',
                    code='invalid-tool-call', retryable=False,
                ) from exc
        allowed_finishes = {'stop'}
        if request.tools:
            allowed_finishes.add('tool_calls')
        if finish_reason not in allowed_finishes:
            raise ProviderProtocolError(
                f'Provider response ended with {finish_reason!r}.',
                code='incomplete-response', retryable=False,
            )
        content = message.get('content')
        content = str(content) if content is not None else None
        parsed_json: dict[str, Any] | list[Any] | None = None
        if request.response_format == 'json_object':
            if finish_reason != 'stop' or not content:
                raise ProviderProtocolError(
                    'Structured response did not finish with JSON content.',
                    code='incomplete-json', retryable=False,
                )
            try:
                parsed_json = json.loads(content)
            except json.JSONDecodeError as exc:
                raise ProviderProtocolError(
                    'Structured response was not valid JSON.',
                    code='invalid-json', retryable=False,
                ) from exc
            if not isinstance(parsed_json, (dict, list)):
                raise ProviderProtocolError(
                    'Structured response must be a JSON object or array.',
                    code='invalid-json-root', retryable=False,
                )
        usage = payload.get('usage') or {}
        return ModelResult(
            provider=self.provider_id,
            model=str(payload.get('model') or self.model),
            response_id=str(payload['id']) if payload.get('id') else None,
            content=content,
            parsed_json=parsed_json,
            tool_calls=tuple(tool_calls),
            finish_reason=finish_reason,
            usage=ModelUsage(
                input_tokens=int(usage.get('prompt_tokens', 0)),
                output_tokens=int(usage.get('completion_tokens', 0)),
                total_tokens=int(usage.get('total_tokens', 0)),
            ),
        )


def build_runtime_provider(
    *,
    provider: str = 'deepseek',
    model: str = 'deepseek-v4-pro',
    secret_ref: str = 'env:DEEPSEEK_API_KEY',
    maximum_fallback_attempts: int = 1,
    health_store_path: Path | None = None,
    trace_sink: TraceSink | None = None,
) -> RoutingModelProvider:
    '''Build the production runtime model provider.

    This is the single approved construction path for the live CEO API and
    both launchers. It binds a routed DeepSeek provider without embedding any
    credential value: only ``secret_ref`` (an ``env:`` reference) is persisted
    or logged. Secret resolution happens inside ``DeepSeekProvider.complete``
    at call time and fails closed with a stable code when the reference is
    unavailable.

    ``maximum_fallback_attempts`` is hard-bounded to 1 for production: the
    runtime may not retry or switch providers on its own. Deterministic or
    fake providers are test-only explicit injections and must never be built
    by this path.
    '''
    if maximum_fallback_attempts < 1:
        raise ValueError('maximum_fallback_attempts must be positive.')
    registry = ProviderRegistry()
    deepseek = DeepSeekProvider(
        model=model,
        secret_ref=secret_ref,
        max_attempts=maximum_fallback_attempts,
    )
    registry.register(
        deepseek,
        capabilities=frozenset({'chat', 'structured-output', 'json_object'}),
        data_policy='standard',
        timeout_classes=frozenset({'standard'}),
        cost_rank=100,
    )
    health_manager = ProviderHealthManager(
        registry,
        store=ProviderHealthStore(health_store_path) if health_store_path else None,
    )
    router = PolicyProviderRouter(registry)
    return RoutingModelProvider(
        registry,
        router=router,
        health_manager=health_manager,
        trace_sink=trace_sink,
        maximum_fallback_attempts=maximum_fallback_attempts,
    )
