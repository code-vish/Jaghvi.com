"""Turso SQL over HTTP. Protocol: https://docs.turso.tech/sdk/http/reference

Each logical connection owns its rotating baton and transaction; a shared HTTP
pool reuses TLS connections. Writes are never replayed automatically: a lost
response might conceal a committed write. Production data stays remote.
"""
from __future__ import annotations
import base64
import math
import threading
from collections.abc import Mapping
from urllib.parse import urlsplit, urlunsplit
import httpx
from .db_common import DatabaseError, IntegrityError, ResultCursor, split_sql_script


_client_lock = threading.Lock()
_shared_client = None


def shared_http_client():
    """Reuse TLS/TCP connections across short-lived logical database sessions."""
    global _shared_client
    with _client_lock:
        if _shared_client is None or _shared_client.is_closed:
            _shared_client = httpx.Client(timeout=httpx.Timeout(15.0, connect=5.0),
                limits=httpx.Limits(max_connections=30,max_keepalive_connections=10,keepalive_expiry=20),
                follow_redirects=False)
        return _shared_client


def close_http_client():
    global _shared_client
    with _client_lock:
        if _shared_client is not None:
            _shared_client.close()
            _shared_client = None


def encode_value(value):
    if value is None:
        return {'type': 'null'}
    if isinstance(value, bool):
        return {'type': 'integer', 'value': str(int(value))}
    if isinstance(value, int):
        if not -(2**63) <= value < 2**63:
            raise ValueError('Database integer is out of range.')
        return {'type': 'integer', 'value': str(value)}
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError('Database numbers must be finite.')
        return {'type': 'float', 'value': value}
    if isinstance(value, str):
        return {'type': 'text', 'value': value}
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {'type': 'blob', 'base64': base64.b64encode(bytes(value)).decode('ascii')}
    raise TypeError('Unsupported database parameter type.')


def decode_value(item):
    try:
        kind = item['type']
        if kind == 'null':
            return None
        if kind == 'integer':
            return int(item['value'])
        if kind == 'float':
            return float(item['value'])
        if kind == 'text':
            return item['value']
        if kind == 'blob':
            value = item['base64']
            return base64.b64decode(value + '=' * (-len(value) % 4), validate=True)
    except (KeyError, TypeError, ValueError) as exc:
        raise DatabaseError('Invalid database result value.', code='DB_PROTOCOL') from exc
    raise DatabaseError('Unsupported database result type.', code='DB_PROTOCOL')


def normalize_url(value: str, *, allow_http_loopback=False) -> str:
    try:
        p = urlsplit(value.strip())
        scheme = 'https' if p.scheme in {'libsql', 'turso'} else p.scheme
        local = p.hostname in {'127.0.0.1', 'localhost', '::1'}
        if scheme != 'https' and not (allow_http_loopback and scheme == 'http' and local):
            raise ValueError
        if not p.hostname or p.username or p.password or p.query or p.fragment:
            raise ValueError
        _ = p.port
        if p.path.rstrip('/') not in {'', '/v2/pipeline'}:
            raise ValueError
        return urlunsplit((scheme, p.netloc, '/v2/pipeline', '', ''))
    except (ValueError, AttributeError):
        raise DatabaseError('TURSO_DATABASE_URL must be the database URL with no quotes or extra parameters.', code='DB_URL_INVALID') from None


def sql_error(error):
    # Provider messages may contain SQL arguments. Never print them to a page/log.
    code = str(error.get('code', '')).upper()
    text = str(error.get('message', '')).lower()
    if 'CONSTRAINT' in code or any(x in text for x in ('constraint failed', 'unique constraint', 'foreign key constraint')):
        raise IntegrityError()
    if 'AUTH' in code or 'JWT' in code:
        raise DatabaseError('The database rejected its credentials.', code='DB_AUTH_REJECTED')
    if any(x in code for x in ('EXPIRED', 'STREAM_NOT_FOUND', 'STREAM_CLOSED', 'BATON')):
        raise DatabaseError('The database connection expired. Retry the operation.', code='DB_CONNECTION_EXPIRED')
    if 'BUSY' in code or 'LOCKED' in code:
        raise DatabaseError('The database is busy. Retry the operation.', code='DB_BUSY')
    raise DatabaseError('A database statement failed. Parameters have not been logged.', code='DB_QUERY_FAILED')


