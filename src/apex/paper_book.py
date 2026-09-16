"""Transactional, funded-long paper accounting; never a broker or real-money book.

SQLite FULL-synchronous commits bind orders, fills, chain and recovered state.
The independent reader recomputes fill arithmetic from primary quote evidence.
Live inputs still produce SIMULATED fills; provenance labels are not signatures.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
from pathlib import Path
import re
import sqlite3

from .core import Refused, canonical, digest, finite
from .data import normalize

MODES = ('SYNTHETIC_PAPER', 'RECORDED_PAPER', 'LIVE_PAPER')
AUTHORITY = 'SIMULATED_PAPER_ONLY_NO_BROKER_OR_REAL_CAPITAL'
CENT = Decimal('0.01')


def _require(value, reason):
    if not value:
        raise Refused(reason)


def _decimal(value):
    _require(isinstance(value, (str, int, float, Decimal)) and not isinstance(value, bool), 'PAPER_INVALID_DECIMAL')
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise Refused('PAPER_INVALID_DECIMAL') from None
    _require(result.is_finite() and abs(result) <= Decimal('1000000000000'), 'PAPER_INVALID_DECIMAL')
    return result


def _cents(value):
    return _decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def _clock(value):
    _require(finite(value) and 0 <= value <= 32503680000, 'PAPER_INVALID_CLOCK')
    return value


def _identifier(value):
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.:\-]{1,160}', value) is not None


@dataclass(frozen=True)
class PaperAccountConfig:
    starting_cash: str = '10000.00'
    max_notional: str = '1000.00'
    commission_per_share: str = '0.005'
    minimum_commission: str = '0.01'
    slippage_bps: str = '1.0'
    min_latency_seconds: float = 1.0
    max_quote_age_seconds: float = 15.0
    order_ttl_seconds: float = 120.0
    feed: str = 'SYNTHETIC'

    def __post_init__(self):
        for key in ('starting_cash', 'max_notional', 'commission_per_share', 'minimum_commission', 'slippage_bps'):
            raw = getattr(self, key)
            _require(isinstance(raw, str) and len(raw) <= 32 and _decimal(raw) >= 0, 'PAPER_INVALID_CONFIG:' + key)
        _require(_cents(self.starting_cash) > 0 and _cents(self.max_notional) > 0, 'PAPER_NONPOSITIVE_CAPITAL')
        _require(_decimal(self.starting_cash) == _cents(self.starting_cash)
                 and _decimal(self.max_notional) == _cents(self.max_notional), 'PAPER_CAPITAL_REQUIRES_EXACT_CENTS')
        _require(_decimal(self.slippage_bps) <= 100, 'PAPER_SLIPPAGE_CONFIG_INVALID')
        for key in ('min_latency_seconds', 'max_quote_age_seconds', 'order_ttl_seconds'):
            _clock(getattr(self, key))
        _require(self.min_latency_seconds >= 0 and self.max_quote_age_seconds > 0
                 and self.order_ttl_seconds > self.min_latency_seconds, 'PAPER_INVALID_TIME_CONFIG')
        _require(self.feed in ('SYNTHETIC', 'iex', 'sip'), 'PAPER_FEED_CONTRACT_INVALID')


def _contract(config, mode):
    _require(isinstance(config, PaperAccountConfig) and mode in MODES, 'PAPER_ACCOUNT_CONTRACT_INVALID')
    _require((mode == 'SYNTHETIC_PAPER') == (config.feed == 'SYNTHETIC'), 'PAPER_MODE_FEED_DISAGREES')
    return {'schema': 'APEX_PAPER_ACCOUNT_V1', 'mode': mode, 'config': asdict(config), 'authority': AUTHORITY,
            'fill_basis': 'SIMULATED_NEXT_ELIGIBLE_QUOTE_FULL_FILL',
            'latency_basis': 'OPERATOR_DECLARED_MINIMUM_SIMULATED_LATENCY_PLUS_OBSERVED_PROCESSING_DELAY',
            'position_policy': 'FUNDED_LONG_ONE_LOT_PER_SYMBOL_FULL_EXITS_NO_MARGIN',
            'notional_policy': 'TOTAL_OPEN_PURCHASE_COST_INCLUDING_ENTRY_FEES_CAPPED',
            'pending_funding_policy': 'NO_RESERVATION; SERIAL_FILL_TIME_CASH_AND_POSITION_CAP_CHECKS',
            'cost_basis': 'ILLUSTRATIVE_COMMISSION_AND_ADVERSE_SLIPPAGE_NOT_BROKER_FEES'}


def _initial(config):
    return {'cash': str(_cents(config.starting_cash)), 'realized_net': '0.00', 'positions': {},
            'orders': {}, 'decisions': {}, 'latest_quotes': {}, 'clock': 0, 'fill_count': 0}


def _quote(raw, contract):
    _require(isinstance(raw, dict) and len(canonical(raw).encode()) <= 32768, 'PAPER_QUOTE_INVALID')
    accepted, rejected = normalize({'schema': 'APEX_DATA_V1', 'observations': [raw]})
    _require(not rejected and len(accepted) == 1 and accepted[0]['kind'] == 'quote', 'PAPER_QUOTE_INVALID')
    quote = accepted[0]
    _require(_identifier(quote['symbol']) and all(type(quote[k]) is int for k in ('bid_size', 'ask_size')),
             'PAPER_QUOTE_SIZE_OR_SYMBOL_INVALID')
    _require(all(_decimal(quote[k]) > 0 for k in ('bid', 'ask')), 'PAPER_QUOTE_PRICE_INVALID')
    if 'observation_id' in raw:
        _require(raw['observation_id'] == quote['observation_id'], 'PAPER_QUOTE_ID_DISAGREES')
    if contract['mode'] == 'SYNTHETIC_PAPER':
        _require(quote['availability_basis'] == 'SYNTHETIC_CLOCK', 'PAPER_QUOTE_CLASS_DISAGREES')
    else:
        _require(quote['availability_basis'] == 'MEASURED_RECEIPT', 'PAPER_QUOTE_CLASS_DISAGREES')
        _require(quote.get('feed') == contract['config']['feed']
                 and quote.get('receipt_basis') == 'PARENT_PROCESS_RECEIPT_UPPER_BOUND'
                 and isinstance(quote.get('raw_response_sha256'), str)
                 and re.fullmatch('[0-9a-f]{64}', quote['raw_response_sha256']) is not None,
                 'PAPER_QUOTE_PROVENANCE_REQUIRED')
        for ns, seconds in (('available_epoch_ns', 'available_epoch'), ('provider_timestamp_ns', 'event_epoch')):
            _require(type(quote.get(ns)) is int and quote[ns] >= 0
                     and abs(quote[ns] / 1e9 - quote[seconds]) <= .000001, 'PAPER_QUOTE_CLOCK_PROVENANCE_DISAGREES')
        _require(quote['provider_timestamp_ns'] <= quote['available_epoch_ns'], 'PAPER_QUOTE_EVENT_AFTER_RECEIPT')
        expected = 'SIP' if quote['feed'] == 'sip' else 'IEX_ONLY_NOT_NBBO'
        _require(quote.get('market_coverage') == expected, 'PAPER_QUOTE_COVERAGE_REQUIRED')
    return quote


def _order_record(order_id, symbol, side, quantity, decision_epoch, decision_id, evidence, expires_epoch, config, limit_price=None):
    _require(_identifier(order_id) and _identifier(decision_id) and _identifier(symbol), 'PAPER_ORDER_ID_INVALID')
    _require(side in ('BUY', 'SELL') and type(quantity) is int and 0 < quantity <= 10000000, 'PAPER_ORDER_CONTRACT_INVALID')
    _clock(decision_epoch)
    expiry = decision_epoch + config.order_ttl_seconds if expires_epoch is None else _clock(expires_epoch)
    _require(decision_epoch + config.min_latency_seconds < expiry <= decision_epoch + config.order_ttl_seconds,
             'PAPER_ORDER_EXPIRY_INVALID')
    _require(isinstance(evidence, dict) and bool(evidence) and len(canonical(evidence).encode()) <= 32768,
             'PAPER_DECISION_EVIDENCE_REQUIRED')
    _require(limit_price is None or (side == 'BUY' and _decimal(limit_price) > 0), 'PAPER_ENTRY_LIMIT_INVALID')
    return {'order_id': order_id, 'symbol': symbol, 'side': side, 'quantity': quantity,
            'decision_epoch': decision_epoch, 'decision_id': decision_id, 'evidence': evidence,
            'expires_epoch': expiry, 'limit_price': str(_decimal(limit_price)) if limit_price is not None else None, 'authority': AUTHORITY}


def _admit_order(state, order):
    _require(not any(o['decision_id'] == order['decision_id'] for o in state['orders'].values()),
             'PAPER_DECISION_ALREADY_HAS_AN_ORDER')
    symbol = order['symbol']
    _require(not any(o['symbol'] == symbol and o['status'] == 'PENDING' for o in state['orders'].values()),
             'PAPER_PENDING_ORDER_ALREADY_EXISTS')
    position = state['positions'].get(symbol)
    if order['side'] == 'BUY':
        _require(position is None, 'PAPER_EXISTING_POSITION_NO_AVERAGING')
    else:
        _require(position is not None and position['quantity'] == order['quantity'], 'PAPER_FULL_FUNDED_EXIT_REQUIRED')


def _observe_quote(state, quote, now):
    previous = state['latest_quotes'].get(quote['symbol'])
    market_fields = ('bid', 'ask', 'bid_size', 'ask_size')
    if previous is None or quote['event_epoch'] > previous['quote']['event_epoch']:
        state['latest_quotes'][quote['symbol']] = {'quote': quote, 'observed_epoch': now, 'conflict': False}
    elif quote['event_epoch'] == previous['quote']['event_epoch']:
        if any(quote[k] != previous['quote'][k] for k in market_fields):
            previous['conflict'] = True
        # Equal market values never refresh the earliest observation's clock.


def _time_problem(state, order, quote, now, config):
    if quote['event_epoch'] <= order['decision_epoch'] or quote['available_epoch'] <= order['decision_epoch']:
        return 'QUOTE_NOT_STRICTLY_AFTER_DECISION'
    if quote['event_epoch'] < order['decision_epoch'] + config.min_latency_seconds:
        return 'QUOTE_BEFORE_MINIMUM_LATENCY'
    if now - quote['event_epoch'] > config.max_quote_age_seconds:
        return 'QUOTE_STALE'
    latest = state['latest_quotes'][quote['symbol']]
    if latest['conflict']:
        return 'LATEST_QUOTE_CONFLICT'
    if quote['event_epoch'] < latest['quote']['event_epoch']:
        return 'OUT_OF_ORDER_QUOTE'
    return None


def _fill_values(state, order, quote, now, config):
    problem = _time_problem(state, order, quote, now, config)
    if problem:
        return None, problem
    quantity, side = order['quantity'], order['side']
    if quote['ask_size' if side == 'BUY' else 'bid_size'] < quantity:
        return None, 'INSUFFICIENT_DISPLAYED_SIZE'
    raw_price = _decimal(quote['ask' if side == 'BUY' else 'bid'])
    slip = raw_price * _decimal(config.slippage_bps) / Decimal(10000)
    price = raw_price + slip if side == 'BUY' else raw_price - slip
    if side == 'BUY' and order['limit_price'] is not None and price > _decimal(order['limit_price']):
        return None, 'ENTRY_PRICE_LIMIT_EXCEEDED'
    amount = _cents(price * quantity)
    commission = _cents(max(_decimal(config.minimum_commission), _decimal(config.commission_per_share) * quantity))
    cash, realized = _decimal(state['cash']), _decimal(state['realized_net'])
    if side == 'BUY':
        debit = amount + commission
        total_basis = sum((_decimal(p['cost_basis']) for p in state['positions'].values()), Decimal(0))
        if debit > cash or total_basis + debit > _cents(config.max_notional):
            return None, 'INSUFFICIENT_FUNDED_CAPITAL_OR_NOTIONAL_LIMIT'
        cash -= debit
        position = {'symbol': order['symbol'], 'quantity': quantity, 'cost_basis': str(debit),
                    'entry_order_id': order['order_id'], 'entry_epoch': now}
        delta = Decimal('0.00')
    else:
        position = state['positions'][order['symbol']]
        delta = amount - commission - _decimal(position['cost_basis'])
        cash += amount - commission
        realized += delta
    return {'order_id': order['order_id'], 'quote_id': quote['observation_id'], 'symbol': order['symbol'],
            'side': side, 'quantity': quantity, 'decision_epoch': order['decision_epoch'], 'execution_epoch': now,
            'observed_latency_seconds': now - order['decision_epoch'], 'quote_price': str(raw_price),
            'slippage_per_share': str(slip), 'fill_price': str(price), 'notional': str(amount), 'commission': str(commission),
            'cash_after': str(cash), 'realized_net_delta': str(delta), 'realized_net_after': str(realized),
            'cost_basis': position['cost_basis'], 'fill_type': 'SIMULATED_PAPER_FILL', 'authority': AUTHORITY}, None


def _apply_fill(state, fill):
    order = state['orders'][fill['order_id']]
    if fill['side'] == 'BUY':
        state['positions'][fill['symbol']] = {'symbol': fill['symbol'], 'quantity': fill['quantity'],
            'cost_basis': fill['cost_basis'], 'entry_order_id': fill['order_id'], 'entry_epoch': fill['execution_epoch']}
    else:
        del state['positions'][fill['symbol']]
    state['cash'], state['realized_net'] = fill['cash_after'], fill['realized_net_after']
    state['fill_count'] += 1
    order.update(status='FILLED', fill=fill)


class PaperBook:
    def __init__(self, db_path: Path, *, config: PaperAccountConfig | None = None, mode='SYNTHETIC_PAPER'):
        self.path = Path(db_path)
        _require(not self.path.is_symlink(), 'PAPER_DATABASE_SYMLINK_REFUSED')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.config = config or PaperAccountConfig()
        self.contract = _contract(self.config, mode)
        self.db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        try:
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.execute('PRAGMA foreign_keys=ON')
            with self._transaction():
                self.db.execute('CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY, kind TEXT NOT NULL, epoch REAL NOT NULL, payload TEXT NOT NULL, prev_hash TEXT NOT NULL, hash TEXT NOT NULL UNIQUE, dedup TEXT UNIQUE)')
                self.db.execute('CREATE TABLE IF NOT EXISTS account (singleton INTEGER PRIMARY KEY CHECK(singleton=1), contract TEXT NOT NULL, state TEXT NOT NULL)')
                record = self.db.execute('SELECT contract,state FROM account WHERE singleton=1').fetchone()
                if record is None:
                    _require(self.db.execute('SELECT COUNT(*) FROM events').fetchone()[0] == 0, 'PAPER_ORPHANED_EVENT_HISTORY')
                    state = _initial(self.config)
                    self.db.execute('INSERT INTO account VALUES (1,?,?)', (canonical(self.contract), canonical(state)))
                    self._append('ACCOUNT_OPEN', 0, self.contract, 'ACCOUNT_OPEN')
                else:
                    _require(record[0] == canonical(self.contract), 'PAPER_RESTART_CONTRACT_DISAGREES')
                document = self._document()
                result = verify_paper_book(document)
                _require(result['status'] == 'VALID', 'PAPER_RECOVERY_VERIFICATION_FAILED:' + ';'.join(result['problems']))
        except BaseException:
            self.db.close()
            raise

    @contextmanager
    def _transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def _state(self):
        return json.loads(self.db.execute('SELECT state FROM account WHERE singleton=1').fetchone()[0])

    def _save(self, state):
        self.db.execute('UPDATE account SET state=? WHERE singleton=1', (canonical(state),))

    def _append(self, kind, epoch, payload, dedup=None):
        previous = self.db.execute('SELECT seq,hash FROM events ORDER BY seq DESC LIMIT 1').fetchone()
        row = {'seq': previous[0] + 1 if previous else 1, 'prev_hash': previous[1] if previous else 'GENESIS',
               'kind': kind, 'epoch': epoch, 'payload': payload}
        row['hash'] = digest(row)
        self.db.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?)',
            (row['seq'], kind, epoch, canonical(payload), row['prev_hash'], row['hash'], dedup))
        return row

    def _record_decision(self, state, decision_id, now, evidence):
        _require(_identifier(decision_id) and isinstance(evidence, dict) and bool(evidence)
                 and len(canonical(evidence).encode()) <= 32768, 'PAPER_DECISION_EVIDENCE_REQUIRED')
        payload = {'decision_id': decision_id, 'decision_epoch': now, 'evidence': evidence}
        existing = state['decisions'].get(decision_id)
        if existing is not None:
            _require(canonical(existing) == canonical(payload), 'PAPER_DECISION_ID_REUSED')
            return existing
        _require(now >= state['clock'], 'PAPER_ACCOUNT_CLOCK_REWIND')
        self._append('DECISION_RECORDED', now, payload, 'DECISION:' + decision_id)
        state['decisions'][decision_id] = payload
        state['clock'] = now
        return payload

    def record_decision(self, decision_id, *, now, payload):
        _clock(now)
        with self._transaction():
            state = self._state()
            result = self._record_decision(state, decision_id, now, payload)
            self._save(state)
            return result

    def submit_order(self, order_id, *, symbol, side, quantity, decision_epoch, decision_id, evidence, expires_epoch=None, limit_price=None):
        order = _order_record(order_id, symbol, side, quantity, decision_epoch, decision_id, evidence, expires_epoch, self.config, limit_price)
        with self._transaction():
            state = self._state()
            if order_id in state['orders']:
                existing = state['orders'][order_id]
                _require(all(existing[k] == v for k, v in order.items()), 'PAPER_ORDER_ID_REUSED_WITH_DIFFERENT_TERMS')
                return existing
            _require(decision_epoch >= state['clock'], 'PAPER_ACCOUNT_CLOCK_REWIND')
            _admit_order(state, order)
            self._record_decision(state, decision_id, decision_epoch, evidence)
            event = self._append('ORDER_SUBMITTED', decision_epoch, order, 'ORDER:' + order_id)
            state['orders'][order_id] = {**order, 'status': 'PENDING', 'submitted_seq': event['seq']}
            state['clock'] = decision_epoch
            self._save(state)
            return state['orders'][order_id]

    def _expire(self, state, now):
        expired = []
        for order in sorted(state['orders'].values(), key=lambda o: o['submitted_seq']):
            if order['status'] == 'PENDING' and now >= order['expires_epoch']:
                self._append('ORDER_CLOSED', now, {'order_id': order['order_id'], 'status': 'EXPIRED', 'reason': 'ORDER_TTL_EXPIRED'})
                order.update(status='EXPIRED', closed_epoch=now, close_reason='ORDER_TTL_EXPIRED')
                expired.append(order['order_id'])
        return expired

    def expire_orders(self, *, now):
        _clock(now)
        with self._transaction():
            state = self._state()
            _require(now >= state['clock'], 'PAPER_ACCOUNT_CLOCK_REWIND')
            expired = self._expire(state, now)
            self._append('CLOCK_ADVANCED', now, {})
            state['clock'] = now
            self._save(state)
            return expired

    def cancel_order(self, order_id, *, now, reason='OPERATOR_CANCELLED'):
        _clock(now)
        _require(_identifier(reason), 'PAPER_CANCEL_REASON_INVALID')
        with self._transaction():
            state = self._state()
            _require(order_id in state['orders'], 'PAPER_ORDER_NOT_FOUND')
            order = state['orders'][order_id]
            if order['status'] != 'PENDING':
                return order
            _require(now >= state['clock'], 'PAPER_ACCOUNT_CLOCK_REWIND')
            self._append('ORDER_CLOSED', now, {'order_id': order_id, 'status': 'CANCELLED', 'reason': reason})
            order.update(status='CANCELLED', closed_epoch=now, close_reason=reason)
            state['clock'] = now
            self._save(state)
            return order

    def process_quote(self, quote, *, now):
        _clock(now)
        quote = _quote(quote, self.contract)
        _require(quote['available_epoch'] <= now, 'PAPER_QUOTE_NOT_YET_AVAILABLE')
        with self._transaction():
            if self.db.execute('SELECT 1 FROM events WHERE dedup=?', ('QUOTE:' + quote['observation_id'],)).fetchone():
                return {'duplicate': True, 'fills': [], 'waits': [], 'expired': []}
            state = self._state()
            _require(now >= state['clock'], 'PAPER_ACCOUNT_CLOCK_REWIND')
            expired = self._expire(state, now)
            self._append('QUOTE_OBSERVED', now, quote, 'QUOTE:' + quote['observation_id'])
            _observe_quote(state, quote, now)
            fills, waits = [], []
            for order in sorted(state['orders'].values(), key=lambda o: o['submitted_seq']):
                if order['status'] != 'PENDING' or order['symbol'] != quote['symbol']:
                    continue
                fill, problem = _fill_values(state, order, quote, now, self.config)
                if problem:
                    wait = {'order_id': order['order_id'], 'quote_id': quote['observation_id'], 'reason': problem}
                    self._append('ORDER_WAIT', now, wait)
                    order['last_wait_reason'] = problem
                    waits.append(wait)
                else:
                    self._append('FILL', now, fill, 'FILL:' + order['order_id'])
                    _apply_fill(state, fill)
                    fills.append(fill)
            state['clock'] = now
            self._save(state)
            return {'duplicate': False, 'fills': fills, 'waits': waits, 'expired': expired}

    def get_order(self, order_id):
        with self._transaction():
            state = self._state()
            _require(order_id in state['orders'], 'PAPER_ORDER_NOT_FOUND')
            return state['orders'][order_id]

    def snapshot(self, *, now):
        _clock(now)
        with self._transaction():
            state = self._state()
            _require(now >= state['clock'], 'PAPER_SNAPSHOT_PRECEDES_ACCOUNT_STATE')
            return _snapshot(state, self.contract, now)

    def _document(self):
        contract, state = self.db.execute('SELECT contract,state FROM account WHERE singleton=1').fetchone()
        events = []
        for r in self.db.execute('SELECT seq,kind,epoch,payload,prev_hash,hash,dedup FROM events ORDER BY seq'):
            payload = json.loads(r[3])
            if r[1] == 'ACCOUNT_OPEN':
                expected_key = 'ACCOUNT_OPEN'
            elif r[1] == 'DECISION_RECORDED':
                expected_key = 'DECISION:' + payload['decision_id']
            elif r[1] == 'ORDER_SUBMITTED':
                expected_key = 'ORDER:' + payload['order_id']
            elif r[1] == 'QUOTE_OBSERVED':
                expected_key = 'QUOTE:' + payload['observation_id']
            elif r[1] == 'FILL':
                expected_key = 'FILL:' + payload['order_id']
            else:
                expected_key = None
            _require(r[6] == expected_key, 'PAPER_IDEMPOTENCY_INDEX_DISAGREES')
            events.append({'seq': r[0], 'kind': r[1], 'epoch': r[2], 'payload': payload, 'prev_hash': r[4], 'hash': r[5]})
        # SQLite REAL affinity normalizes integer epochs; restore original hash representation below.
        for row in events:
            if row['epoch'].is_integer():
                integer = {**row, 'epoch': int(row['epoch'])}
                if digest({k: v for k, v in integer.items() if k != 'hash'}) == row['hash']:
                    row['epoch'] = integer['epoch']
        return {'schema': 'APEX_PAPER_BOOK_EXPORT_V1', 'contract': json.loads(contract), 'events': events, 'state': json.loads(state)}

    def export(self, path: Path):
        with self._transaction():
            document = self._document()
            result = verify_paper_book(document)
            _require(result['status'] == 'VALID', 'PAPER_EXPORT_VERIFICATION_FAILED')
        with Path(path).open('x', encoding='utf-8') as handle:
            handle.write(canonical(document))
        return result

    def verify(self):
        with self._transaction():
            return verify_paper_book(self._document())

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def _snapshot(state, contract, now):
    positions, all_marked = [], True
    market_value, unrealized = Decimal('0.00'), Decimal('0.00')
    cfg = contract['config']
    for symbol, position in sorted(state['positions'].items()):
        latest = state['latest_quotes'].get(symbol)
        quote = latest['quote'] if latest else None
        status = 'NO_QUOTE' if quote is None else 'CONFLICTING_QUOTE' if latest['conflict'] else 'STALE_QUOTE' if now - quote['event_epoch'] > cfg['max_quote_age_seconds'] else 'MARKED'
        gross, net, pnl = None, None, None
        if status == 'MARKED' and quote['bid_size'] < position['quantity']:
            status = 'INSUFFICIENT_MARK_SIZE'
        if status == 'MARKED':
            gross = _cents(_decimal(quote['bid']) * position['quantity'])
            slipped = _decimal(quote['bid']) * (Decimal(1) - _decimal(cfg['slippage_bps']) / Decimal(10000))
            fee = _cents(max(_decimal(cfg['minimum_commission']), _decimal(cfg['commission_per_share']) * position['quantity']))
            net = _cents(slipped * position['quantity']) - fee
            pnl = net - _decimal(position['cost_basis'])
            market_value += gross
            unrealized += pnl
        else:
            all_marked = False
        entry = state['orders'][position['entry_order_id']]
        positions.append({**position, 'entry_decision_id': entry['decision_id'], 'entry_evidence': entry['evidence'], 'mark_status': status, 'mark_quote_id': quote['observation_id'] if quote else None,
                          'mark_event_epoch': quote['event_epoch'] if quote else None,
                          'bid_mark': str(_decimal(quote['bid'])) if status == 'MARKED' else None,
                          'market_value': str(gross) if gross is not None else None,
                          'estimated_liquidation_value': str(net) if net is not None else None,
                          'unrealized_net': str(pnl) if pnl is not None else None})
    cash, realized = _decimal(state['cash']), _decimal(state['realized_net'])
    total_net = realized + unrealized if all_marked else None
    return {'schema': 'APEX_PAPER_ACCOUNT_SNAPSHOT_V1', 'mode': contract['mode'], 'as_of_epoch': now,
            'cash': str(cash), 'available_cash': str(cash), 'reserved_cash': '0.00',
            'pending_funding_policy': contract['pending_funding_policy'], 'positions': positions, 'quantity': sum(p['quantity'] for p in positions),
            'cost_basis': str(sum((_decimal(p['cost_basis']) for p in positions), Decimal('0.00'))),
            'realized_net': str(realized), 'unrealized_net': str(unrealized) if all_marked else None,
            'market_value': str(market_value) if all_marked else None,
            'equity': str(cash + market_value) if all_marked else None,
            'estimated_liquidation_equity': str(_decimal(cfg['starting_cash']) + total_net) if total_net is not None else None,
            'total_net_pnl': str(total_net) if total_net is not None else None,
            'mark_status': 'COMPLETE' if all_marked else 'UNRESOLVED',
            'pending_orders': [o for o in state['orders'].values() if o['status'] == 'PENDING'],
            'orders': len(state['orders']), 'fills': state['fill_count'], 'authority': AUTHORITY,
            'pnl_basis': 'SIMULATED_PAPER_ONLY; unrealized net deducts assumed exit slippage and commission',
            'provenance_scope': 'Validated retained fields; source authenticity, exchange execution and broker fills are not established'}


def _audit_fill(state, order, quote, now, cfg):
    """Separate fee, debit, basis and P&L arithmetic; never calls producer fill math."""
    problem = _time_problem(state, order, quote, now, cfg)
    if problem:
        return None, problem
    qty, side = order['quantity'], order['side']
    if quote['ask_size' if side == 'BUY' else 'bid_size'] < qty:
        return None, 'INSUFFICIENT_DISPLAYED_SIZE'
    rounded = lambda x: x.quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
    raw = Decimal(str(quote['ask' if side == 'BUY' else 'bid']))
    slippage = raw * Decimal(cfg.slippage_bps) / Decimal('10000')
    px = raw + slippage if side == 'BUY' else raw - slippage
    if side == 'BUY' and order['limit_price'] is not None and px > Decimal(order['limit_price']):
        return None, 'ENTRY_PRICE_LIMIT_EXCEEDED'
    value = rounded(px * Decimal(qty))
    charge = rounded(max(Decimal(cfg.minimum_commission), Decimal(qty) * Decimal(cfg.commission_per_share)))
    cash = Decimal(state['cash'])
    realized = Decimal(state['realized_net'])
    if side == 'BUY':
        basis = value + charge
        existing = sum((Decimal(p['cost_basis']) for p in state['positions'].values()), Decimal(0))
        if basis > cash or basis + existing > Decimal(cfg.max_notional):
            return None, 'INSUFFICIENT_FUNDED_CAPITAL_OR_NOTIONAL_LIMIT'
        resulting_cash, delta = cash - basis, Decimal('0.00')
    else:
        basis = Decimal(state['positions'][order['symbol']]['cost_basis'])
        resulting_cash = cash + value - charge
        delta = value - charge - basis
        realized += delta
    return {'order_id': order['order_id'], 'quote_id': quote['observation_id'], 'symbol': order['symbol'],
            'side': side, 'quantity': qty, 'decision_epoch': order['decision_epoch'], 'execution_epoch': now,
            'observed_latency_seconds': now - order['decision_epoch'], 'quote_price': str(raw),
            'slippage_per_share': str(slippage), 'fill_price': str(px), 'notional': str(value), 'commission': str(charge),
            'cash_after': str(resulting_cash), 'realized_net_delta': str(delta), 'realized_net_after': str(realized),
            'cost_basis': str(basis), 'fill_type': 'SIMULATED_PAPER_FILL', 'authority': AUTHORITY}, None


def _verify_document(document):
    _require(isinstance(document, dict) and set(document) == {'schema', 'contract', 'events', 'state'}
             and document['schema'] == 'APEX_PAPER_BOOK_EXPORT_V1', 'PAPER_EXPORT_SCHEMA_INVALID')
    contract = document['contract']
    cfg = PaperAccountConfig(**contract['config'])
    _require(canonical(contract) == canonical(_contract(cfg, contract['mode'])), 'PAPER_ACCOUNT_CONTRACT_DISAGREES')
    events = document['events']
    _require(isinstance(events, list) and events, 'PAPER_ACCOUNT_HISTORY_MISSING')
    state, previous, last_epoch = _initial(cfg), 'GENESIS', 0
    quote_ids, queue, current_quote = set(), [], None
    for index, row in enumerate(events, 1):
        _require(isinstance(row, dict) and set(row) == {'seq', 'kind', 'epoch', 'payload', 'prev_hash', 'hash'}, 'PAPER_EVENT_SCHEMA_INVALID')
        _require(type(row['seq']) is int and row['seq'] == index and row['prev_hash'] == previous
                 and row['hash'] == digest({k: v for k, v in row.items() if k != 'hash'}), 'PAPER_EVENT_CHAIN_INVALID')
        _clock(row['epoch'])
        _require(row['epoch'] >= last_epoch, 'PAPER_EVENT_CLOCK_REWIND')
        kind, now, p = row['kind'], row['epoch'], row['payload']
        _require(isinstance(p, dict), 'PAPER_EVENT_PAYLOAD_INVALID')
        if queue:
            _require(kind in ('FILL', 'ORDER_WAIT') and p.get('order_id') == queue[0], 'PAPER_QUOTE_ORDER_EVALUATION_MISSING')
        if index == 1:
            _require(kind == 'ACCOUNT_OPEN' and now == 0 and canonical(p) == canonical(contract), 'PAPER_ACCOUNT_OPEN_INVALID')
        elif kind == 'DECISION_RECORDED':
            _require(set(p) == {'decision_id', 'decision_epoch', 'evidence'} and _identifier(p['decision_id'])
                     and p['decision_epoch'] == now and isinstance(p['evidence'], dict) and bool(p['evidence'])
                     and len(canonical(p['evidence']).encode()) <= 32768 and p['decision_id'] not in state['decisions'],
                     'PAPER_DECISION_HISTORY_INVALID')
            state['decisions'][p['decision_id']] = p
        elif kind == 'ORDER_SUBMITTED':
            order = _order_record(p['order_id'], p['symbol'], p['side'], p['quantity'], p['decision_epoch'], p['decision_id'], p['evidence'], p['expires_epoch'], cfg, p['limit_price'])
            _require(state['decisions'].get(p['decision_id']) == {'decision_id': p['decision_id'],
                     'decision_epoch': now, 'evidence': p['evidence']}, 'PAPER_ORDER_DECISION_NOT_BOUND')
            _require(canonical(order) == canonical(p) and now == p['decision_epoch'] and p['order_id'] not in state['orders'], 'PAPER_ORDER_HISTORY_INVALID')
            _admit_order(state, order)
            state['orders'][order['order_id']] = {**order, 'status': 'PENDING', 'submitted_seq': index}
        elif kind == 'QUOTE_OBSERVED':
            quote = _quote(p, contract)
            _require(canonical(quote) == canonical(p) and quote['available_epoch'] <= now and quote['observation_id'] not in quote_ids,
                     'PAPER_QUOTE_HISTORY_INVALID')
            _require(not any(o['status'] == 'PENDING' and o['expires_epoch'] <= now for o in state['orders'].values()), 'PAPER_EXPIRED_ORDER_NOT_CLOSED')
            quote_ids.add(quote['observation_id'])
            _observe_quote(state, quote, now)
            current_quote = quote
            queue = [o['order_id'] for o in sorted(state['orders'].values(), key=lambda x: x['submitted_seq'])
                     if o['status'] == 'PENDING' and o['symbol'] == quote['symbol']]
        elif kind in ('FILL', 'ORDER_WAIT'):
            _require(queue and current_quote is not None and now == last_epoch, 'PAPER_FILL_WITHOUT_QUOTE_EVALUATION')
            order = state['orders'][queue.pop(0)]
            expected, reason = _audit_fill(state, order, current_quote, now, cfg)
            if reason:
                _require(kind == 'ORDER_WAIT' and p == {'order_id': order['order_id'], 'quote_id': current_quote['observation_id'], 'reason': reason}, 'PAPER_ORDER_WAIT_DISAGREES')
                order['last_wait_reason'] = reason
            else:
                _require(kind == 'FILL' and canonical(p) == canonical(expected), 'PAPER_FILL_ACCOUNTING_DISAGREES')
                # Apply independently reconstructed, never supplied monetary totals.
                if order['side'] == 'BUY':
                    state['positions'][order['symbol']] = {'symbol': order['symbol'], 'quantity': order['quantity'],
                        'cost_basis': expected['cost_basis'], 'entry_order_id': order['order_id'], 'entry_epoch': now}
                else:
                    del state['positions'][order['symbol']]
                state['cash'], state['realized_net'] = expected['cash_after'], expected['realized_net_after']
                state['fill_count'] += 1
                order.update(status='FILLED', fill=expected)
        elif kind == 'ORDER_CLOSED':
            _require(set(p) == {'order_id', 'status', 'reason'} and p['order_id'] in state['orders'], 'PAPER_ORDER_CLOSE_INVALID')
            order = state['orders'][p['order_id']]
            _require(order['status'] == 'PENDING' and p['status'] in ('EXPIRED', 'CANCELLED') and _identifier(p['reason']), 'PAPER_ORDER_CLOSE_INVALID')
            if p['status'] == 'EXPIRED':
                _require(now >= order['expires_epoch'] and p['reason'] == 'ORDER_TTL_EXPIRED', 'PAPER_EXPIRY_INVALID')
            order.update(status=p['status'], closed_epoch=now, close_reason=p['reason'])
        elif kind == 'CLOCK_ADVANCED':
            _require(p == {} and not any(o['status'] == 'PENDING' and o['expires_epoch'] <= now for o in state['orders'].values()), 'PAPER_CLOCK_ADVANCE_INVALID')
        else:
            raise Refused('PAPER_UNKNOWN_EVENT_KIND')
        state['clock'] = now
        previous, last_epoch = row['hash'], now
    _require(not queue, 'PAPER_QUOTE_ORDER_EVALUATION_MISSING')
    _require(canonical(state) == canonical(document['state']), 'PAPER_RECOVERED_STATE_DISAGREES')
    return {'status': 'VALID', 'problems': [], 'head': previous, 'events': len(events), 'orders': len(state['orders']),
            'fills': state['fill_count'], 'cash': state['cash'], 'realized_net': state['realized_net'],
            'mode': contract['mode'], 'authority': AUTHORITY,
            'scope': 'Independent Decimal fill/account reconstruction and event completeness; shared timing/provenance validators; not broker execution or external authenticity'}


def verify_paper_book(source):
    """Read an export dict/JSON or a SQLite database without mutating that source."""
    try:
        if isinstance(source, dict):
            document = source
        else:
            path = Path(source)
            _require(path.is_file() and not path.is_symlink(), 'PAPER_VERIFICATION_SOURCE_UNAVAILABLE')
            with path.open('rb') as handle:
                header = handle.read(16)
            if header == b'SQLite format 3\x00':
                connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, isolation_level=None)
                try:
                    connection.execute('BEGIN')
                    book = object.__new__(PaperBook)
                    book.db = connection
                    document = book._document()
                finally:
                    connection.close()
            else:
                document = json.loads(path.read_text())
        return _verify_document(document)
    except (Refused, ValueError, TypeError, KeyError, AttributeError, IndexError, OSError, sqlite3.Error, ArithmeticError) as exc:
        reason = str(exc) if isinstance(exc, Refused) else 'PAPER_ARTIFACT_INVALID:' + type(exc).__name__
        return {'status': 'MISMATCH', 'problems': [reason], 'authority': AUTHORITY}
