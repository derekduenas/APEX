"""Synthetic fixtures exercise the measured CONTRACT; none constitute live commissioning."""
from dataclasses import asdict, replace
import json
from pathlib import Path

import pytest

from apex import paper_feed as feed
from apex import paper_runtime as runtime
from apex.core import Refused, digest
from apex.paper_book import PaperAccountConfig
from apex.paper_service import tick_files
from apex.runtime import runtime_lock
from test_paper_runtime import fixture
from test_paper_feed import NOW, transport_for, pair


def setup(tmp_path, monkeypatch):
    doc, start, calendar, config = fixture()
    # Explicitly generated fixtures for boundary testing, never real-market artifacts.
    doc['source'] = 'ALPACA_REST_CAPTURE'
    for row in doc['observations']:
        row.update(availability_basis='MEASURED_RECEIPT', feed='sip', market_coverage='SIP',
                   raw_response_sha256='a'*64, receipt_basis='PARENT_PROCESS_RECEIPT_UPPER_BOUND')
        row['event_ns'] = int(row['event_epoch']) * 10**9
        row['available_ns'] = int(row['available_epoch']) * 10**9
        row['provider_timestamp_ns'] = row['event_ns']
        row['available_epoch_ns'] = row['available_ns']
    calendar = replace(calendar, calendar_source='ALPACA_TRADING_CALENDAR_V2')
    settings = tmp_path/'settings.json'
    settings.write_text(json.dumps({'schema':'APEX_PAPER_SERVICE_V1', 'model':config.record(),
                                   'account':asdict(PaperAccountConfig(feed="sip"))}))
    clock = [start]
    monkeypatch.setattr(runtime.time, 'time', lambda:clock[0])
    root = tmp_path/'feed'
    account = tmp_path/'account'
    def publish(at, name, calendar_override=None):
        document = {**doc, 'observations':[r for r in doc['observations'] if r['available_epoch'] <= at],
                    'generation_id':name, 'provenance':{'retrieved_epoch':at}}
        session = {'generation_id':name, 'session':asdict(calendar_override or calendar)}
        feed.publish_generation(root, root/'generations'/name, document, session)
        return document,session
    def tick(at):
        clock[0] = at
        return tick_files(account, settings_path=settings, generation_root=root)
    return root,account,start,publish,tick


def test_failure_through_service_preserves_position_and_removes_marks(tmp_path, monkeypatch):
    root,account,start,publish,tick = setup(tmp_path, monkeypatch)
    publish(start,'g1'); first=tick(start)
    assert first['account']['pending_orders']
    publish(start+60,'g2'); opened=tick(start+60)
    assert opened['account']['fills']==1 and opened['account']['positions']
    feed._publish(root/'publisher-status.json', {'status':'FAILED','reason':'CAPTURE_CONTAINS_NO_QUOTE'})
    blocked=tick(start+61)  # The old quote is still fresh: publisher failure itself must block it.
    assert blocked['status']=='BLOCKED_SOURCE'
    assert blocked['feed']['problem']=='FEED_PUBLISHER_NOT_HEALTHY'
    assert blocked['account']['fills']==1
    assert blocked['account']['positions'][0]['quantity']==opened['account']['positions'][0]['quantity']
    later=tick(start+180)
    assert later['account']['positions'][0]['bid_mark'] is None
    assert later['account']['positions'][0]['unrealized_net'] is None
    assert runtime.report(account, now=start+180)['verification']['status']=='VALID'


def test_actual_pending_order_expires_through_service_when_feed_stops(tmp_path, monkeypatch):
    root,account,start,publish,tick=setup(tmp_path, monkeypatch)
    publish(start,'g1'); first=tick(start)
    assert len(first['account']['pending_orders'])==1 and first['account']['fills']==0
    later=tick(start+180)
    assert later['feed']['problem']=='FEED_GENERATION_STALE_OR_FUTURE'
    assert later['account']['pending_orders']==[] and later['account']['fills']==0
    assert runtime.report(account, now=start+180)['verification']['status']=='VALID'


def test_swap_between_reads_through_actual_tick_files(tmp_path, monkeypatch):
    root,account,start,publish,tick=setup(tmp_path, monkeypatch)
    publish(start,'g1')
    real=feed.read_json
    swapped=[]
    def read(path, **kwargs):
        result=real(path, **kwargs)
        if Path(path).name=='input.json' and not swapped:
            swapped.append(True); publish(start,'g2')
        return result
    monkeypatch.setattr(feed,'read_json',read)
    result=tick(start)
    assert swapped and result['feed']['generation_id']=='g1'
    assert result['source_problem'] is None
    assert (root/'current').resolve().name=='g2'


def test_calendar_receipt_and_two_real_service_ticks_share_pinned_facts(tmp_path, monkeypatch):
    root,account,start,publish,tick=setup(tmp_path, monkeypatch)
    from datetime import datetime
    from apex.data import EASTERN
    day=datetime.fromtimestamp(start,EASTERN).date().isoformat()
    transport=transport_for(calendar=[{'date':day,'open':'09:30','close':'16:00'}])
    stamps=iter([int((start-5)*1e9), int((start-4)*1e9)+123456789])
    one,evidence=feed.pinned_session(root, now=start-5, transport=transport, clock_ns=lambda:next(stamps))
    stamps=iter([int((start+60)*1e9),int((start+61)*1e9)])
    two,_=feed.pinned_session(root, now=start+60, transport=transport, clock_ns=lambda:next(stamps))
    assert one.record()==two.record()
    assert evidence['received_ns']>evidence['requested_ns']
    assert one.known_at_epoch==evidence['received_ns']/1e9
    publish(start,'g1',one); a=tick(start)
    publish(start+61,'g2',two); b=tick(start+61)
    assert a['source_problem'] is None and b['source_problem'] is None
    assert b['status']!='BLOCKED_SERVICE_INPUT'


def test_generation_reuse_and_overlapping_writer_refuse(tmp_path):
    a,session,_=pair(tmp_path,'a'); dest=tmp_path/'generations'/'g1'
    feed.publish_generation(tmp_path,dest,a,session)
    before=(dest/'input.json').read_bytes()
    with pytest.raises(Refused,match='ALREADY_WRITTEN'):
        feed.publish_generation(tmp_path,dest,a,session)
    assert (dest/'input.json').read_bytes()==before
    with runtime_lock(tmp_path/'writer-lock'):
        with pytest.raises(Refused,match='RUNTIME_ALREADY_ACTIVE'):
            feed.publish_generation(tmp_path,tmp_path/'generations'/'g2',a,session)


def test_failure_record_does_not_echo_exception_text(tmp_path, monkeypatch):
    def fail(*args,**kwargs): raise OSError('private-server-text-secret')
    monkeypatch.setattr(feed,'_publish_once',fail)
    with pytest.raises(Refused, match='PUBLISHER_OPERATION_FAILED'): feed.publish(tmp_path,settings={})
    assert 'private-server-text-secret' not in (tmp_path/'publisher-status.json').read_text()