class TursoHTTPConnection:
    remote = True

    def __init__(self, url: str, auth_token: str, *, client=None, allow_http_loopback=False):
        if not url or not auth_token:
            raise DatabaseError('Connect Turso to this project: TURSO_DATABASE_URL and TURSO_AUTH_TOKEN are required.', code='DB_CONFIG_MISSING')
        if '\n' in auth_token or '\r' in auth_token:
            raise DatabaseError('The database token has invalid whitespace.', code='DB_TOKEN_INVALID')
        self._endpoint = normalize_url(url, allow_http_loopback=allow_http_loopback)
        self._original_host = urlsplit(self._endpoint).hostname
        self._allow_http_loopback = allow_http_loopback
        self._token = auth_token.strip()
        self._client = client or shared_http_client()
        self._owns_client = False  # logical DB close must not close the shared HTTP pool
        self._baton = None
        self._started = self._closed = self._broken = self._transaction = False

    def _route_to(self, base_url):
        if not base_url:
            return
        candidate = normalize_url(base_url, allow_http_loopback=self._allow_http_loopback)
        old, new = urlsplit(self._endpoint), urlsplit(candidate)
        same_origin = (new.scheme, new.hostname, new.port) == (old.scheme, old.hostname, old.port)
        turso_route = (self._original_host.endswith('.turso.io') and new.hostname.endswith('.turso.io')
                       and new.scheme == 'https' and new.port in (None, 443))
        if not same_origin and not turso_route:
            self._broken = True
            raise DatabaseError('Unexpected database connection address.', code='DB_PROTOCOL')
        self._endpoint = candidate

    def _send(self, operations):
        if self._closed or self._broken:
            raise DatabaseError('The database connection is closed.', code='DB_CONNECTION_CLOSED')
        payload = {'requests': operations}
        if self._baton is not None:
            payload['baton'] = self._baton
        try:
            response = self._client.post(self._endpoint, json=payload, headers={
                'Authorization': 'Bearer ' + self._token, 'Content-Type': 'application/json'})
        except httpx.HTTPError:
            self._broken = True
            raise DatabaseError('The database could not be reached. Check the database service and network.', code='DB_NETWORK') from None
        if response.status_code != 200:
            self._broken = True
            if response.status_code in (401, 403):
                raise DatabaseError('Turso rejected the credentials. Check the connected integration.', code='DB_AUTH_REJECTED')
            if response.status_code in (402, 429):
                raise DatabaseError('The database is enforcing a usage or rate limit.', code='DB_LIMIT')
            if response.status_code == 404:
                raise DatabaseError('The database endpoint was not found. Check TURSO_DATABASE_URL.', code='DB_URL_INVALID')
            raise DatabaseError('The database service returned an unsuccessful response.', code='DB_HTTP_ERROR')
        try:
            if len(response.content) > 32 * 1024 * 1024:
                raise ValueError
            body = response.json()
            if not isinstance(body, dict) or not isinstance(body.get('results'), list):
                raise ValueError
            if len(body['results']) != len(operations):
                raise ValueError
            baton = body.get('baton')
            if baton is not None and not isinstance(baton, str):
                raise ValueError
        except (ValueError, TypeError):
            self._broken = True
            raise DatabaseError('The database returned an invalid response.', code='DB_PROTOCOL') from None
        # The baton rotates even when a statement returns an error.
        self._baton, self._started = baton, True
        self._route_to(body.get('base_url'))
        results = []
        for item in body['results']:
            if not isinstance(item, dict):
                raise DatabaseError('Invalid database operation result.', code='DB_PROTOCOL')
            if item.get('type') == 'error':
                sql_error(item.get('error', {}))
            if item.get('type') != 'ok' or not isinstance(item.get('response'), dict):
                raise DatabaseError('Invalid database operation result.', code='DB_PROTOCOL')
            results.append(item['response'])
        if operations and operations[-1].get('type') != 'close' and self._baton is None:
            self._broken = True
            raise DatabaseError('The database did not return an open connection.', code='DB_CONNECTION_EXPIRED')
        return results

    @staticmethod
    def _statement(sql, params):
        if not isinstance(sql, str) or not sql.strip():
            raise ValueError('A SQL statement is required.')
        stmt = {'sql': sql, 'want_rows': True}
        if isinstance(params, Mapping):
            stmt['named_args'] = [{'name': str(k), 'value': encode_value(v)} for k, v in params.items()]
        else:
            stmt['args'] = [encode_value(v) for v in params]
        return {'type': 'execute', 'stmt': stmt}

    @staticmethod
    def _result(response):
        try:
            if response['type'] != 'execute':
                raise ValueError
            data = response['result']
            columns = [c['name'] for c in data['cols']]
            values = [[decode_value(v) for v in r] for r in data['rows']]
            rowid = data.get('last_insert_rowid')
            return ResultCursor(columns, values, rowcount=int(data.get('affected_row_count', 0)),
                                lastrowid=int(rowid) if rowid is not None else None)
        except (KeyError, TypeError, ValueError) as exc:
            raise DatabaseError('The database result could not be decoded.', code='DB_PROTOCOL') from exc

    def _execute_operations(self, operations):
        prefix = [] if self._started else [self._statement('PRAGMA foreign_keys=ON', ())]
        return [self._result(r) for r in self._send(prefix + operations)[len(prefix):]]

    def execute(self, sql, params=()):
        result = self._execute_operations([self._statement(sql, params)])[0]
        command = sql.strip().split()[0].rstrip(';').upper()
        if command == 'BEGIN':
            self._transaction = True
        elif command in {'COMMIT', 'END', 'ROLLBACK'}:
            self._transaction = False
        return result

    def executemany(self, sql, seq):
        operations = [self._statement(sql, p) for p in seq]
        if not operations:
            return ResultCursor(rowcount=0)
        results = self._execute_operations(operations)
        return ResultCursor(rowcount=sum(max(0, r.rowcount) for r in results), lastrowid=results[-1].lastrowid)

    def executescript(self, script):
        operations = [self._statement(s, ()) for s in split_sql_script(script)]
        if operations:
            self._execute_operations(operations)

    def commit(self):
        # Read-only/autocommit connections do not accept an extra COMMIT.
        if self._transaction:
            self.execute('COMMIT')

    def rollback(self):
        if self._transaction and not self._broken and not self._closed:
            try:
                self.execute('ROLLBACK')
            except DatabaseError:
                pass

    def close(self):
        if self._closed:
            return
        try:
            if self._baton and not self._broken:
                try:
                    self._send([{'type': 'close'}])
                except DatabaseError:
                    pass
        finally:
            self._closed, self._baton = True, None
            if self._owns_client:
                self._client.close()
