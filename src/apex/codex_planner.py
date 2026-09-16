"""ChatGPT-authenticated Codex CLI proposals, with no API-token extraction.

Uses the supported CLI's saved authentication and structured-output route:
https://learn.chatgpt.com/docs/auth
https://learn.chatgpt.com/docs/non-interactive-mode
https://learn.chatgpt.com/docs/permissions

No credential file is read by this module. A generated noncredential canary
must prove the CLI's restricted sandbox before an inference command is allowed.
The model's actions remain the existing two research proposals, never orders.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import socket
import subprocess
import tempfile
import time

from .ai_planner import (MAX_PROPOSAL_BYTES, MAX_REQUEST_BYTES, SYSTEM_PROMPT,
                         proposal_schema, strict_json, symbol_valid, validate_proposal)
from .core import Refused, canonical, digest

SUPPORTED_CLI_VERSION = '0.154.0'
PROVENANCE = 'CODEX_CHATGPT_CLI_NOT_INDEPENDENTLY_AUTHENTICATED'
MAX_STDOUT_BYTES = 262144
MAX_STDERR_BYTES = 65536
WALL_SECONDS = 60
PREFLIGHT_SECONDS = 15
DISABLED_FEATURES = (
    'shell_tool', 'unified_exec', 'unified_exec_tty', 'shell_snapshot',
    'apps', 'plugins', 'remote_plugin', 'browser_use', 'browser_use_external',
    'computer_use', 'image_generation', 'view_image', 'multi_agent',
    'hooks', 'memories', 'code_mode_host', 'skill_search',
    'skill_mcp_dependency_install', 'tool_suggest', 'workspace_dependencies',
    'auth_elicitation', 'unbounded_connection_retries',
)
CONFIG = {
    'default_permissions': 'apex_planner',
    'permissions.apex_planner.filesystem': {':root': 'deny', ':minimal': 'read', ':workspace_roots': {'.': 'read'}},
    'permissions.apex_planner.network.enabled': False,
    'approval_policy': 'never', 'web_search': 'disabled',
    'tools.view_image': False, 'mcp_servers': {},
    'shell_environment_policy.inherit': 'none',
    'project_doc_max_bytes': 0,
}
ENV_ALLOWLIST = frozenset({
    'PATH', 'HOME', 'USER', 'LOGNAME', 'LANG', 'LC_ALL', 'TZ', 'TMPDIR', 'CODEX_HOME',
    'SSL_CERT_FILE', 'SSL_CERT_DIR', 'HTTPS_PROXY', 'HTTP_PROXY', 'NO_PROXY',
    'https_proxy', 'http_proxy', 'no_proxy',
})


def _environment():
    # Preserve the user's supported auth location; never repurpose HOME or
    # CODEX_HOME. API keys, access-token overrides and broker secrets are absent.
    return {key: value for key, value in os.environ.items() if key in ENV_ALLOWLIST}


def _toml(value):
    if isinstance(value, dict):
        return '{' + ','.join(canonical(k) + '=' + _toml(v) for k, v in value.items()) + '}'
    return canonical(value)


def _config_args():
    args = [item for key, value in CONFIG.items() for item in ('-c', key + '=' + _toml(value))]
    args.extend(item for feature in DISABLED_FEATURES for item in ('--disable', feature))
    return args


def _resolve(executable=None):
    if os.name != 'posix':
        raise Refused('CODEX_PLANNER_UNAVAILABLE:UNSUPPORTED_PROCESS_PLATFORM')
    selected = str(executable) if executable is not None else os.environ.get('APEX_CODEX_EXECUTABLE') or shutil.which('codex')
    if not selected:
        raise Refused('CODEX_PLANNER_UNAVAILABLE:CLI_NOT_INSTALLED')
    path = Path(selected).absolute()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise Refused('CODEX_PLANNER_UNAVAILABLE:CLI_NOT_EXECUTABLE')
    return path


def _run(argv, *, cwd, stdin=b'', timeout=PREFLIGHT_SECONDS, env=None):
    """Bound both complete streams and the entire process group, without a shell."""
    if len(stdin) > MAX_REQUEST_BYTES:
        raise Refused('CODEX_REQUEST_BYTES_EXCEEDED')
    if os.name != 'posix':
        raise Refused('CODEX_PLANNER_UNAVAILABLE:UNSUPPORTED_PROCESS_PLATFORM')
    process = None
    selector = selectors.DefaultSelector()
    buffers = {'stdout': bytearray(), 'stderr': bytearray()}
    remaining = memoryview(stdin)
    try:
        process = subprocess.Popen(argv, cwd=cwd, env=_environment() if env is None else env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            shell=False, start_new_session=True, close_fds=True)
        for name, handle in (('stdout', process.stdout), ('stderr', process.stderr)):
            os.set_blocking(handle.fileno(), False)
            selector.register(handle, selectors.EVENT_READ, name)
        if stdin:
            os.set_blocking(process.stdin.fileno(), False)
            selector.register(process.stdin, selectors.EVENT_WRITE, 'stdin')
        else:
            process.stdin.close()
        deadline = time.monotonic() + timeout
        while selector.get_map():
            budget = deadline - time.monotonic()
            if budget <= 0:
                raise Refused('CODEX_PROCESS_WALL_TIME_EXCEEDED')
            for entry, _ in selector.select(min(budget, .1)):
                handle, name = entry.fileobj, entry.data
                if name == 'stdin':
                    try:
                        written = os.write(handle.fileno(), remaining[:8192])
                    except BrokenPipeError:
                        written, remaining = 0, memoryview(b'')
                    remaining = remaining[written:]
                    if not remaining:
                        selector.unregister(handle)
                        handle.close()
                    continue
                chunk = os.read(handle.fileno(), 8192)
                if not chunk:
                    selector.unregister(handle)
                    handle.close()
                    continue
                buffers[name].extend(chunk)
                maximum = MAX_STDOUT_BYTES if name == 'stdout' else MAX_STDERR_BYTES
                if len(buffers[name]) > maximum:
                    raise Refused('CODEX_PROCESS_OUTPUT_BYTES_EXCEEDED')
        budget = deadline - time.monotonic()
        if budget <= 0:
            raise Refused('CODEX_PROCESS_WALL_TIME_EXCEEDED')
        try:
            returncode = process.wait(timeout=budget)
        except subprocess.TimeoutExpired:
            raise Refused('CODEX_PROCESS_WALL_TIME_EXCEEDED') from None
        return returncode, bytes(buffers['stdout']), bytes(buffers['stderr'])
    except OSError:
        raise Refused('CODEX_PROCESS_UNAVAILABLE') from None
    finally:
        selector.close()
        if process is not None:
            # Also remove descendants which closed their inherited streams.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            for handle in (process.stdin, process.stdout, process.stderr):
                if handle is not None and not handle.closed:
                    handle.close()


def codex_status(*, executable=None):
    """Only CLI status commands inspect auth; raw output is never returned."""
    result = {'status': 'CODEX_PLANNER_UNAVAILABLE', 'cli_version': None,
              'auth_method': 'UNVERIFIED', 'isolation_verified': False,
              'runtime_llm_call_completed': False}
    try:
        binary = _resolve(executable)
        with tempfile.TemporaryDirectory(prefix='apex-codex-status-') as cwd:
            code, output, _ = _run([str(binary), '--version'], cwd=cwd)
            expected = ('codex-cli ' + SUPPORTED_CLI_VERSION).encode()
            if code != 0 or output.strip() != expected:
                raise Refused('CODEX_PLANNER_UNAVAILABLE:UNREVIEWED_CLI_VERSION')
            result['cli_version'] = SUPPORTED_CLI_VERSION
            code, output, errors = _run([str(binary), 'login', 'status'], cwd=cwd)
            status = output + b'\n' + errors
            if code != 0 or b'Logged in using ChatGPT' not in status or b'API key' in status:
                raise Refused('CODEX_PLANNER_UNAVAILABLE:SAVED_CHATGPT_LOGIN_REQUIRED')
            result.update(status='CODEX_CHATGPT_AUTH_AVAILABLE_ISOLATION_UNVERIFIED', auth_method='CHATGPT')
    except Refused as exc:
        result['reason'] = str(exc)
    return result


def make_codex_request(context, symbols, model):
    if set(symbols) != {'target_symbol', 'market_symbol', 'sector_symbol'} or not all(symbol_valid(x) for x in symbols.values()):
        raise Refused('CODEX_PLANNER_SYMBOL_CONTRACT_INVALID')
    if not isinstance(model, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:\-]{0,99}', model):
        raise Refused('CODEX_PLANNER_UNAVAILABLE:EXPLICIT_MODEL_REQUIRED')
    record = {'schema': 'APEX_CODEX_REQUEST_V1', 'model': model,
        'system_prompt': SYSTEM_PROMPT + ' Do not invoke tools. All necessary metadata is included below.',
        'context': context, 'response_schema': proposal_schema(symbols),
        'cli_version': SUPPORTED_CLI_VERSION, 'configuration': CONFIG,
        'disabled_features': list(DISABLED_FEATURES), 'auth': 'SAVED_CHATGPT_CLI_ONLY',
        'inference_wall_seconds': WALL_SECONDS, 'maximum_stdout_bytes': MAX_STDOUT_BYTES}
    if len(canonical(record).encode()) > MAX_REQUEST_BYTES:
        raise Refused('CODEX_REQUEST_BYTES_EXCEEDED')
    return record


def _isolation_probe(binary, workspace, outside):
    """Harmless, generated canaries; never touches a real credential or key."""
    python = Path('/usr/bin/python3')
    if not python.is_file():
        raise Refused('CODEX_ISOLATION_UNVERIFIED:PROBE_RUNTIME_UNAVAILABLE')
    readable, forbidden = workspace/'readable.txt', outside/'canary.txt'
    readable.write_text('APEX_PUBLIC_CANARY')
    forbidden.write_text('APEX_GENERATED_NONCREDENTIAL_CANARY')
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        code = (
            'import pathlib,socket,sys; p=pathlib.Path(sys.argv[1]); q=pathlib.Path(sys.argv[2]); '
            'assert p.read_text()=="APEX_PUBLIC_CANARY"; '
            '\ntry:q.read_text();sys.exit(11)\nexcept (PermissionError,FileNotFoundError):pass'
            '\ntry:p.write_text("forbidden");sys.exit(12)\nexcept (PermissionError,OSError):pass'
            '\ns=socket.socket();s.settimeout(.5)'
            '\ntry:s.connect(("127.0.0.1",int(sys.argv[3])));sys.exit(13)\nexcept OSError:pass'
            '\nprint("APEX_ISOLATION_VERIFIED")'
        )
        args = [str(binary), 'sandbox', '--include-managed-config', '-P', 'apex_planner', '-C', str(workspace),
                *_config_args(), '--', str(python), '-c', code, str(readable), str(forbidden), str(port)]
        try:
            returncode, output, _ = _run(args, cwd=workspace)
        except Refused:
            raise Refused('CODEX_ISOLATION_UNVERIFIED:PROBE_FAILED') from None
        if returncode != 0 or output.strip() != b'APEX_ISOLATION_VERIFIED' or readable.read_text() != 'APEX_PUBLIC_CANARY':
            raise Refused('CODEX_ISOLATION_UNVERIFIED:BOUNDARY_NOT_ESTABLISHED')


def _parse_events(raw, symbols):
    if len(raw) > MAX_STDOUT_BYTES:
        raise Refused('CODEX_PROCESS_OUTPUT_BYTES_EXCEEDED')
    started, completed, messages = False, False, []
    for line in raw.splitlines():
        event = strict_json(line)
        if not isinstance(event, dict) or completed:
            raise Refused('CODEX_EVENT_ORDER_INVALID')
        kind = event.get('type')
        if kind == 'thread.started':
            if started:
                raise Refused('CODEX_EVENT_ORDER_INVALID')
        elif kind == 'turn.started':
            if started:
                raise Refused('CODEX_MULTIPLE_TURNS_REFUSED')
            started = True
        elif kind in ('item.started', 'item.updated', 'item.completed'):
            item = event.get('item')
            if not started or not isinstance(item, dict) or item.get('type') not in ('agent_message', 'reasoning'):
                raise Refused('CODEX_TOOL_OR_UNKNOWN_EVENT_REFUSED')
            if item['type'] == 'agent_message' and kind == 'item.completed':
                if not isinstance(item.get('text'), str):
                    raise Refused('CODEX_MESSAGE_INVALID')
                messages.append(item['text'])
        elif kind == 'turn.completed':
            if not started:
                raise Refused('CODEX_EVENT_ORDER_INVALID')
            completed = True
        else:
            raise Refused('CODEX_FAILED_OR_UNKNOWN_EVENT')
    if not completed or len(messages) != 1:
        raise Refused('CODEX_COMPLETE_SINGLE_PROPOSAL_REQUIRED')
    return validate_proposal(strict_json(messages[0]), symbols)


def request_codex_plan(context, symbols, model, *, executable=None):
    request = make_codex_request(context, symbols, model)
    binary = _resolve(executable)
    status = codex_status(executable=binary)
    if status['auth_method'] != 'CHATGPT':
        raise Refused(status.get('reason', 'CODEX_PLANNER_UNAVAILABLE:SAVED_CHATGPT_LOGIN_REQUIRED'))
    with tempfile.TemporaryDirectory(prefix='apex-codex-plan-') as directory:
        outside = Path(directory)
        workspace = outside/'workspace'
        workspace.mkdir()
        _isolation_probe(binary, workspace, outside)
        schema, output = workspace/'schema.json', workspace/'proposal.json'
        schema.write_text(canonical(request['response_schema']))
        args = [str(binary), 'exec', '--ignore-user-config', '--strict-config', '--ephemeral',
                '--skip-git-repo-check', '--json', '--color', 'never', '--output-schema', str(schema),
                '--output-last-message', str(output), '--model', model, '-C', str(workspace),
                *_config_args(), '-']
        prompt = (request['system_prompt'] + '\n' + canonical(context)).encode()
        returncode, events, _ = _run(args, cwd=workspace, stdin=prompt, timeout=WALL_SECONDS)
        if returncode != 0:
            raise Refused('CODEX_PLANNER_UNAVAILABLE:INFERENCE_FAILED')
        proposal = _parse_events(events, symbols)
        if not output.is_file() or output.is_symlink() or output.stat().st_size > MAX_PROPOSAL_BYTES:
            raise Refused('CODEX_FINAL_OUTPUT_UNAVAILABLE_OR_OVERSIZED')
        with output.open('rb') as handle:
            saved = handle.read(MAX_PROPOSAL_BYTES + 1)
        if len(saved) > MAX_PROPOSAL_BYTES or canonical(strict_json(saved)) != canonical(proposal):
            raise Refused('CODEX_FINAL_OUTPUT_DISAGREES')
        return {'proposal': proposal, 'provenance': PROVENANCE, 'model': model,
                'request_digest': digest(request), 'response_sha256': hashlib.sha256(events).hexdigest()}
