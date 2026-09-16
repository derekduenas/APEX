"""One bounded Responses request; an LLM proposal never acquires execution authority.

Contract checked against the official Structured Outputs guide on 2026-09-16:
https://developers.openai.com/api/docs/guides/structured-outputs
Only aggregate input metadata goes into the prompt. No credentials, source
text, filenames, provider URLs, or holdout outcomes are sent to the model.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import multiprocessing
import os
from pathlib import Path
import re
import tempfile
import time

from .core import Refused, canonical, digest

MAX_REQUEST_BYTES = 32_768
MAX_RESPONSE_BYTES = 262_144
MAX_PROPOSAL_BYTES = 8_192
WALL_SECONDS = 45
MAX_OUTPUT_TOKENS = 1200
ENDPOINT = 'https://api.openai.com/v1/responses'
PROPOSAL_SCHEMA = 'APEX_RESEARCH_PROPOSAL_V1'
ACTIONS = ('RUN_PEER_DISLOCATION_STUDY', 'DEFER_MISSING_DATA')
SYSTEM_PROMPT = (
    'You direct one bounded APEX offline research experiment. Decide whether the supplied '
    'operator-selected target, market and sector symbols justify running the predefined '
    'peer-dislocation study, or defer for missing data. The hypothesis is that a liquid '
    'stock lagging a coherent market/sector impulse may subsequently catch up, conditional '
    'on observed state and costs. This is an unproven hypothesis, not known alpha. '
    'You may choose only RUN_PEER_DISLOCATION_STUDY or DEFER_MISSING_DATA. Copy the exact '
    'operator-selected symbol roles. Explain the research rationale and data limitations. '
    'Never invent prices, probabilities, confidence, profits or study outcomes. '
    'You cannot change thresholds, costs, capital, sources, files, network endpoints or '
    'research budget, write code, place orders or promote a strategy. Inputs contain only '
    'data-coverage metadata, never test outcomes. Produce only the required JSON object.'
)


def strict_json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise Refused('DUPLICATE_JSON_KEY')
            result[key] = value
        return result
    def nonfinite(_):
        raise Refused('NONFINITE_JSON')
    try:
        return json.loads(raw, object_pairs_hook=unique, parse_constant=nonfinite)
    except (ValueError, TypeError, UnicodeError) as exc:
        raise Refused('INVALID_JSON_DOCUMENT') from None


def symbol_valid(value):
    return isinstance(value, str) and re.fullmatch(r'[A-Z][A-Z0-9.\-]{0,14}', value) is not None


def proposal_schema(symbols):
    properties = {
        'schema': {'type': 'string', 'enum': [PROPOSAL_SCHEMA]},
        'action': {'type': 'string', 'enum': list(ACTIONS)},
        **{role: {'type': 'string', 'enum': [symbol]} for role, symbol in symbols.items()},
        'rationale': {'type': 'string'},
    }
    return {'type': 'object', 'properties': properties,
            'required': list(properties), 'additionalProperties': False}


def validate_proposal(proposal, symbols):
    if not isinstance(proposal, dict) or set(proposal) != {'schema', 'action', 'rationale', *symbols}:
        raise Refused('PROPOSAL_FIELDS_NOT_ALLOWLISTED')
    if proposal['schema'] != PROPOSAL_SCHEMA or proposal['action'] not in ACTIONS:
        raise Refused('PROPOSAL_ACTION_NOT_ALLOWLISTED')
    if any(proposal[role] != symbol for role, symbol in symbols.items()):
        raise Refused('PROPOSAL_SYMBOL_OUTSIDE_OPERATOR_SELECTION')
    if not isinstance(proposal['rationale'], str) or not 1 <= len(proposal['rationale']) <= 1600:
        raise Refused('PROPOSAL_RATIONALE_INVALID')
    encoded = canonical(proposal).encode()
    if len(encoded) > MAX_PROPOSAL_BYTES:
        raise Refused('PROPOSAL_BYTES_EXCEEDED')
    key = os.environ.get('OPENAI_API_KEY')
    if key and key.encode() in encoded:
        raise Refused('PROPOSAL_CONTAINS_CREDENTIAL')
    return proposal


def make_request(context, symbols, model):
    if not isinstance(model, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.:\-]{0,99}', model):
        raise Refused('AI_PLANNER_UNAVAILABLE:EXPLICIT_MODEL_REQUIRED')
    request = {'model': model, 'store': False, 'max_output_tokens': MAX_OUTPUT_TOKENS,
        'input': [{'role': 'system', 'content': SYSTEM_PROMPT},
                  {'role': 'user', 'content': canonical(context)}],
        'text': {'format': {'type': 'json_schema', 'name': 'apex_research_proposal',
                           'strict': True, 'schema': proposal_schema(symbols)}}}
    raw = canonical(request).encode()
    if len(raw) > MAX_REQUEST_BYTES:
        raise Refused('AI_REQUEST_BYTES_EXCEEDED')
    return request


def _http_worker(output_path, request_bytes, key):
    """Process boundary supplies the hard wall-clock bound, including DNS/TLS."""
    connection = None
    def emit(value):
        with Path(output_path).open('xb') as handle:
            handle.write(value)
    try:
        connection = http.client.HTTPSConnection('api.openai.com', timeout=15)
        connection.request('POST', '/v1/responses', body=request_bytes,
            headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
        response = connection.getresponse()
        if response.status != 200:
            emit(canonical({'error': 'AI_HTTP_STATUS_' + str(response.status)}).encode())
            return
        declared = response.getheader('Content-Length')
        if declared is not None and (not declared.isdigit() or int(declared) > MAX_RESPONSE_BYTES):
            emit(b'{"error":"AI_RESPONSE_BYTES_EXCEEDED"}')
            return
        chunks, size, deadline = [], 0, time.monotonic() + 30
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError
            chunk = response.read(min(8192, MAX_RESPONSE_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                emit(b'{"error":"AI_RESPONSE_BYTES_EXCEEDED"}')
                return
        emit(b'O' + b''.join(chunks))
    except Exception:
        # Exception bodies and headers can contain credentials or provider data.
        try:
            emit(b'{"error":"AI_TRANSPORT_FAILED"}')
        except Exception:
            pass
    finally:
        if connection:
            connection.close()


def _request_bytes(request, key):
    context = multiprocessing.get_context('spawn')
    with tempfile.TemporaryDirectory(prefix='apex-planner-') as directory:
        output_path = Path(directory) / 'response.bin'
        process = context.Process(target=_http_worker,
            args=(str(output_path), canonical(request).encode(), key), daemon=True)
        process.start()
        try:
            # Wait for the entire producer, not merely the first readable IPC
            # byte. A partial response cannot escape this hard deadline.
            process.join(timeout=WALL_SECONDS)
            if process.is_alive():
                raise Refused('AI_PLANNER_WALL_TIME_EXCEEDED')
            if process.exitcode != 0 or not output_path.is_file() or output_path.is_symlink():
                raise Refused('AI_TRANSPORT_FAILED')
            if output_path.stat().st_size > MAX_RESPONSE_BYTES + 1:
                raise Refused('AI_RESPONSE_BYTES_EXCEEDED')
            with output_path.open('rb') as handle:
                raw = handle.read(MAX_RESPONSE_BYTES + 2)
            if len(raw) > MAX_RESPONSE_BYTES + 1:
                raise Refused('AI_RESPONSE_BYTES_EXCEEDED')
            if not raw.startswith(b'O'):
                failure = strict_json(raw)
                raise Refused(failure.get('error', 'AI_TRANSPORT_FAILED'))
            return raw[1:]
        except OSError:
            raise Refused('AI_TRANSPORT_FAILED') from None
        finally:
            if process.is_alive():
                process.terminate()
            process.join(timeout=2)
            if process.is_alive():
                process.kill()
                process.join(timeout=2)


def parse_response(raw, symbols):
    if len(raw) > MAX_RESPONSE_BYTES:
        raise Refused('AI_RESPONSE_BYTES_EXCEEDED')
    response = strict_json(raw)
    if not isinstance(response, dict) or response.get('status') != 'completed' or response.get('error') is not None:
        raise Refused('AI_RESPONSE_NOT_COMPLETED')
    texts = []
    output = response.get('output')
    if not isinstance(output, list):
        raise Refused('AI_RESPONSE_OUTPUT_INVALID')
    for item in output:
        if not isinstance(item, dict):
            raise Refused('AI_RESPONSE_OUTPUT_INVALID')
        if item.get('type') == 'reasoning':
            continue
        if item.get('type') != 'message' or item.get('role') != 'assistant' or item.get('status') != 'completed':
            raise Refused('AI_RESPONSE_UNEXPECTED_TOOL_OR_MESSAGE')
        for content in item.get('content', []):
            if not isinstance(content, dict) or content.get('type') != 'output_text' or not isinstance(content.get('text'), str):
                raise Refused('AI_RESPONSE_REFUSED_OR_INVALID')
            texts.append(content['text'])
    if len(texts) != 1:
        raise Refused('AI_RESPONSE_REQUIRES_ONE_PROPOSAL')
    proposal = validate_proposal(strict_json(texts[0]), symbols)
    return proposal


def request_plan(context, symbols, model):
    key = os.environ.get('OPENAI_API_KEY')
    if not key or not isinstance(model, str) or not model.strip():
        raise Refused('AI_PLANNER_UNAVAILABLE:KEY_AND_EXPLICIT_MODEL_REQUIRED')
    if len(key) > 1024 or any(ord(char) < 33 or ord(char) > 126 for char in key):
        raise Refused('AI_PLANNER_UNAVAILABLE:INVALID_KEY_FORMAT')
    request = make_request(context, symbols, model)
    raw = _request_bytes(request, key)
    proposal = parse_response(raw, symbols)
    return {'proposal': proposal, 'provenance': 'OPENAI_RESPONSES_NOT_INDEPENDENTLY_AUTHENTICATED',
            'model': model, 'request_digest': digest(request),
            'response_sha256': hashlib.sha256(raw).hexdigest()}
