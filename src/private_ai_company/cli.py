'''Command-line interface for the portable private AI group runtime.'''

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .contracts import has_errors, load_json, validate_capability_manifest, validate_company_manifest
from .compatibility import evaluate_compatibility
from .doctor import run_doctor
from .executors import build_template_executors
from .orchestrator import GroupOrchestrator
from .organization import (
    load_organization_graph, render_organization_tree,
)
from .root import RootDiscoveryError, discover_root, resolve_explicit_root


def _print_findings(findings) -> None:
    for item in findings:
        location = f' [{item.location}]' if item.location else ''
        print(f'{item.severity.upper():7} {item.code}{location}: {item.message}')


def _load_group(root_arg: str | None):
    resolution = resolve_explicit_root(root_arg) if root_arg else discover_root()
    if resolution.pack_kind != 'group':
        raise RootDiscoveryError('This command requires a GroupPack root.')
    manifest_name = resolution.marker.get('manifest', 'group.manifest.json')
    manifest = load_json(resolution.path / str(manifest_name))
    graph, findings = load_organization_graph(resolution.path, manifest)
    return graph, findings


def main(argv: list[str] | None = None) -> int:
    # Robust output encoding across platforms. Windows consoles default to a
    # locale codepage (e.g. cp1252) that cannot encode non-ASCII text such as a
    # task summary, which would raise UnicodeEncodeError and abort the CLI.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError, OSError):
            pass

    parser = argparse.ArgumentParser(prog='private-ai-company')
    sub = parser.add_subparsers(dest='command', required=True)
    doctor = sub.add_parser('doctor', help='Run read-only host and portable pack diagnostics.')
    doctor.add_argument('--root')
    doctor.add_argument('--json', action='store_true')
    discover = sub.add_parser('discover-root', help='Locate the portable company root.')
    discover.add_argument('--start')
    discover.add_argument('--root')
    company = sub.add_parser('validate-company')
    company.add_argument('file')
    group = sub.add_parser('validate-group')
    group.add_argument('--root')
    tree = sub.add_parser('organization-tree')
    tree.add_argument('--root')
    tree.add_argument('--json', action='store_true')
    route = sub.add_parser('route-department')
    route.add_argument('capability')
    route.add_argument('--root')
    route.add_argument('--json', action='store_true')
    run_task = sub.add_parser('run-task')
    run_task.add_argument('--root')
    run_task.add_argument('--instruction', required=True)
    run_task.add_argument('--capability', action='append', required=True)
    run_task.add_argument('--criterion', action='append', required=True)
    run_task.add_argument('--task-id')
    run_task.add_argument('--json', action='store_true')
    serve_api = sub.add_parser('serve-api', help='Run the local CEO FastAPI gateway.')
    serve_api.add_argument('--root', required=True)
    serve_api.add_argument('--host', default='127.0.0.1')
    serve_api.add_argument('--port', type=int, default=8790)
    capability = sub.add_parser('validate-capability')
    capability.add_argument('file')
    compatibility = sub.add_parser('compatibility')
    compatibility.add_argument('company')
    compatibility.add_argument('model')
    args = parser.parse_args(argv)

    if args.command == 'doctor':
        report = run_doctor(explicit_root=args.root)
        if args.json:
            print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        else:
            _print_findings(report.checks)
            print(f'HEALTHY: {report.healthy}')
        return 0 if report.healthy else 1
    if args.command == 'discover-root':
        try:
            result = resolve_explicit_root(args.root) if args.root else discover_root(args.start)
        except RootDiscoveryError as exc:
            print(f'ERROR: {exc}')
            return 1
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        return 0
    if args.command == 'serve-api':
        if args.host not in {'127.0.0.1', 'localhost'}:
            print('ERROR: CEO API may only bind to the local Windows host.')
            return 1
        if not 1 <= args.port <= 65535:
            print('ERROR: port must be between 1 and 65535.')
            return 1
        try:
            from .ceo_api import create_runtime_app
            import uvicorn
            app = create_runtime_app(Path(args.root))
        except (ImportError, RuntimeError, ValueError) as exc:
            print(f'ERROR: {exc}')
            return 1
        uvicorn.run(app, host=args.host, port=args.port, log_level='info')
        return 0
    if args.command in {
        'validate-group', 'organization-tree', 'route-department', 'run-task',
    }:
        try:
            graph, findings = _load_group(args.root)
        except (RootDiscoveryError, ValueError) as exc:
            print(f'ERROR: {exc}')
            return 1
        if args.command == 'validate-group':
            _print_findings(findings)
            return 1 if has_errors(findings) else 0
        if has_errors(findings):
            _print_findings(findings)
            return 1
        if args.command == 'organization-tree':
            if args.json:
                print(json.dumps(graph.to_dict(), ensure_ascii=False, indent=2))
            else:
                print(render_organization_tree(graph))
            return 0
        if args.command == 'run-task':
            try:
                result = GroupOrchestrator(
                    graph, build_template_executors(graph),
                ).run(
                    instruction=args.instruction,
                    capabilities=args.capability,
                    acceptance_criteria=args.criterion,
                    task_id=args.task_id,
                )
            except (RuntimeError, ValueError) as exc:
                print(f'ERROR: {exc}')
                return 1
            if args.json:
                print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
            else:
                report = result.task.get('final_report', {})
                print(report.get('summary', 'Task completed.'))
                print(f"TASK: {result.task['task_id']}")
            return 0
        routes = graph.route(args.capability)
        if args.json:
            print(json.dumps(routes, ensure_ascii=False, indent=2))
        else:
            for item in routes:
                print(
                    f"{item['group_id']} / {item['subsidiary_id']} / "
                    f"{item['department_id']}"
                )
        return 0 if routes else 2
    if args.command == 'compatibility':
        report = evaluate_compatibility(load_json(Path(args.company)), load_json(Path(args.model)))
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        return 0 if report.compatible else 1
    data = load_json(Path(args.file))
    findings = validate_company_manifest(data) if args.command == 'validate-company' else validate_capability_manifest(data)
    _print_findings(findings)
    if not findings:
        print('PASS')
    return 1 if has_errors(findings) else 0


if __name__ == '__main__':
    raise SystemExit(main())
