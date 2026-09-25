"""Exercise native ownership through a real framed child and real file effects."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import os
from pathlib import Path
import shlex
import sys

import pytest

from meadow_bridge.json_types import JsonObject, json_object, json_text, parse_json
from meadow_bridge.native_client import NativeClient
from meadow_bridge.native_transport import NativeProtocolError
from meadow_bridge.native_types import (
    AllocatedBinding, ConversationBinding, NativeCancelled, NativeCompleted,
    NativeFailed, NativeTerminal, NativeUnsettledError,
)
from meadow_bridge.permission_policy import PermissionPolicy


_SERVER = r'''
import json, os, sys, time
from pathlib import Path
config = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
log = Path('requests.jsonl')
turn_number = 0
registered = False

def receive():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line: raise EOFError
        if line == b'\r\n': break
        key, value = line.decode().split(':', 1)
        headers[key.lower()] = value.strip()
    message = json.loads(sys.stdin.buffer.read(int(headers['content-length'])))
    with log.open('a', encoding='utf-8') as stream: stream.write(json.dumps(message) + '\n')
    return message

def send(message):
    body = json.dumps(message).encode()
    sys.stdout.buffer.write(f'Content-Length: {len(body)}\r\n\r\n'.encode() + body)
    sys.stdout.buffer.flush()

def result(request, value):
    send({'jsonrpc':'2.0', 'id':request['id'], 'result':value})

def turn(request):
    global turn_number
    turn_number += 1
    params = request['params']
    identity = {'conversationId':params.get('conversationId', 'native-'+str(turn_number)), 'turnId':'turn-'+str(turn_number)}
    token = params['workDoneToken']
    terminal = False
    def progress(kind, **fields):
        send({'jsonrpc':'2.0', 'method':'$/progress', 'params':{'token':token, 'value':{'kind':kind, **identity, **fields}}})
    def end(cancelled=False):
        nonlocal terminal
        if terminal: return
        terminal = True
        fields = {'cancellationReason':'CancelledByUser'} if cancelled else {}
        if config.get('native_error'): fields['error'] = {'code':42, 'message':'fixture failure', 'responseIsIncomplete':True}
        progress('end', **fields)
        value = {**identity, 'modelInfo':params['modelInfo'], 'modelName':'Test Model'}
        if config.get('wrong_result'): value['turnId'] = 'wrong-turn'
        result(request, value)
    def await_callback(identifier):
        while True:
            message = receive()
            if message.get('method') == '$/cancelRequest':
                assert message['params'] == {'id':request['id']}
                end(True)
            elif message.get('id') == identifier and 'result' in message:
                return message['result']
            else: raise ValueError('unexpected callback wait message')
    progress('begin')
    mode = config.get('mode')
    if mode == 'many-reports':
        for index in range(5000): progress('report', reply=str(index)+';')
        end()
        return
    if mode == 'overflow':
        progress('report', reply='Ω'*200)
        message = receive()
        assert message['method'] == '$/cancelRequest' and message['params'] == {'id':request['id']}
        end(True)
        return
    queued = []
    for index, call in enumerate(config.get('calls', [])):
        callback = {**identity, 'roundId':index, 'toolCallId':str(index), 'name':call['name'], 'input':call['input']}
        if call.get('confirm', True):
            send({'jsonrpc':'2.0', 'id':'confirm-'+str(index), 'method':'conversation/invokeClientToolConfirmation', 'params':callback})
            decision = await_callback('confirm-'+str(index))
            if decision[0]['result'] == 'reject': continue
        if mode == 'late': end()
        send({'jsonrpc':'2.0', 'id':'invoke-'+str(index), 'method':'conversation/invokeClientTool', 'params':callback})
        if mode == 'queue':
            queued.append('invoke-'+str(index))
            continue
        if mode == 'duplicate':
            send({'jsonrpc':'2.0', 'id':'duplicate-'+str(index), 'method':'conversation/invokeClientTool', 'params':callback})
        if mode == 'early-end': end()
        if mode == 'child-loss':
            deadline = time.monotonic() + 5
            while not Path('started').exists() and time.monotonic() < deadline: time.sleep(.01)
            sys.exit(8)
        await_callback('invoke-'+str(index))
    for identifier in queued: await_callback(identifier)
    if not terminal: progress('report', reply='response '+str(turn_number))
    end()

while True:
    try: request = receive()
    except EOFError: break
    method = request.get('method')
    if method == 'initialize':
        assert os.environ['GITHUB_COPILOT_ACP_USE_CLI'] == '0'
        assert request['params']['initializationOptions']['copilotCapabilities']['mcpServerManagement'] is False
        result(request, {'serverInfo':{'name':'fixture', 'version':'1'}})
    elif method == 'workspace/didChangeConfiguration':
        assert request['params']['settings']['github']['copilot']['mcp'] == '{}'
    elif method == 'mcp/getTools':
        key = 'mcp_after_turn' if turn_number else ('mcp_after_registration' if registered else 'mcp')
        result(request, config.get(key, config.get('mcp', [])))
    elif method == 'copilot/models': result(request, config.get('models', [{'id':'test-model', 'modelName':'Test Model', 'scopes':['agent-panel']}]))
    elif method == 'conversation/registerTools':
        for item in request['params']['tools']:
            assert all(isinstance(prop.get('description'), str) and prop['description'] for prop in item['inputSchema']['properties'].values())
            assert all(isinstance(item['confirmationMessages'].get(key), str) and item['confirmationMessages'][key] for key in ('title', 'message'))
        tools = [{'name':item['name'], 'status':'enabled', 'type':'client', 'toolProvider':{'id':'copilot-editor'}} for item in request['params']['tools']]
        if config.get('bad_provider'): tools[0]['toolProvider']['id'] = 'other-provider'
        if config.get('duplicate_tool'): tools.append(tools[0])
        registered = True
        result(request, tools)
    elif method in ('conversation/create', 'conversation/turn'): turn(request)
    elif method == 'conversation/destroy': result(request, 'OK')
    elif method == 'shutdown': result(request, None)
    elif method == 'exit': break
    elif method != 'initialized': raise ValueError(method)
'''


def _client(tmp_path: Path, config: JsonObject, *, command_env: dict[str, str] | None = None) -> NativeClient:
    script = tmp_path / 'native_server.py'
    script.write_text(_SERVER, encoding='utf-8')
    configuration = tmp_path / 'scenario.json'
    configuration.write_text(json_text(config), encoding='utf-8')
    return NativeClient(
        sys.executable, cwd=tmp_path, arguments=('-u', str(script), str(configuration)),
        request_timeout=3, command_env=command_env,
    )


@asynccontextmanager
async def _running(tmp_path: Path, config: JsonObject) -> AsyncIterator[NativeClient]:
    client = _client(tmp_path, config)
    await client.start()
    client.allocate_session('logical', tmp_path, 'test-model', PermissionPolicy(1, 'allow_all'))
    try:
        yield client
    finally:
        await client.stop()


async def _turn(client: NativeClient, text: str = 'fresh text', *, event_bytes: int = 100_000,
                response_bytes: int = 100_000) -> NativeTerminal:
    return await client.run_turn('logical', text, 5, event_bytes, response_bytes)


def _requests(tmp_path: Path) -> list[JsonObject]:
    return [json_object(parse_json(line), 'fixture request') for line in (tmp_path / 'requests.jsonl').read_text(encoding='utf-8').splitlines()]


def _create(*, confirm: bool = True) -> JsonObject:
    return {'name':'workspace_create_files', 'input':{'files':[{'path':'new.txt', 'content':'initial'}]}, 'confirm':confirm}


def _command(code: str) -> JsonObject:
    # PowerShell's explicit invocation operator also accepts individually quoted arguments.
    command = ('& ' + ' '.join("'" + word.replace("'", "''") + "'" for word in (sys.executable, '-c', code))) if os.name == 'nt' else shlex.join((sys.executable, '-c', code))
    return {'name':'workspace_run_command', 'input':{'command':command, 'cwd':'.', 'timeout':4}}


@pytest.mark.asyncio
async def test_allocation_continuation_and_destroy_preserve_actual_identity(tmp_path: Path) -> None:
    async with _running(tmp_path, {}) as client:
        assert isinstance(client.binding('logical'), AllocatedBinding)
        assert not any(item.get('method') == 'conversation/create' for item in _requests(tmp_path))
        first = await _turn(client, 'instruction supplied once')
        second = await _turn(client, 'only new text')
        assert isinstance(first, NativeCompleted) and isinstance(second, NativeCompleted)
        assert second.observation.response_text == 'response 2'
        assert first.observation.binding.conversation_id == second.observation.binding.conversation_id
        assert first.observation.binding.turn_id != second.observation.binding.turn_id
        await client.retire_session('logical')
        with pytest.raises(NativeProtocolError, match='retired'):
            await _turn(client)
    requests = _requests(tmp_path)
    prompts = [item for item in requests if item.get('method') in ('conversation/create', 'conversation/turn')]
    assert json_object(prompts[0]['params'], 'create')['turns'] == [{'request':'instruction supplied once'}]
    followup = json_object(prompts[1]['params'], 'turn')
    assert followup['message'] == 'only new text' and 'turns' not in followup
    assert [item['params'] for item in requests if item.get('method') == 'conversation/destroy'] == [{'conversationId':'native-1'}]


@pytest.mark.asyncio
async def test_never_bound_retirement_does_not_infer_or_destroy(tmp_path: Path) -> None:
    async with _running(tmp_path, {}) as client:
        await client.retire_session('logical')
        assert isinstance(client.binding('logical'), AllocatedBinding)
    assert not any(str(item.get('method')).startswith('conversation/') and item['method'] != 'conversation/registerTools' for item in _requests(tmp_path))


@pytest.mark.asyncio
@pytest.mark.parametrize('config', [
    {'mcp':[{'name':'unexpected'}]}, {'mcp_after_registration':[{'name':'unexpected'}]},
    {'bad_provider':True}, {'duplicate_tool':True},
])
async def test_startup_rejects_mcp_or_ambiguous_tool_ownership(tmp_path: Path, config: JsonObject) -> None:
    client = _client(tmp_path, config)
    try:
        with pytest.raises(NativeProtocolError):
            await client.start()
        assert not client.is_alive
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_real_file_effects_and_command_env_remain_distinct_from_server_auth(tmp_path: Path) -> None:
    command = _command("import os; from pathlib import Path; Path('env.txt').write_text(os.environ.get('SERVER_ONLY_SECRET','absent')+'|'+os.environ['USER_SETTING'], encoding='utf-8')")
    config: JsonObject = {'calls':[_create(), {'name':'workspace_edit_files', 'input':{'files':[{'path':'new.txt','edits':[{'old_text':'initial','new_text':'edited'}]}]}}, command]}
    client = _client(tmp_path, config, command_env={**os.environ, 'USER_SETTING':'preserved'})
    await client.start({**os.environ, 'SERVER_ONLY_SECRET':'server credential'})
    client.allocate_session('logical', tmp_path, 'test-model', PermissionPolicy(1, 'allow_all'))
    try:
        result = await _turn(client)
        assert isinstance(result, NativeCompleted)
        assert len(result.observation.permissions) == len(result.observation.effects) == 3
        assert [(tool.name, tool.status, tool.scope) for tool in result.observation.tools] == [
            ('workspace_create_files', 'completed', 'bridge'),
            ('workspace_edit_files', 'completed', 'bridge'),
            ('workspace_run_command', 'completed', 'bridge'),
        ]
        assert (tmp_path / 'new.txt').read_text(encoding='utf-8') == 'edited'
        assert (tmp_path / 'env.txt').read_text(encoding='utf-8') == 'absent|preserved'
        effects = [json_object(parse_json(item.result_json), 'effect') for item in result.observation.effects]
        assert all(item['ok'] is True for item in effects)
        assert json_object(effects[2]['command'], 'command')['exit_code'] == 0
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_native_failure_retains_first_observed_binding(tmp_path: Path) -> None:
    async with _running(tmp_path, {'native_error':True}) as client:
        result = await _turn(client)
        assert isinstance(result, NativeFailed)
        assert isinstance(client.binding('logical'), ConversationBinding)
        assert result.observation.binding == client.binding('logical')
        assert result.observation.complete


@pytest.mark.asyncio
async def test_late_mcp_catalog_revokes_success_but_retains_settled_effect_evidence(tmp_path: Path) -> None:
    async with _running(tmp_path, {'calls':[_create()], 'mcp_after_turn':[{'name':'unexpected'}]}) as client:
        with pytest.raises(NativeUnsettledError, match='MCP catalog') as caught:
            await _turn(client)
        observation = caught.value.observation
        assert observation.binding == client.binding('logical')
        assert observation.response_text == 'response 1'
        assert observation.events[-1].kind == 'native.progress.end'
        assert len(observation.effects) == 1
        assert not observation.complete and not client.is_alive
        assert (tmp_path / 'new.txt').read_text(encoding='utf-8') == 'initial'


@pytest.mark.asyncio
async def test_only_concrete_agent_models_are_admitted(tmp_path: Path) -> None:
    config: JsonObject = {'models':[
        {'id':'auto','modelName':'Auto','scopes':['agent-panel']},
        {'id':'completion-only','modelName':'Completion','scopes':['completion']},
        {'id':'test-model','modelName':'Test Model','scopes':['agent-panel']},
    ]}
    async with _running(tmp_path, config) as client:
        assert [model.id for model in client.models] == ['test-model']
        for model in ('auto', 'completion-only'):
            with pytest.raises(NativeProtocolError):
                client.allocate_session(model, tmp_path, model, PermissionPolicy(1, 'allow_all'))


@pytest.mark.asyncio
@pytest.mark.parametrize('confirm', [False, True])
async def test_bad_known_tool_arguments_allow_correction_in_the_same_turn(tmp_path: Path, confirm: bool) -> None:
    invalid: JsonObject = {'name':'workspace_create_files', 'input':{'files':'invalid'}, 'confirm':confirm}
    async with _running(tmp_path, {'calls':[invalid, _create()]}) as client:
        result = await _turn(client)
        assert isinstance(result, NativeCompleted) and client.is_alive
        assert (tmp_path / 'new.txt').read_text(encoding='utf-8') == 'initial'
        assert len(result.observation.permissions) == 1
        assert result.observation.permissions[0].allowed
        if not confirm:
            rejected = json_object(parse_json(result.observation.effects[0].result_json), 'rejected effect')
            assert rejected['receipts'] == [] and rejected['ok'] is False
            assert json_object(rejected['error'], 'error')['code'] == 'invalid_arguments'


@pytest.mark.asyncio
async def test_concurrent_callbacks_serialize_effects_in_admission_order(tmp_path: Path) -> None:
    first = _command("import time; from pathlib import Path; time.sleep(.1); Path('ordered').write_text('first', encoding='utf-8')")
    second = _command("from pathlib import Path; p=Path('ordered'); p.write_text(p.read_text(encoding='utf-8')+' second', encoding='utf-8')")
    first['confirm'] = second['confirm'] = False
    async with _running(tmp_path, {'mode':'queue','calls':[first,second]}) as client:
        result = await _turn(client)
        assert isinstance(result, NativeCompleted)
        assert [effect.tool_call_id for effect in result.observation.effects] == ['0', '1']
        assert (tmp_path / 'ordered').read_text(encoding='utf-8') == 'first second'


async def _wait_for_file(path: Path) -> None:
    async with asyncio.timeout(3):
        while not path.exists():
            await asyncio.sleep(.01)


@pytest.mark.asyncio
async def test_cancel_settles_running_effect_and_prevents_queued_effect(tmp_path: Path) -> None:
    first = _command("import time; from pathlib import Path; Path('started').touch(); time.sleep(1); Path('late-first').touch()")
    second = _command("from pathlib import Path; Path('late-second').touch()")
    first['confirm'] = second['confirm'] = False
    async with _running(tmp_path, {'mode':'queue','calls':[first,second]}) as client:
        turn = asyncio.create_task(_turn(client))
        await _wait_for_file(tmp_path / 'started')
        await client.cancel_session('logical')
        result = await turn
        assert isinstance(result, NativeCancelled)
        assert len(result.observation.effects) == 2
        await asyncio.sleep(1.1)
        assert not (tmp_path / 'late-first').exists()
        assert not (tmp_path / 'late-second').exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('config', [{'wrong_result':True}, {'mode':'late','calls':[_create(confirm=False)]}, {'mode':'duplicate','calls':[_create(confirm=False)]}])
async def test_identity_or_callback_admission_fault_revokes_generation(tmp_path: Path, config: JsonObject) -> None:
    async with _running(tmp_path, config) as client:
        with pytest.raises(NativeUnsettledError) as caught:
            await _turn(client)
        assert isinstance(caught.value.observation.binding, ConversationBinding)
        assert not client.is_alive
        assert len(caught.value.observation.effects) <= 1
        if config.get('mode') == 'late':
            assert not (tmp_path / 'new.txt').exists()


@pytest.mark.asyncio
async def test_early_native_end_waits_for_admitted_effect_and_callback_reply(tmp_path: Path) -> None:
    command = _command("import time; from pathlib import Path; time.sleep(.15); Path('settled').write_text('done', encoding='utf-8')")
    async with _running(tmp_path, {'mode':'early-end', 'calls':[command]}) as client:
        result = await _turn(client)
        assert isinstance(result, NativeCompleted)
        assert (tmp_path / 'settled').read_text(encoding='utf-8') == 'done'
        assert len(result.observation.effects) == 1


@pytest.mark.asyncio
async def test_many_small_progress_events_preserve_complete_response(tmp_path: Path) -> None:
    """Chunking alone cannot terminate an otherwise bounded native response."""
    async with _running(tmp_path, {'mode':'many-reports'}) as client:
        result = await _turn(client, event_bytes=2_000_000)
        assert isinstance(result, NativeCompleted)
        assert result.observation.complete
        assert len(result.observation.events) == 5002
        assert result.observation.response_text == ''.join(str(index)+';' for index in range(5000))
        assert result.observation.events[0].kind == 'native.progress.begin'
        assert result.observation.events[-1].kind == 'native.progress.end'


@pytest.mark.asyncio
@pytest.mark.parametrize('limits', [{'event_bytes':150}, {'response_bytes':10}])
async def test_evidence_overflow_retains_bounded_prefix_and_requires_native_cancel(tmp_path: Path, limits: dict[str, int]) -> None:
    async with _running(tmp_path, {'mode':'overflow'}) as client:
        result = await _turn(client, event_bytes=limits.get('event_bytes', 100_000),
                             response_bytes=limits.get('response_bytes', 100_000))
        assert isinstance(result, NativeCancelled)
        assert result.reason == 'evidence_limit' and not result.observation.complete
        assert sum(len(event.payload_json.encode()) for event in result.observation.events) <= limits.get('event_bytes',100_000)
        assert len(result.observation.response_text.encode()) <= limits.get('response_bytes',100_000)


@pytest.mark.asyncio
async def test_child_loss_settles_running_command_before_exposing_uncertainty(tmp_path: Path) -> None:
    command = _command("import time; from pathlib import Path; Path('started').touch(); time.sleep(1); Path('must-not-appear').touch()")
    async with _running(tmp_path, {'mode':'child-loss', 'calls':[command]}) as client:
        with pytest.raises(NativeUnsettledError) as caught:
            await _turn(client)
        assert not caught.value.observation.complete
        assert caught.value.observation.events
        assert isinstance(caught.value.observation.binding, ConversationBinding)
        assert (tmp_path / 'started').exists()
        await asyncio.sleep(1.1)
        assert not (tmp_path / 'must-not-appear').exists()


@pytest.mark.asyncio
async def test_cancelled_stop_cannot_detach_running_effect_cleanup(tmp_path: Path) -> None:
    command = _command("import time; from pathlib import Path; Path('started').touch(); time.sleep(1); Path('late').touch()")
    async with _running(tmp_path, {'calls':[command]}) as client:
        turn = asyncio.create_task(_turn(client))
        await _wait_for_file(tmp_path / 'started')
        stopping = asyncio.create_task(client.stop())
        await asyncio.sleep(0)
        stopping.cancel()
        await asyncio.sleep(0)
        stopping.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stopping
        await client.stop()
        with pytest.raises(NativeUnsettledError):
            await turn
        assert not client.is_alive
        await asyncio.sleep(1.1)
        assert not (tmp_path / 'late').exists()
