"""Actual SQLite account/fill path, fault recovery, causal execution and reader."""
from copy import deepcopy
from dataclasses import replace
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3

import pytest

from apex.core import Refused, canonical, digest
from apex.paper_book import PaperBook, PaperAccountConfig, verify_paper_book


CFG = PaperAccountConfig(max_notional='2000.00')
EVIDENCE = {'decision': 'EXPERIMENTAL_LONG', 'source': 'SYNTHETIC_CONTROL_NOT_MARKET_EDGE', 'due_epoch': 102}


def quote(event=101, *, available=None, bid=99.98, ask=100.0, size=100, symbol='SPY', measured=False):
    available = event + .1 if available is None else available
    q = {'kind': 'quote', 'symbol': symbol, 'event_epoch': event, 'available_epoch': available,
         'availability_basis': 'MEASURED_RECEIPT' if measured else 'SYNTHETIC_CLOCK',
         'bid': bid, 'ask': ask, 'bid_size': size, 'ask_size': size}
    if measured:
        q.update(feed='iex', receipt_basis='PARENT_PROCESS_RECEIPT_UPPER_BOUND',
                 raw_response_sha256='a'*64, available_epoch_ns=round(available*1e9),
                 provider_timestamp_ns=round(event*1e9), market_coverage='IEX_ONLY_NOT_NBBO')
    return q


def buy(book, *, order_id='buy-1', symbol='SPY', quantity=10, epoch=100, **kwargs):
    return book.submit_order(order_id, symbol=symbol, side='BUY', quantity=quantity,
        decision_epoch=epoch, decision_id=order_id+'-decision', evidence=EVIDENCE, **kwargs)


def sell(book, *, epoch=102):
    return book.submit_order('sell-1', symbol='SPY', side='SELL', quantity=10, decision_epoch=epoch,
        decision_id='sell-decision', evidence={'reason': 'FIXED_HORIZON_EXIT_NOT_MODEL_DEPENDENT'})


def test_exact_cents_later_quote_entry_exit_and_independent_reader(tmp_path):
    path = tmp_path/'account.sqlite'
    with PaperBook(path, config=CFG) as book:
        buy(book)
        result = book.process_quote(quote(), now=101.1)
        assert len(result['fills']) == 1
        f = result['fills'][0]
        assert f['notional'] == '1000.10' and f['commission'] == '0.05'
        assert f['cash_after'] == '8999.85' and f['fill_type'] == 'SIMULATED_PAPER_FILL'
        marked = book.snapshot(now=101.1)
        assert marked['cost_basis'] == '1000.15' and marked['quantity'] == 10
        assert marked['unrealized_net'] == '-0.50' and marked['equity'] == '9999.65'
        assert marked['estimated_liquidation_equity'] == '9999.50'
        sell(book)
        result = book.process_quote(quote(103, bid=101, ask=101.02), now=103.1)
        assert result['fills'][0]['notional'] == '1009.90'
        final = book.snapshot(now=200)
        assert final['cash'] == final['equity'] == '10009.70'
        assert final['realized_net'] == final['total_net_pnl'] == '9.70'
        assert final['unrealized_net'] == '0.00' and final['positions'] == []
        assert book.verify()['status'] == 'VALID'
        exported = tmp_path/'export.json'
        book.export(exported)
    assert verify_paper_book(path)['status'] == 'VALID'
    assert verify_paper_book(exported)['status'] == 'VALID'


def test_restart_idempotence_and_immutable_terms(tmp_path):
    path = tmp_path/'book.sqlite'
    with PaperBook(path, config=CFG) as book:
        buy(book)
        book.process_quote(quote(), now=101.1)
    with PaperBook(path, config=CFG) as book:
        assert buy(book)['status'] == 'FILLED'
        assert book.process_quote(quote(), now=101.1)['duplicate'] is True
        assert book.snapshot(now=101.1)['fills'] == 1
        with pytest.raises(Refused, match='ORDER_ID_REUSED'):
            buy(book, quantity=9)
        assert book.verify()['status'] == 'VALID'
    with pytest.raises(Refused, match='RESTART_CONTRACT'):
        PaperBook(path, config=replace(CFG, slippage_bps='2'))


@pytest.mark.parametrize('q,now,reason', [
    (quote(100, available=100), 100, 'NOT_STRICTLY_AFTER'),
    (quote(100.5), 100.6, 'MINIMUM_LATENCY'),
    (quote(101), 120, 'QUOTE_STALE'),
    (quote(101, size=9), 101.1, 'DISPLAYED_SIZE'),
    (quote(101, ask=201, bid=200), 101.1, 'FUNDED_CAPITAL'),
])
def test_ineligible_quotes_cannot_fill(tmp_path, q, now, reason):
    with PaperBook(tmp_path/'book.sqlite', config=CFG) as book:
        buy(book)
        result = book.process_quote(q, now=now)
        assert result['fills'] == [] and reason in result['waits'][0]['reason']
        assert book.snapshot(now=now)['cash'] == '10000.00'
        assert book.verify()['status'] == 'VALID'


