'''Read-only public-web acquisition for Research departments.'''

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from ipaddress import ip_address
from pathlib import Path
import socket
from typing import Callable, Iterable, Mapping
from urllib.parse import urlsplit, urlunsplit

from .timebase import authoritative_timestamp
from .tools import ToolRegistry, ToolRequest, ToolSpec


AddressResolver = Callable[[str], Iterable[str]]
PageLoader = Callable[[str], 'PageSnapshot']
ContentExtractor = Callable[[str, str], Mapping[str, object]]


def _normalized_host(value: str) -> str:
    host = value.strip().rstrip('.').lower()
    if not host or '/' in host or '\\' in host or '@' in host:
        raise ValueError('Allowed hosts must be plain DNS names.')
    try:
        ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError('Literal IP addresses are not valid allowed hosts.')
    return host.encode('idna').decode('ascii')


def resolve_addresses(host: str) -> tuple[str, ...]:
    values = {
        item[4][0]
        for item in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    }
    return tuple(sorted(values))


def validate_public_url(
    value: str,
    *,
    allowed_hosts: Iterable[str],
    resolver: AddressResolver = resolve_addresses,
) -> str:
    '''Return a canonical HTTPS URL or reject unsafe network destinations.'''
    if not isinstance(value, str) or not value.strip() or '\x00' in value:
        raise ValueError('A non-empty URL is required.')
    parsed = urlsplit(value.strip())
    if parsed.scheme.lower() != 'https':
        raise ValueError('Only HTTPS research URLs are allowed.')
    if parsed.username is not None or parsed.password is not None:
        raise ValueError('URL credentials are forbidden.')
    if parsed.port not in {None, 443}:
        raise ValueError('Only the default HTTPS port is allowed.')
    if parsed.hostname is None:
        raise ValueError('URL host is required.')
    host = _normalized_host(parsed.hostname)
    normalized_allowed = frozenset(_normalized_host(item) for item in allowed_hosts)
    if host not in normalized_allowed:
        raise ValueError('URL host is not allowlisted.')
    addresses = tuple(resolver(host))
    if not addresses:
        raise ValueError('URL host did not resolve.')
    for value_address in addresses:
        address = ip_address(value_address)
        if not address.is_global:
            raise ValueError('URL host resolves to a non-public address.')
    netloc = host
    path = parsed.path or '/'
    return urlunsplit(('https', netloc, path, parsed.query, ''))


@dataclass(frozen=True)
class ResearchBrowserConfig:
    allowed_hosts: tuple[str, ...]
    navigation_timeout_ms: int = 20_000
    max_html_bytes: int = 2_000_000
    max_text_chars: int = 80_000
    executable_path: str | None = None

    def __post_init__(self) -> None:
        hosts = tuple(dict.fromkeys(_normalized_host(item) for item in self.allowed_hosts))
        if not hosts:
            raise ValueError('At least one allowed host is required.')
        if self.navigation_timeout_ms <= 0:
            raise ValueError('navigation_timeout_ms must be positive.')
        if self.max_html_bytes <= 0 or self.max_text_chars <= 0:
            raise ValueError('Content limits must be positive.')
        object.__setattr__(self, 'allowed_hosts', hosts)

    @property
    def resource_prefixes(self) -> tuple[str, ...]:
        return tuple(f'https://{host}' for host in self.allowed_hosts)


@dataclass(frozen=True)
class PageSnapshot:
    requested_url: str
    final_url: str
    status_code: int
    html: str


