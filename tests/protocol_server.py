"""Disposable SQL-over-HTTP protocol emulator, not a Turso Cloud service.

Independent JSON encoder/decoder; real SQLite engine behind a loopback HTTP
server. Rotates batons on every request, preserves transactions, rejects stale
batons and enforces foreign keys. NEVER use this as a production database.
"""
from __future__ import annotations
import base64
import json
import secrets
import sqlite3
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def from_json(value):
    kind = value['type']
    if kind == 'null': return None
    if kind == 'integer':
        assert isinstance(value['value'], str)
        return int(value['value'])
    if kind == 'float':
        assert isinstance(value['value'], (float, int))
        return float(value['value'])
    if kind == 'text': return value['value']
    if kind == 'blob': return base64.b64decode(value['base64'])
    raise ValueError('Unknown type')


def to_json(value):
    if value is None: return {'type':'null'}
    if isinstance(value, int): return {'type':'integer','value':str(value)}
    if isinstance(value, float): return {'type':'float','value':value}
    if isinstance(value, str): return {'type':'text','value':value}
    if isinstance(value, bytes): return {'type':'blob','base64':base64.b64encode(value).decode()}
    raise TypeError('Unknown type')


class ProtocolServer:
    def __init__(self, path, *, tls_context=None):
        self.path = str(path)
        self.token = 'protocol-test-only-not-a-real-credential'
        self.connections = {}
        self.lock = threading.Lock()
        self.requests = []
        self.queries = []
        self.denied = False
        self.fail_sql = None
        self.clock = 0
        parent = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            def setup(self):
                super().setup()
                self.connection.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1)
            def log_message(self, *args): pass
            def do_POST(self):
                size = int(self.headers.get('Content-Length',0))
                raw = self.rfile.read(size)
                if parent.denied or self.headers.get('Authorization') != 'Bearer '+parent.token:
                    return self.reply(401, {'error':'test authentication denied'})
                if self.path != '/v2/pipeline':
                    return self.reply(404, {'error':'unknown path'})
                try:
                    payload = json.loads(raw)
                    status, body = parent.handle(payload)
                    self.reply(status,body)
                except Exception as exc:
                    self.reply(500, {'error':type(exc).__name__})
            def reply(self, status, body):
                raw=json.dumps(body).encode()
                self.send_response(status)
                self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.server.daemon_threads=True
        scheme='http'
        if tls_context is not None:
            self.server.socket=tls_context.wrap_socket(self.server.socket,server_side=True)
            scheme='https'
        self.url=f'{scheme}://127.0.0.1:{self.server.server_address[1]}'
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)

    def handle(self,payload):
        with self.lock:
            self.requests.append(payload)
            baton=payload.get('baton')
            if baton:
                con=self.connections.pop(baton,None)
                if con is None:
                    return 200,{'baton':None,'base_url':None,'results':[
                        {'type':'error','error':{'code':'STREAM_EXPIRED','message':'Expired test connection'}}
                        for _ in payload['requests']]}
            else:
                con=sqlite3.connect(self.path, isolation_level=None, check_same_thread=False, timeout=2)
        results=[]
        closed=False
        for op in payload['requests']:
            if op['type']=='close':
                con.close(); closed=True
                results.append({'type':'ok','response':{'type':'close'}})
                continue
            statement=op['stmt']; sql=statement['sql']
            self.queries.append(sql)
            try:
                if self.fail_sql and self.fail_sql in sql:
                    raise sqlite3.OperationalError('injected query failure')
                if 'named_args' in statement:
                    params={x['name'].lstrip(':@$'):from_json(x['value']) for x in statement['named_args']}
                else:
                    params=[from_json(x) for x in statement.get('args',[])]
                cursor=con.execute(sql,params)
                values=cursor.fetchall()
                result={'cols':[{'name':d[0],'decltype':None} for d in (cursor.description or [])],
                        'rows':[[to_json(v) for v in row] for row in values],
                        'affected_row_count':max(cursor.rowcount,0),
                        'last_insert_rowid':str(cursor.lastrowid) if cursor.lastrowid is not None else None}
                results.append({'type':'ok','response':{'type':'execute','result':result}})
            except sqlite3.Error as exc:
                results.append({'type':'error','error':{'code':getattr(exc,'sqlite_errorname','SQLITE_ERROR'),
                                                      'message':str(exc)}})
        baton=None
        if not closed:
            baton=secrets.token_hex(12)
            with self.lock: self.connections[baton]=con
        return 200,{'baton':baton,'base_url':self.url,'results':results}

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self,*args):
        from app.turso_http import close_http_client
        close_http_client()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        for con in self.connections.values():
            con.close()
        self.connections.clear()
