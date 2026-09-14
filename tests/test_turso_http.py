from __future__ import annotations
import json
import math
import pytest
import httpx
from app import db
from app.db_common import DatabaseError, IntegrityError, ResultCursor, Row, split_sql_script
from app.turso_http import TursoHTTPConnection, normalize_url, encode_value, decode_value
from protocol_server import ProtocolServer


@pytest.fixture
def remote(tmp_path):
    with ProtocolServer(tmp_path/'protocol.sqlite3') as server:
        yield server


def connection(server):
    return TursoHTTPConnection(server.url,server.token,allow_http_loopback=True)


@pytest.mark.parametrize('value',[None,False,True,0,-17,2**62,0.25,'','Jaghvi · चाँदी',b'\x00\xff'])
def test_value_roundtrip(value):
    assert decode_value(encode_value(value)) == value


@pytest.mark.parametrize('url',['libsql://jaghvi-test.turso.io','https://jaghvi-test.turso.io/',
                                 'turso://jaghvi-test.turso.io','https://jaghvi-test.turso.io/v2/pipeline'])
def test_https_normalization(url):
    assert normalize_url(url)=='https://jaghvi-test.turso.io/v2/pipeline'


@pytest.mark.parametrize('url',['http://example.com','file:///tmp/a.db','', 'https://user:password@example.com',
                                 'https://example.com/?token=x','https://example.com/#token','"libsql://x.turso.io"'])
def test_invalid_configuration_is_not_echoed(url):
    with pytest.raises(DatabaseError) as err: normalize_url(url)
    assert err.value.code=='DB_URL_INVALID'
    assert url not in str(err.value) or url==''


def test_integer_float_validation():
    for value in (2**63,-2**63-1,float('nan'),float('inf')):
        with pytest.raises(ValueError):encode_value(value)
    with pytest.raises(TypeError):encode_value(object())


def test_row_and_partial_cursor_consumption():
    cursor=ResultCursor(['name','value'],[['a',None],['b',0],['c','']])
    assert dict(cursor.fetchone())=={'name':'a','value':None}
    assert next(iter(cursor))['name']=='b'
    assert [dict(r) for r in cursor.fetchall()]==[{'name':'c','value':''}]
    assert cursor.fetchone() is None
    assert list(cursor)==[]


def test_actual_http_query_decoding_and_baton_rotation(remote):
    con=connection(remote)
    try:
        row=con.execute('SELECT ? AS name, ? AS huge, ? AS empty, ? AS fraction, ? AS bytes',
                        ('Jaghvi',2**62,None,0.25,b'\x00\xff')).fetchone()
        assert dict(row)=={'name':'Jaghvi','huge':2**62,'empty':None,'fraction':0.25,'bytes':b'\x00\xff'}
        old=con._baton
        assert con.execute('SELECT 2 AS n').fetchone()['n']==2
        assert con._baton != old
        con.commit()
        assert not any(q=='COMMIT' for q in remote.queries)
    finally:con.close()
    assert not remote.connections


def test_named_parameters(remote):
    con=connection(remote)
    try:assert con.execute('SELECT :brand AS name',{'brand':'Jaghvi'}).fetchone()['name']=='Jaghvi'
    finally:con.close()


def test_pragma_initialization_idempotency_and_existing_settings(remote,monkeypatch):
    monkeypatch.setattr(db,'connect',lambda:connection(remote))
    db.initialize()
    with db.database(True) as con:
        con.execute('UPDATE settings SET value=? WHERE key=?',(json.dumps('My own title'),'hero_title'))
        con.execute('DELETE FROM collections')
    db.initialize()
    with db.database() as con:
        assert db.site_settings(con)['hero_title']=='My own title'
        assert con.execute('SELECT COUNT(*) FROM collections').fetchone()[0]==0
        assert 'checkout_key' in {row['name'] for row in con.execute('PRAGMA table_info(orders)')}
    assert not remote.connections


def test_failed_transaction_rollback_and_foreign_keys(remote,monkeypatch):
    monkeypatch.setattr(db,'connect',lambda:connection(remote))
    db.initialize()
    with pytest.raises(IntegrityError):
        with db.database(True) as con:
            con.execute("INSERT INTO settings VALUES('rollback_test','1')")
            con.execute("INSERT INTO settings VALUES('rollback_test','2')")
    with db.database() as con:
        assert con.execute("SELECT 1 FROM settings WHERE key='rollback_test'").fetchone() is None
    with pytest.raises(IntegrityError):
        with db.database(True) as con:
            con.execute("INSERT INTO images(product_id,path) VALUES (99999,'/test.webp')")
    assert not remote.connections


