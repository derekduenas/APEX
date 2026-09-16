"""Original integer receipts must survive the actual capture/reader boundary."""
import json

import pytest

from apex.core import Config, Refused
from apex.data import epoch_ns, normalize, visible, decision_cutoff_ns
from apex.decision import quote_at
from apex.capture import replay_capture
from test_capture import CaptureFixture


@pytest.mark.parametrize('rounding', ['early', 'late'])
def test_capture_quote_visible_at_exact_receipt_and_replay_agrees(tmp_path, rounding):
    f = CaptureFixture()
    base = f.ns + 30_000_000  # Three requests, each advancing this fixture clock by 10ms.
    offset = next(i for i in range(1, 2048)
                  if (epoch_ns((base+i)/1e9) < base+i if rounding=='early'
                      else epoch_ns((base+i)/1e9) > base+i))
    f.ns += offset
    root = tmp_path/'capture'
    f.capture(root)
    shadow = json.loads((root/'shadow-0003.json').read_text())
    result = shadow['result']
    assert result['now_ns'] == shadow['as_of_ns'] == base+offset
    assert result['health']['visible_quotes'] == 1
    assert result['health']['latest_quote_age_from_event_s'] < .04
    assert result['candidate']['reason'] != 'QUOTE_UNAVAILABLE_OR_CONFLICTING'
    assert result['quote_id']
    document = json.loads((root/'input.json').read_text())
    rows, rejected = normalize(document)
    assert not rejected
    legacy, _ = visible(rows,now=result['now'],symbol='SPY',kind='quote')
    if rounding == 'early':
        assert legacy == [], 'Reproduce the original lossy-cutoff failure on these same rows.'
    assert result["snapshot"]["bar_ids"]
    assert replay_capture(root)['status'] == 'AGREEMENT'


def test_integer_cutoff_never_admits_next_ns_even_when_float_aliases():
    base = 1789565400_000000000
    early = late = 0
    for offset in range(2048):
        cutoff = base + offset
        display = cutoff / 1e9
        early += epoch_ns(display) < cutoff
        late += epoch_ns(display) > cutoff
        raw = []
        for advance in (0, 1):
            event = cutoff - 35_000_000 + advance
            available = cutoff + advance
            raw.append({'kind':'quote','symbol':'SPY','event_ns':event,'available_ns':available,
                        'event_epoch':event/1e9,'available_epoch':available/1e9,
                        'availability_basis':'MEASURED_RECEIPT','bid':100,'ask':101+advance,
                        'bid_size':100,'ask_size':100})
        rows,rejected = normalize({'schema':'APEX_DATA_V1','observations':raw})
        assert not rejected
        seen,_ = visible(rows,now=display,now_ns=cutoff,symbol='SPY',kind='quote')
        assert len(seen)==1 and seen[0]['available_ns']==cutoff
        q,problem = quote_at(rows,display,Config(),now_ns=cutoff)
        assert problem is None and q['available_ns']==cutoff
    assert early and late, 'Exercise both lossy float round-trip directions.'


@pytest.mark.parametrize('bad', [True, -1, 1789565400_000001000])
def test_conflicting_or_invalid_clock_is_refused(bad):
    with pytest.raises(Refused,match='DECISION_CLOCK'):
        decision_cutoff_ns(1789565400.0,bad)