def test_future_quote_and_spoofed_observation_id_refused(tmp_path):
    with PaperBook(tmp_path/'book.sqlite', config=CFG) as book:
        buy(book)
        with pytest.raises(Refused, match='NOT_YET_AVAILABLE'):
            book.process_quote(quote(110), now=101)
        q = quote()
        q['observation_id'] = 'b'*64
        with pytest.raises(Refused, match='ID_DISAGREES'):
            book.process_quote(q, now=101.1)
        assert book.verify()['fills'] == 0


def test_entry_price_limit_and_expiry_preserve_unfilled_cash(tmp_path):
    with PaperBook(tmp_path/'book.sqlite', config=CFG) as book:
        buy(book, limit_price='100.00', expires_epoch=103)
        result = book.process_quote(quote(), now=101.1)
        assert result['waits'][0]['reason'] == 'ENTRY_PRICE_LIMIT_EXCEEDED'  # Adverse slippage included.
        assert book.process_quote(quote(103), now=103.1)['expired'] == ['buy-1']
        assert book.snapshot(now=103.1)['pending_orders'] == []
        assert book.verify()['fills'] == 0


def test_expiry_without_quote_and_cancel_are_retained(tmp_path):
    with PaperBook(tmp_path/'book.sqlite', config=CFG) as book:
        buy(book, expires_epoch=110)
        assert book.expire_orders(now=110) == ['buy-1']
        buy(book, order_id='buy-2', epoch=111)
        assert book.cancel_order('buy-2', now=112)['status'] == 'CANCELLED'
        assert book.cancel_order('buy-2', now=113)['status'] == 'CANCELLED'
        assert book.verify()['status'] == 'VALID'


def test_no_shorting_averaging_or_multiple_pending_orders(tmp_path):
    with PaperBook(tmp_path/'book.sqlite', config=CFG) as book:
        with pytest.raises(Refused, match='FULL_FUNDED_EXIT'):
            sell(book, epoch=100)
        buy(book)
        with pytest.raises(Refused, match='PENDING_ORDER'):
            buy(book, order_id='buy-2')
        book.process_quote(quote(), now=101.1)
        with pytest.raises(Refused, match='NO_AVERAGING'):
            buy(book, order_id='buy-3', epoch=102)
        with pytest.raises(Refused, match='FULL_FUNDED_EXIT'):
            book.submit_order('sell-partial', symbol='SPY', side='SELL', quantity=9,
                decision_epoch=102, decision_id='partial', evidence={'reason': 'EXIT'})
        assert book.verify()['status'] == 'VALID'


def test_total_position_cost_is_capped_across_symbols(tmp_path):
    with PaperBook(tmp_path/'book.sqlite', config=replace(CFG, max_notional='1500.00')) as book:
        buy(book)
        buy(book, order_id='second', symbol='QQQ')
        book.process_quote(quote(), now=101.1)
        result = book.process_quote(quote(symbol='QQQ'), now=101.1)
        assert result['fills'] == [] and 'NOTIONAL_LIMIT' in result['waits'][0]['reason']
        assert book.snapshot(now=101.1)['quantity'] == 10
        assert book.verify()['status'] == 'VALID'


def test_stale_or_conflicting_mark_is_unknown_not_zero(tmp_path):
    with PaperBook(tmp_path/'book.sqlite', config=CFG) as book:
        buy(book)
        book.process_quote(quote(), now=101.1)
        stale = book.snapshot(now=200)
        assert stale['equity'] is None and stale['unrealized_net'] is None and stale['total_net_pnl'] is None
        assert stale['cash'] == '8999.85' and stale['realized_net'] == '0.00'
        book.process_quote(quote(101, available=102, bid=99.9), now=102)
        conflict = book.snapshot(now=102)
        assert conflict['positions'][0]['mark_status'] == 'CONFLICTING_QUOTE'
        assert conflict['equity'] is None
        assert book.verify()['status'] == 'VALID'


@pytest.mark.parametrize('mode', ['RECORDED_PAPER', 'LIVE_PAPER'])
def test_measured_input_contract_cannot_mix_synthetic_or_omit_receipts(tmp_path, mode):
    config = replace(CFG, feed='iex')
    with PaperBook(tmp_path/'book.sqlite', config=config, mode=mode) as book:
        buy(book)
        with pytest.raises(Refused, match='CLASS_DISAGREES'):
            book.process_quote(quote(), now=101.1)
        q = quote(measured=True)
        q.pop('raw_response_sha256')
        with pytest.raises(Refused, match='PROVENANCE_REQUIRED'):
            book.process_quote(q, now=101.1)
        result = book.process_quote(quote(measured=True), now=101.1)
        assert result['fills'][0]['fill_type'] == 'SIMULATED_PAPER_FILL'
        assert book.snapshot(now=101.1)['mode'] == mode
        assert book.verify()['status'] == 'VALID'


