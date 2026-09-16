"""Local process controls; fake CLI fixtures never commission live AI access."""
import json
import os
from pathlib import Path
import sys
import time

import pytest

from apex import codex_planner as planner
from apex.core import Refused, canonical, digest


SYMBOLS = {'target_symbol':'AAPL', 'market_symbol':'SPY', 'sector_symbol':'XLK'}
PROPOSAL = {'schema':'APEX_RESEARCH_PROPOSAL_V1', 'action':'DEFER_MISSING_DATA', **SYMBOLS,
            'rationale':'Synthetic transport test only; no observed market or model result.'}


def events(proposal=None, tool=False):
    rows = [{'type':'thread.started','thread_id':'synthetic'}, {'type':'turn.started'}]
    if tool:
        rows.append({'type':'item.completed','item':{'type':'command_execution','command':'forbidden'}})
    rows += [{'type':'item.completed','item':{'type':'agent_message','text':canonical(proposal or PROPOSAL)}},
             {'type':'turn.completed','usage':{'input_tokens':1,'output_tokens':1}}]
    return b''.join(canonical(row).encode()+b'\n' for row in rows)


def fake_cli(tmp_path, *, auth='ChatGPT', version='0.154.0', tool=False, bad_file=False, isolation=True):
    path = tmp_path/'synthetic-codex'
    script = f'''#!{sys.executable}
import json,os,pathlib,sys
args=sys.argv[1:]
if args==['--version']:
 print('codex-cli {version}')
elif args==['login','status']:
 print('Logged in using {auth}')
elif args and args[0]=='sandbox':
 assert '--include-managed-config' in args
 assert 'permissions.apex_planner.network.enabled=false' in args
 assert 'approval_policy="never"' in args
 assert '--disable' in args
 print({'APEX_ISOLATION_VERIFIED' if isolation else 'ISOLATION_UNAVAILABLE'!r})
elif args and args[0]=='exec':
 assert not any(k in os.environ for k in ('OPENAI_API_KEY','CODEX_API_KEY','CODEX_ACCESS_TOKEN','ALPACA_API_KEY'))
 assert '--ignore-user-config' in args and '--strict-config' in args
 assert '--ephemeral' in args and '--json' in args
 assert '--ignore-rules' not in args
 assert '--dangerously-bypass-approvals-and-sandbox' not in args
 assert 'shell_tool' in args and 'apps' in args and 'plugins' in args
 assert 'shell_environment_policy.inherit="none"' in args
 text=sys.stdin.read()
 assert 'SYNTHETIC_TEST_SECRET' not in text
 schema=json.loads(pathlib.Path(args[args.index('--output-schema')+1]).read_text())
 assert schema['additionalProperties'] is False
 target=pathlib.Path(args[args.index('--output-last-message')+1])
 target.write_text({canonical({**PROPOSAL, 'action':'RUN_PEER_DISLOCATION_STUDY'} if bad_file else PROPOSAL)!r})
 sys.stdout.write({events(tool=tool).decode()!r})
else:
 sys.exit(5)
'''
    path.write_text(script)
    path.chmod(0o700)
    return path


def test_supported_saved_auth_and_structured_cli_transport(tmp_path, monkeypatch):
    executable = fake_cli(tmp_path)
    for key in ('OPENAI_API_KEY','CODEX_API_KEY','CODEX_ACCESS_TOKEN','ALPACA_API_KEY'):
        monkeypatch.setenv(key, 'SYNTHETIC_TEST_SECRET')
    status = planner.codex_status(executable=executable)
    assert status['auth_method']=='CHATGPT' and status['runtime_llm_call_completed'] is False
    context = {'input_class':'SYNTHETIC_RESEARCH_CONTROL','symbols':SYMBOLS}
    result = planner.request_codex_plan(context, SYMBOLS, 'synthetic-model', executable=executable)
    assert result['proposal'] == PROPOSAL
    assert result['provenance'] == planner.PROVENANCE
    assert result['request_digest'] == digest(planner.make_codex_request(context,SYMBOLS,'synthetic-model'))
    assert 'SYNTHETIC_TEST_SECRET' not in canonical(result)


@pytest.mark.parametrize('auth', ['API key', 'Workload identity', 'Unauthenticated'])
def test_non_chatgpt_auth_never_falls_back_to_api_or_starts_inference(tmp_path, monkeypatch, auth):
    binary = fake_cli(tmp_path,auth=auth)
    monkeypatch.setattr(planner, '_isolation_probe', lambda *a: pytest.fail('Inference gate must stop before probe'))
    with pytest.raises(Refused,match='SAVED_CHATGPT_LOGIN_REQUIRED'):
        planner.request_codex_plan({},SYMBOLS,'synthetic-model',executable=binary)