class PlaywrightPageLoader:
    '''Load only the main HTTPS document; scripts, assets, writes and downloads stay off.'''

    def __init__(
        self,
        config: ResearchBrowserConfig,
        *,
        resolver: AddressResolver = resolve_addresses,
    ) -> None:
        self.config = config
        self.resolver = resolver

    def __call__(self, url: str) -> PageSnapshot:
        canonical = validate_public_url(
            url, allowed_hosts=self.config.allowed_hosts, resolver=self.resolver,
        )
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError('research-browser-playwright-unavailable') from exc

        with sync_playwright() as playwright:
            launch_options: dict[str, object] = {'headless': True}
            if self.config.executable_path:
                executable = Path(self.config.executable_path)
                if not executable.is_file():
                    raise RuntimeError('research-browser-executable-missing')
                launch_options['executable_path'] = str(executable)
            browser = playwright.chromium.launch(**launch_options)
            try:
                context = browser.new_context(
                    accept_downloads=False,
                    java_script_enabled=False,
                    service_workers='block',
                )

                def guard(route) -> None:
                    request = route.request
                    if request.method.upper() not in {'GET', 'HEAD'}:
                        route.abort('blockedbyclient')
                        return
                    if request.resource_type != 'document':
                        route.abort('blockedbyclient')
                        return
                    try:
                        validate_public_url(
                            request.url,
                            allowed_hosts=self.config.allowed_hosts,
                            resolver=self.resolver,
                        )
                    except (OSError, ValueError):
                        route.abort('blockedbyclient')
                        return
                    route.continue_()

                context.route('**/*', guard)
                page = context.new_page()
                response = page.goto(
                    canonical,
                    wait_until='domcontentloaded',
                    timeout=self.config.navigation_timeout_ms,
                )
                if response is None:
                    raise RuntimeError('research-browser-no-response')
                if response.status < 200 or response.status >= 400:
                    raise RuntimeError('research-browser-http-error')
                final_url = validate_public_url(
                    page.url,
                    allowed_hosts=self.config.allowed_hosts,
                    resolver=self.resolver,
                )
                html = page.content()
                if len(html.encode('utf-8')) > self.config.max_html_bytes:
                    raise RuntimeError('research-browser-content-too-large')
                return PageSnapshot(canonical, final_url, response.status, html)
            finally:
                browser.close()


def extract_with_trafilatura(html: str, url: str) -> Mapping[str, object]:
    try:
        from trafilatura import bare_extraction
    except ImportError as exc:
        raise RuntimeError('research-browser-trafilatura-unavailable') from exc
    document = bare_extraction(
        html,
        url=url,
        include_comments=False,
        include_tables=True,
        favor_precision=True,
        as_dict=True,
    )
    if not isinstance(document, Mapping) or not str(document.get('text') or '').strip():
        raise RuntimeError('research-browser-extraction-empty')
    return document


class ResearchBrowserHandler:
    def __init__(
        self,
        config: ResearchBrowserConfig,
        *,
        loader: PageLoader | None = None,
        extractor: ContentExtractor = extract_with_trafilatura,
        resolver: AddressResolver = resolve_addresses,
    ) -> None:
        self.config = config
        self.resolver = resolver
        self.loader = loader or PlaywrightPageLoader(config, resolver=resolver)
        self.extractor = extractor

    def __call__(self, request: ToolRequest) -> Mapping[str, object]:
        canonical = validate_public_url(
            request.resource,
            allowed_hosts=self.config.allowed_hosts,
            resolver=self.resolver,
        )
        requested_limit = request.arguments.get('max_text_chars', self.config.max_text_chars)
        if not isinstance(requested_limit, int) or requested_limit <= 0:
            raise ValueError('max_text_chars must be a positive integer.')
        limit = min(requested_limit, self.config.max_text_chars)
        snapshot = self.loader(canonical)
        final_url = validate_public_url(
            snapshot.final_url,
            allowed_hosts=self.config.allowed_hosts,
            resolver=self.resolver,
        )
        document = self.extractor(snapshot.html, final_url)
        full_text = str(document.get('text') or '').strip()
        if not full_text:
            raise RuntimeError('research-browser-extraction-empty')
        text = full_text[:limit]
        return {
            'schema_version': 'research-source/v0',
            'source_url': final_url,
            'http_status': snapshot.status_code,
            'title': str(document.get('title') or '').strip() or None,
            'author': str(document.get('author') or '').strip() or None,
            'published_at': str(document.get('date') or '').strip() or None,
            'text': text,
            'text_chars': len(text),
            'truncated': len(text) < len(full_text),
            'content_sha256': sha256(full_text.encode('utf-8')).hexdigest(),
            'retrieved_at': authoritative_timestamp(),
        }


def register_research_browser_tool(
    registry: ToolRegistry,
    config: ResearchBrowserConfig,
    *,
    loader: PageLoader | None = None,
    extractor: ContentExtractor = extract_with_trafilatura,
    resolver: AddressResolver = resolve_addresses,
) -> ToolSpec:
    spec = ToolSpec(
        tool_id='research-browser-read',
        action='read-public-web',
        permission_level='L3',
        resource_prefixes=config.resource_prefixes,
        budget_category='tools_research',
        external_effect=True,
        reversible=True,
        timeout_seconds=max(1, config.navigation_timeout_ms // 1000),
        idempotent=True,
        required_capabilities=frozenset({'research'}),
    )
    registry.register(spec, ResearchBrowserHandler(
        config, loader=loader, extractor=extractor, resolver=resolver,
    ))
    return spec
