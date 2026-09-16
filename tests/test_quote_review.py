from decimal import Decimal

import pytest

from apex.data import available_ns, duration_ns, normalize, visible
from apex.admissibility import require_mode
from apex.core import Refused
from test_admissibility import BAR, MEASURED, SYNTHETIC, verdict


def test_fractional_seconds_without_a_nanosecond_stamp_are_refused_not_reconstructed():
    """The replacement for a bound that was never sound.

    Two attempts were made to recover a provider's nanoseconds from float seconds. The first floored and could
    place an observation EARLIER than it happened. The second added an ulp and ceiled, and measurement over
    200,000 stamps found it still landed early about once in a hundred, by up to 112ns. The reason is that a
    float made as `n / 1e9` is not the nearest float to n/10**9 at all: n exceeds 2**53, so the int-to-float
    conversion rounds before the division happens. A bound therefore depends on HOW the float was produced, which
    the reader is never told. So the reconstruction is gone and the row is refused.
    """
    stamp = 1787578204996061335
    seconds = stamp / 1e9
    assert not float(seconds).is_integer()
    row = dict(MEASURED, available_epoch=seconds, event_epoch=1787578140.0)
    rows, rejected = normalize({'schema': 'APEX_DATA_V1', 'observations': [row]})
    assert rows == []
    assert rejected == [{'input_index': 0,
                         'reason': 'FRACTIONAL_SECONDS_REQUIRE_NANOSECOND_STAMPS:available_epoch'}]
    # An adapter that kept the integer stamp is used verbatim, with nothing inferred.
    row.update(event_ns=1787578140000000000, available_ns=stamp, available_epoch=stamp / 1e9)
    assert available_ns(row) == stamp


def test_the_double_rounding_that_made_a_bound_impossible_is_real():
    """Recorded as arithmetic rather than as a claim, because it is the whole reason for the refusal."""
    from fractions import Fraction
    n = 1871622886123547495
    assert n > 2 ** 53
    assert n / 1e9 != float(Fraction(n, 1_000_000_000)), "int->float rounds before the division"


def test_whole_seconds_carry_no_lost_digits_and_are_still_accepted():
    row = dict(MEASURED, event_epoch=1787578140.0, available_epoch=1787578200.0)
    rows, rejected = normalize({'schema': 'APEX_DATA_V1', 'observations': [row]})
    assert not rejected and available_ns(rows[0]) == 1787578200 * 10 ** 9


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
