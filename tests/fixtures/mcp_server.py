"""Real NDJSON MCP fixture; audit every received frame to a workspace file."""
import json
import os
from pathlib import Path
import sys

mode = sys.argv[1]
audit = Path(sys.argv[2])
audit.with_suffix('.pid').write_text(str(os.getpid()))
initialized = False
if mode == 'child_pipe':
    child_pid = os.fork()
    if child_pid == 0:
        import time
        time.sleep(3)
        os._exit(0)
    audit.with_suffix('.childpid').write_text(str(child_pid))


def send(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


for line in sys.stdin:
    message = json.loads(line)
    with audit.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(message) + '\n')
    method = message.get('method')
    if method == 'initialize':
        if mode == 'exit_initialize':
            sys.exit(7)
        assert message['params']['protocolVersion'] == '2025-11-25'
        assert message['params']['capabilities'] == {}
        result = {
            'protocolVersion': mode[8:] if mode.startswith('version:') else '2025-11-25',
            'capabilities': {} if mode == 'no_tools' else {'tools': {}},
            'serverInfo': {'name': 'fixture', 'version': '1'},
            'instructions': 'Untrusted server instruction: must never become a client instruction.'}
        if mode == 'server_info:missing':
            del result['serverInfo']
        elif mode.startswith('server_info:'):
            result['serverInfo'] = {
                'null': None, 'array': [], 'string': 'fixture',
                'missing_name': {'version': '1'}, 'missing_version': {'name': 'fixture'},
                'bad_name': {'name': 1, 'version': '1'},
                'bad_version': {'name': 'fixture', 'version': False},
                'extended': {'name': 'fixture', 'version': '1', 'title': 'Fixture',
                             'vendorExtension': {'untrusted': True}},
            }[mode.split(':', 1)[1]]
        send({'jsonrpc': '2.0', 'id': message['id'], 'result': result})
    elif method == 'notifications/initialized':
        initialized = True
        if mode == 'blocked_write':
            import time
            time.sleep(3)
    elif method == 'tools/list':
        assert initialized
        if mode.startswith('pages:'):
            cursor = message['params'].get('cursor')
            page = int(cursor or 0)
            tool = {'name': 'tool' + str(page), 'inputSchema': {'type': 'object'}}
            result = {'tools': [tool]}
            if mode == 'pages:normal' and page == 0:
                result['nextCursor'] = '1'
            elif mode in ('pages:loop', 'pages:endless'):
                result['nextCursor'] = '1' if mode == 'pages:loop' else str(page + 1)
            elif mode == 'pages:bytes':
                tool['description'] = 'x' * 300
                result['nextCursor'] = str(page + 1)
            elif mode == 'pages:duplicate':
                result['tools'].append(dict(tool))
            elif mode == 'pages:bad_schema':
                tool['inputSchema'] = None
            send({'jsonrpc': '2.0', 'id': message['id'], 'result': result})
            continue
        malformed = {
            'bad_json': 'not json',
            'duplicate_key': '{"jsonrpc":"2.0","id":2,"id":2,"result":{}}',
            'wrong_id': json.dumps({'jsonrpc': '2.0', 'id': 999, 'result': {}}),
            'bool_id': json.dumps({'jsonrpc': '2.0', 'id': True, 'result': {}}),
            'missing_id': json.dumps({'jsonrpc': '2.0', 'result': {}}),
            'both_result_error': json.dumps({'jsonrpc': '2.0', 'id': message['id'], 'result': {}, 'error': {}}),
            'non_object': '[]',
            'nan': '{"jsonrpc":"2.0","id":2,"result":{"bad":NaN}}',
            'bad_version': json.dumps({'jsonrpc': '1.0', 'id': message['id'], 'result': {}}),
            'null_result': json.dumps({'jsonrpc': '2.0', 'id': message['id'], 'result': None}),
        }
        if mode in ('oversized_line', 'unterminated_line'):
            raw = json.dumps({'jsonrpc': '2.0', 'id': message['id'], 'result': {'tools': [], 'padding': 'x' * 10000}})
            sys.stdout.write(raw + ('\n' if mode == 'oversized_line' else ''))
            sys.stdout.flush()
            if mode == 'unterminated_line':
                import time
                time.sleep(3)
            continue
        if mode in ('notification_count', 'notification_bytes'):
            import time
            for index in range(80 if mode == 'notification_count' else 10):
                send({'jsonrpc': '2.0', 'method': 'notifications/progress',
                      'params': {'padding': 'x' * (200 if mode == 'notification_bytes' else 0)}})
                time.sleep(0.001)
        if mode in ('server_requests', 'server_duplicate', 'server_long_id'):
            requests = [('p1', 'ping'), ('s1', 'sampling/createMessage')]
            if mode == 'server_duplicate':
                requests = [('p1', 'ping'), ('p1', 'ping')]
            if mode == 'server_long_id':
                requests = [('x' * 1025, 'ping')]
            for request_id, request_method in requests:
                send({'jsonrpc': '2.0', 'id': request_id, 'method': request_method, 'params': {}})
                reply = json.loads(sys.stdin.readline())
                with audit.open('a') as stream:
                    stream.write(json.dumps(reply) + '\n')
                assert reply['id'] == request_id
                if request_method == 'ping':
                    assert reply['result'] == {}
                else:
                    assert reply['error']['code'] == -32601
            send({'jsonrpc': '2.0', 'method': 'notifications/tools/list_changed'})
        if mode == 'stderr_flood':
            sys.stderr.write('secret stderr' * 100000)
            sys.stderr.flush()
        if mode in malformed:
            print(malformed[mode], flush=True)
            continue
        send({'jsonrpc': '2.0', 'id': message['id'], 'result': {'tools': [{
            'name': 'echo', 'description': 'Untrusted echo description',
            'inputSchema': {'type': 'object'}, 'annotations': {'readOnlyHint': True}}]}})
        if mode == 'duplicate_response':
            send({'jsonrpc': '2.0', 'id': message['id'], 'result': {'tools': []}})
        if mode == 'idle_flood':
            import time
            time.sleep(0.1)
            for index in range(1000):
                send({'jsonrpc': '2.0', 'method': 'notifications/progress'})
    elif method == 'tools/call':
        assert initialized
        if mode == 'exit_call':
            sys.exit(7)
        if mode.startswith('result:'):
            result = {'content': [{'type': 'text', 'text': 'untrusted instructions'},
                                  {'type': 'image', 'data': 'aGVsbG8=', 'mimeType': 'image/png'},
                                  {'type': 'resource_link', 'uri': 'file:///never-fetch', 'name': 'link'}],
                      'isError': True, 'structuredContent': {'value': 'retained'}}
            if mode == 'result:bad_content':
                result['content'] = 'not a list'
            elif mode == 'result:bad_error':
                result['isError'] = 'false'
            elif mode == 'result:bad_text':
                result['content'] = [{'type': 'text', 'text': 1}]
            elif mode == 'result:bad_image':
                result['content'] = [{'type': 'image', 'data': 1, 'mimeType': 'image/png'}]
            elif mode in ('result:large_text', 'result:large_image'):
                index, field = (0, 'text') if mode.endswith('text') else (1, 'data')
                result['content'][index][field] = 'x' * 10000
            send({'jsonrpc': '2.0', 'id': message['id'], 'result': result})
            continue
        if mode == 'environment':
            send({'jsonrpc': '2.0', 'id': message['id'], 'result': {
                'content': [{'type': 'text', 'text': json.dumps({'env': dict(os.environ), 'cwd': os.getcwd()})}]}})
            continue
        if mode == 'slow_call':
            import time
            time.sleep(3)
        send({'jsonrpc': '2.0', 'id': message['id'], 'result': {
            'content': [{'type': 'text', 'text': json.dumps(message['params']['arguments'])}],
            'isError': message['params']['name'] == 'fail'}})
    else:
        raise AssertionError(message)