def test_transaction_rolls_back_quote_fill_and_state_on_failed_save(tmp_path, monkeypatch):
    path = tmp_path/'book.sqlite'
    with PaperBook(path, config=CFG) as book:
        buy(book)
        original = book._save
        def failed_save(state):
            raise RuntimeError('simulated process failure before commit')
        monkeypatch.setattr(book, '_save', failed_save)
        with pytest.raises(RuntimeError):
            book.process_quote(quote(), now=101.1)
        monkeypatch.setattr(book, '_save', original)
        assert book.snapshot(now=101.1)['fills'] == 0
        assert len(book.process_quote(quote(), now=101.1)['fills']) == 1
        assert book.verify()['status'] == 'VALID'


def _crash_during_fill(path):
    book = PaperBook(Path(path), config=CFG)
    def crash(_state):
        os._exit(19)
    book._save = crash
    book.process_quote(quote(), now=101.1)


def test_killed_writer_recovers_without_double_execution(tmp_path):
    path = tmp_path/'book.sqlite'
    with PaperBook(path, config=CFG) as book:
        buy(book)
    context = multiprocessing.get_context('spawn')
    worker = context.Process(target=_crash_during_fill, args=(str(path),))
    worker.start()
    worker.join(timeout=10)
    if worker.is_alive():
        worker.kill()
        worker.join(timeout=2)
        pytest.fail('Crash worker did not finish')
    assert worker.exitcode == 19
    with PaperBook(path, config=CFG) as book:
        assert book.snapshot(now=101.1)['fills'] == 0
        assert len(book.process_quote(quote(), now=101.1)['fills']) == 1
        assert book.verify()['status'] == 'VALID'


def rehash(document):
    previous = 'GENESIS'
    for index, row in enumerate(document['events'], 1):
        row.update(seq=index, prev_hash=previous)
        row['hash'] = digest({k:v for k,v in row.items() if k != 'hash'})
        previous = row['hash']


def test_independent_reader_rejects_rehashed_accounting_lie_and_deleted_fill(tmp_path):
    path = tmp_path/'export.json'
    with PaperBook(tmp_path/'book.sqlite', config=CFG) as book:
        buy(book)
        book.process_quote(quote(), now=101.1)
        book.export(path)
    original = json.loads(path.read_text())
    forged = deepcopy(original)
    fill = next(r for r in forged['events'] if r['kind'] == 'FILL')
    fill['payload']['cash_after'] = '999999.00'
    forged['state']['cash'] = '999999.00'
    rehash(forged)
    assert verify_paper_book(forged)['problems'] == ['PAPER_FILL_ACCOUNTING_DISAGREES']
    erased = deepcopy(original)
    erased['events'] = [r for r in erased['events'] if r['kind'] != 'FILL']
    rehash(erased)
    assert verify_paper_book(erased)['problems'] == ['PAPER_QUOTE_ORDER_EVALUATION_MISSING']


def test_restart_refuses_corrupt_materialized_state(tmp_path):
    path = tmp_path/'book.sqlite'
    with PaperBook(path, config=CFG) as book:
        buy(book)
        book.process_quote(quote(), now=101.1)
    with sqlite3.connect(path) as db:
        state = json.loads(db.execute('SELECT state FROM account').fetchone()[0])
        state['cash'] = '12345.00'
        db.execute('UPDATE account SET state=?', (canonical(state),))
    with pytest.raises(Refused, match='RECOVERY_VERIFICATION_FAILED'):
        PaperBook(path, config=CFG)


def test_wait_decision_is_durable_and_cannot_be_rewritten(tmp_path):
    path = tmp_path/'book.sqlite'
    with PaperBook(path, config=CFG) as book:
        book.record_decision('wait-1', now=100, payload={'decision':'WAIT','reason':'NO_EDGE'})
        book.record_decision('wait-1', now=100, payload={'decision':'WAIT','reason':'NO_EDGE'})
        with pytest.raises(Refused, match='DECISION_ID_REUSED'):
            book.record_decision('wait-1', now=100, payload={'decision':'BUY'})
    with PaperBook(path, config=CFG) as book:
        assert book.verify()['events'] == 2
        assert book.snapshot(now=100)['orders'] == 0


def test_one_decision_cannot_generate_a_second_order_even_with_new_order_id(tmp_path):
    with PaperBook(tmp_path/'book.sqlite', config=CFG) as book:
        buy(book)
        book.cancel_order('buy-1', now=100)
        with pytest.raises(Refused, match='DECISION_ALREADY_HAS_AN_ORDER'):
            book.submit_order('different-order', symbol='SPY', side='BUY', quantity=10,
                decision_epoch=100, decision_id='buy-1-decision', evidence=EVIDENCE)
        assert book.verify()['orders'] == 1


def test_restart_also_verifies_persisted_idempotency_keys(tmp_path):
    path = tmp_path/'book.sqlite'
    with PaperBook(path, config=CFG) as book:
        buy(book)
        book.process_quote(quote(), now=101.1)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE events SET dedup=NULL WHERE kind='QUOTE_OBSERVED'")
    assert verify_paper_book(path)['problems'] == ['PAPER_IDEMPOTENCY_INDEX_DISAGREES']
    with pytest.raises(Refused, match='IDEMPOTENCY_INDEX_DISAGREES'):
        PaperBook(path, config=CFG)