def test_missing_binary_and_unreviewed_version_refuse(tmp_path, monkeypatch):
    monkeypatch.delenv('APEX_CODEX_EXECUTABLE',raising=False)
    monkeypatch.setattr(planner.shutil,'which',lambda name:None)
    assert planner.codex_status()['reason'].endswith('CLI_NOT_INSTALLED')
    assert planner.codex_status(executable=fake_cli(tmp_path,version='99.0.0'))['reason'].endswith('UNREVIEWED_CLI_VERSION')


def test_operator_path_selection_is_outside_model_control(tmp_path,monkeypatch):
    binary=fake_cli(tmp_path)
    monkeypatch.setenv('APEX_CODEX_EXECUTABLE',str(binary))
    assert planner.codex_status()['auth_method']=='CHATGPT'
    with pytest.raises(Refused,match='EXPLICIT_MODEL'):
        planner.make_codex_request({},SYMBOLS,'synthetic; touch /tmp/forbidden')


@pytest.mark.parametrize('mode',['tool','file','isolation'])
def test_actual_reader_rejects_tool_events_different_final_file_and_failed_isolation(tmp_path,mode):
    binary=fake_cli(tmp_path,tool=mode=='tool',bad_file=mode=='file',isolation=mode!='isolation')
    with pytest.raises(Refused):
        planner.request_codex_plan({},SYMBOLS,'synthetic-model',executable=binary)


def test_process_wall_bound_includes_child_inherited_streams(tmp_path):
    program='import os,time; child=os.fork(); time.sleep(30) if child==0 else None'
    started=time.monotonic()
    with pytest.raises(Refused,match='WALL_TIME'):
        planner._run([sys.executable,'-c',program],cwd=tmp_path,timeout=.15)
    assert time.monotonic()-started<3


@pytest.mark.parametrize('channel',['stdout','stderr'])
def test_process_output_bounds_do_not_buffer_unbounded_streams(tmp_path,channel):
    program=f'import sys; sys.{channel}.write("x"*400000);sys.{channel}.flush()'
    with pytest.raises(Refused,match='OUTPUT_BYTES'):
        planner._run([sys.executable,'-c',program],cwd=tmp_path,timeout=3)


def test_process_complete_io_handles_bounded_stdin_without_shell(tmp_path):
    code='import sys; data=sys.stdin.buffer.read();sys.stdout.buffer.write(data)'
    payload=b'x'*30000
    result=planner._run([sys.executable,'-c',code],cwd=tmp_path,stdin=payload,timeout=3)
    assert result==(0,payload,b'')


def test_truncated_and_extra_turn_or_fields_are_rejected():
    good=events()
    with pytest.raises(Refused,match='COMPLETE_SINGLE'):
        planner._parse_events(b'\n'.join(good.splitlines()[:-1]),SYMBOLS)
    with pytest.raises(Refused):
        planner._parse_events(good+b'{"type":"turn.started"}\n',SYMBOLS)
    with pytest.raises(Refused,match='FIELDS_NOT_ALLOWLISTED'):
        planner._parse_events(events({**PROPOSAL,'order_quantity':1000}),SYMBOLS)


def test_canary_probe_rejects_actual_boundary_failure(tmp_path,monkeypatch):
    workspace=tmp_path/'workspace';workspace.mkdir()
    def unsafe(args,**kwargs):
        (workspace/'readable.txt').write_text('unauthorized change')
        return 0,b'APEX_ISOLATION_VERIFIED\n',b''
    monkeypatch.setattr(planner,'_run',unsafe)
    with pytest.raises(Refused,match='BOUNDARY_NOT_ESTABLISHED'):
        planner._isolation_probe(Path('/synthetic/codex'),workspace,tmp_path)


def test_request_binds_security_policy_and_is_bounded():
    request=planner.make_codex_request({},SYMBOLS,'synthetic-model')
    assert request['configuration']['permissions.apex_planner.filesystem'][':root']=='deny'
    assert request['configuration']['permissions.apex_planner.network.enabled'] is False
    assert request['auth']=='SAVED_CHATGPT_CLI_ONLY'
    with pytest.raises(Refused,match='REQUEST_BYTES'):
        planner.make_codex_request({'oversized':'x'*40000},SYMBOLS,'synthetic-model')
