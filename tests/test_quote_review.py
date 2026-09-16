from decimal import Decimal

import pytest

from apex.data import available_ns, duration_ns, normalize, visible
from apex.admissibility import require_mode
from apex.core import Refused
from test_admissibility import BAR, MEASURED, SYNTHETIC, verdict


def test_fractional_seconds_cannot_admit_a_quote_before_its_lost_ns_stamp():
    stamp = 1787578204996061335
    seconds = stamp / 1e9
    row = dict(MEASURED, available_epoch=seconds, event_epoch=1787578140.0)
    rows, rejected = normalize({'schema': 'APEX_DATA_V1', 'observations': [row]})
    assert not rejected
    assert available_ns(rows[0]) >= stamp
    assert visible(rows, now=seconds, symbol='SPY', kind='bar')[0] == []
    assert visible(rows, now=seconds + .000001, symbol='SPY', kind='bar')[0]
    row.update(event_ns=1787578140000000000, available_ns=stamp)
    assert available_ns(row) == stamp


def test_duration_does_not_extend_fractional_age_budget():
    assert duration_ns(15) == 15000000000
    assert duration_ns(Decimal('0.0000000019')) == 1


@pytest.mark.parametrize('row,source,allowed', [
    (BAR, 'RECORDED', {'OFFLINE_RESEARCH'}),
    (MEASURED, 'RECORDED', {'OFFLINE_RESEARCH', 'SHADOW_OBSERVATION'}),
    (SYNTHETIC, 'SYNTHETIC_CONTROL', {'SYNTHETIC_CONTROL'}),
])
def test_exact_admission_set_and_execution_refusal(row, source, allowed):
    doc, rows, result = verdict(source, [row])
    assert {mode for mode, yes in result['modes'].items() if yes} == allowed
    for mode in ('LIVE_PAPER', 'REAL_MONEY'):
        with pytest.raises(Refused, match='INPUT_NOT_ADMISSIBLE_FOR_' + mode):
            require_mode(doc, rows, mode)
