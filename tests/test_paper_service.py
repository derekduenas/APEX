from dataclasses import asdict
import json

import pytest

from apex.core import Config, Refused
from apex.paper_book import PaperAccountConfig
from apex.paper_service import load_service_settings, read_json, tick_files


def _settings(tmp_path):
    path = tmp_path / 'settings.json'
    path.write_text(json.dumps({'schema': 'APEX_PAPER_SERVICE_V1', 'model': Config().record(),
                               'account': asdict(PaperAccountConfig(feed='sip'))}))
    return path


def test_missing_service_files_retain_honest_blocked_health(tmp_path):
    root = tmp_path / 'account'
    result = tick_files(root, input_path=tmp_path/'missing', session_path=tmp_path/'no-calendar',
                        settings_path=_settings(tmp_path))
    assert result['status'] == 'BLOCKED_SERVICE_INPUT'
    assert result['fills'] is None and result['account_pnl'] is None
    assert json.loads((root/'service-health.json').read_text()) == result
    assert not (root/'paper.sqlite').exists()


def test_denied_source_initializes_paper_account_without_fabricating_trades(tmp_path, monkeypatch):
    from apex import paper_runtime
    now = 1789567200.
    monkeypatch.setattr(paper_runtime.time, 'time', lambda: now)
    calendar = tmp_path/'session.json'
    calendar.write_text(json.dumps({'open_epoch': now - 600, 'close_epoch': now + 600,
        'calendar_id': 'TEST_ONLY', 'calendar_source': 'TEST_TRANSPORT_CALENDAR', 'known_at_epoch': now - 1000}))
    source = tmp_path/'source.json'
    source.write_text(json.dumps({'schema': 'APEX_DATA_V1', 'source_status': 'NOT_ENTITLED', 'observations': []}))
    result = tick_files(tmp_path/'account', input_path=source, session_path=calendar, settings_path=_settings(tmp_path))
    assert result['status'] == 'BLOCKED_NO_AUTH'
    assert result['account']['orders'] == result['account']['fills'] == 0
    assert result['account']['cash'] == '10000.00'


def test_settings_reject_feed_substitution_and_conflicting_economics(tmp_path):
    path = _settings(tmp_path)
    original = json.loads(path.read_text())
    changed = json.loads(path.read_text()); changed['account']['feed'] = 'iex'
    path.write_text(json.dumps(changed))
    with pytest.raises(Refused, match='SIP_FEED'):
        load_service_settings(path)
    original['account']['minimum_commission'] = '9.00'
    path.write_text(json.dumps(original))
    with pytest.raises(Refused, match='COSTS_DISAGREE'):
        load_service_settings(path)


def test_file_boundary_rejects_duplicate_keys_and_oversize(tmp_path):
    path = tmp_path/'input.json'
    path.write_text('{"status":"OK","status":"FAILED"}')
    with pytest.raises(Refused, match='INVALID_JSON_DOCUMENT'):
        read_json(path)
    with pytest.raises(Refused, match='TOO_LARGE'):
        read_json(path, limit=8)