def test_successful_transaction_persists_across_connections(remote,monkeypatch):
    monkeypatch.setattr(db,'connect',lambda:connection(remote))
    db.initialize()
    with db.database(True) as con:
        row=con.execute("INSERT INTO collections(name,slug) VALUES(?,?) RETURNING id",('New','new')).fetchone()
        created_id=row[0]
    with db.database() as con:
        assert con.execute('SELECT name FROM collections WHERE id=?',(created_id,)).fetchone()['name']=='New'


def test_scripts_with_semicolons_and_multi_execute(remote):
    con=connection(remote)
    try:
        con.execute('BEGIN IMMEDIATE')
        con.executescript("CREATE TABLE test(id INTEGER PRIMARY KEY, name TEXT); INSERT INTO test(name) VALUES('one;two');")
        result=con.executemany('INSERT INTO test(name) VALUES(?)',[('three',),('four',)])
        assert result.rowcount==2
        con.commit()
        assert [r['name'] for r in con.execute('SELECT name FROM test')]==['one;two','three','four']
    finally:con.close()


def test_migration_from_original_orders_table(remote,monkeypatch):
    monkeypatch.setattr(db,'connect',lambda:connection(remote))
    with db.database(True) as con:
        con.executescript(db.SCHEMA.replace('checkout_key TEXT,',''))
    db.initialize()
    with db.database() as con:
        assert 'checkout_key' in {r['name'] for r in con.execute('PRAGMA table_info(orders)')}
        assert con.execute('SELECT version FROM schema_version').fetchone()[0]==2


def test_auth_errors_are_actionable_and_no_token_is_exposed(remote):
    con=TursoHTTPConnection(remote.url,'secret-not-to-be-logged',allow_http_loopback=True)
    try:
        with pytest.raises(DatabaseError) as err:con.execute('SELECT 1')
        assert err.value.code=='DB_AUTH_REJECTED'
        assert 'secret-not-to-be-logged' not in str(err.value)
    finally:con.close()


def test_missing_environment_does_not_fall_back_to_filesystem(monkeypatch,tmp_path):
    monkeypatch.setattr(db,'IS_VERCEL',True)
    monkeypatch.setattr(db,'TURSO_URL','')
    monkeypatch.setattr(db,'TURSO_TOKEN','')
    monkeypatch.setattr(db,'DATA',tmp_path/'forbidden')
    with pytest.raises(DatabaseError) as err:db.connect()
    assert err.value.code=='DB_CONFIG_MISSING'
    assert not (tmp_path/'forbidden').exists()


def test_lost_response_is_never_retried():
    requests=[]
    def fail(request):
        requests.append(request)
        raise httpx.ReadTimeout('test only')
    client=httpx.Client(transport=httpx.MockTransport(fail))
    con=TursoHTTPConnection('https://sample.turso.io','test-only',client=client)
    try:
        with pytest.raises(DatabaseError) as err:con.execute('INSERT INTO test VALUES (?)',(1,))
        assert err.value.code=='DB_NETWORK'
        assert len(requests)==1
        with pytest.raises(DatabaseError):con.execute('INSERT INTO test VALUES (?)',(1,))
        assert len(requests)==1
    finally:con.close();client.close()


def test_unsafe_server_routing_is_rejected():
    def respond(request):
        body=json.loads(request.content)
        return httpx.Response(200,json={'baton':'test-baton','base_url':'https://attacker.invalid',
            'results':[{'type':'ok','response':{'type':'execute','result':{'cols':[],'rows':[],
                'affected_row_count':0,'last_insert_rowid':None}}} for _ in body['requests']]})
    client=httpx.Client(transport=httpx.MockTransport(respond))
    con=TursoHTTPConnection('https://sample.turso.io','test-only',client=client)
    try:
        with pytest.raises(DatabaseError) as err:con.execute('SELECT 1')
        assert err.value.code=='DB_PROTOCOL'
    finally:con.close();client.close()


def test_bulk_settings_are_one_http_roundtrip(remote):
    con=connection(remote)
    try:
        con.execute('CREATE TABLE bulk_test(key TEXT PRIMARY KEY,value TEXT)')
        con.execute('BEGIN IMMEDIATE')
        before=len(remote.requests)
        result=con.executemany('INSERT INTO bulk_test VALUES(?,?)',[(str(i),'value') for i in range(60)])
        assert result.rowcount==60
        assert len(remote.requests)==before+1
        con.commit()
        assert con.execute('SELECT COUNT(*) FROM bulk_test').fetchone()[0]==60
    finally:con.close()
