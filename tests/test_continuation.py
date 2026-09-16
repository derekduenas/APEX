import csv
import io
import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from apex.continuation import evaluate, run_continuation, verify_continuation
from apex.core import Refused

PLAN = Path(__file__).parents[1] / 'plans/continuation-001.json'


def fixture(reverse=False):
    rows = []
    day = datetime(2026, 3, 2, tzinfo=ZoneInfo('America/New_York'))
    for i in range(20):
        d = day + timedelta(days=i)
        if d.weekday() >= 5:
            continue
        for hour, close in [(9, 101), (15, 99 if reverse else 102)]:
            rows.append(['SPY', int(d.replace(hour=hour, minute=30).timestamp()*1000),
                         100, 103, 98, close, 1000])
    return rows


def encode(rows):
    s = io.StringIO()
    w = csv.writer(s)
    w.writerow(['ticker', 't', 'o', 'h', 'l', 'c', 'v'])
    w.writerows(rows)
    return s.getvalue().encode()


def test_future_outcomes_cannot_change_decisions_and_dst():
    plan = json.loads(PLAN.read_text())
    a = evaluate(encode(fixture()), plan)
    b = evaluate(encode(fixture(True)), plan)
    assert [r['signal'] for r in a['rows']] == [r['signal'] for r in b['rows']]
    assert a['mean_signed_target_bps'] > 0 > b['mean_signed_target_bps']
    assert all(r['feature_available_assumed'] < r['decision_at'] < r['target_start'] < r['target_complete'] for r in a['rows'])
    assert {datetime.fromtimestamp(r['decision_at'], ZoneInfo('America/New_York')).strftime('%H:%M:%S') for r in a['rows']} == {'10:00:05'}


def test_cli_reconstruction_and_truncation(tmp_path):
    import os
    import subprocess
    import sys
    source = tmp_path / 'input.csv'
    source.write_bytes(encode(fixture()))
    out = tmp_path / 'run'
    command = [sys.executable, '-m', 'apex.cli', 'continuation-experiment', '--input', str(source), '--plan', str(PLAN), '--out', str(out)]
    proc = subprocess.run(command, capture_output=True, text=True, env=os.environ.copy())
    assert proc.returncode == 0, proc.stderr
    assert verify_continuation(out)['status'] == 'VALID'
    result = json.loads((out / 'result.json').read_text())
    result['rows'].pop()
    (out / 'result.json').write_text(json.dumps(result))
    assert verify_continuation(out)['status'] == 'INVALID'
    check = subprocess.run([sys.executable, '-m', 'apex.cli', 'verify-continuation', '--run', str(out)], capture_output=True, text=True)
    assert check.returncode == 2
    with pytest.raises(FileExistsError):
        run_continuation(source, PLAN, out)


def test_duplicates_missing_and_invalid_are_not_zero():
    plan = json.loads(PLAN.read_text())
    rows = fixture()
    with pytest.raises(Refused, match='DUPLICATE'):
        evaluate(encode(rows + rows[:1]), plan)
    del rows[1]
    rows[2][5] = float('nan')
    result = evaluate(encode(rows), plan)
    reasons = {x['reason'] for x in result['excluded_weekdays']}
    assert 'INVALID_OHLCV' in reasons and 'MISSING_WINDOW_OR_CLOSED_SESSION' in reasons
    assert result['eligible_sessions'] == len(fixture()) // 2 - 2
    assert result['account_pnl'] is None and result['actual_trades'] == 0


def test_plan_mutation_refused():
    plan = json.loads(PLAN.read_text())
    plan['to'] = '2026-06-30'
    with pytest.raises(Refused, match='PLAN_NOT_FROZEN'):
        evaluate(encode(fixture()), plan)
