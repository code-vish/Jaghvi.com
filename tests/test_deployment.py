"""Vercel-mode subprocess tests against a disposable HTTPS protocol emulator.

These tests do not run on Vercel or connect to Turso Cloud. OpenSSL is used only
to generate a temporary test CA/certificate; no test key is committed.
"""
from __future__ import annotations
import json
import os
import shutil
import sqlite3
import ssl
import subprocess
import sys
from pathlib import Path
import pytest
from protocol_server import ProtocolServer

ROOT=Path(__file__).resolve().parent.parent


@pytest.fixture
def deployment(tmp_path):
    if not shutil.which('openssl'):
        pytest.skip('OpenSSL is required for disposable HTTPS integration tests.')
    cert,key=tmp_path/'test-cert.pem',tmp_path/'test-key.pem'
    subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1',
        '-keyout',str(key),'-out',str(cert),'-subj','/CN=127.0.0.1',
        '-addext','subjectAltName=IP:127.0.0.1'],check=True,capture_output=True)
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert,key)
    database=tmp_path/'remote.sqlite3'
    with ProtocolServer(database,tls_context=context) as server:
        env={**os.environ,'PYTHONPATH':str(ROOT),'VERCEL':'1','APP_ENV':'production',
             'TURSO_DATABASE_URL':server.url,'TURSO_AUTH_TOKEN':server.token,
             'SSL_CERT_FILE':str(cert),'JAGHVI_DATA_DIR':str(tmp_path/'no-local-data')}
        yield server,env,tmp_path,database


def run_code(env,code,cwd,timeout=40):
    result=subprocess.run([sys.executable,'-c',code],env=env,cwd=cwd,capture_output=True,text=True,timeout=timeout)
    assert result.returncode==0,(result.stdout,result.stderr)
    return result


def test_build_checks_live_protocol_without_writing_schema(deployment):
    server,env,tmp,dbfile=deployment
    result=subprocess.run([sys.executable,str(ROOT/'scripts/verify_build.py')],env=env,cwd=tmp,
                          capture_output=True,text=True,timeout=30)
    assert result.returncode==0,(result.stdout,result.stderr)
    assert 'live Turso authentication, HTTP query' in result.stdout
    with sqlite3.connect(dbfile) as con:
        assert con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()==[]
    assert not (tmp/'no-local-data').exists()


def test_vercel_mode_https_owner_bootstrap_and_persistence(deployment):
    server,env,tmp,dbfile=deployment
    env.update(JAGHVI_OWNER_ID='test-master',JAGHVI_OWNER_EMAIL='owner@example.test',
               JAGHVI_OWNER_PASSWORD='Temporary-test-passphrase-794!')
    code=r'''
import re,json
from fastapi.testclient import TestClient
from app.main import app
from app.db import database
from app.release import BUILD_ID
with TestClient(app,base_url='https://jaghvi-com.vercel.app',follow_redirects=False) as client:
    health=client.get('/health'); assert health.status_code==200,health.text
    assert health.json()=={'status':'ok','build':BUILD_ID,'database':'reachable'}
    for path in ('/','/shop','/owner/login','/static/styles.css','/static/assets/jaghvi-logo.webp'):
        response=client.get(path); assert response.status_code==200,(path,response.status_code,response.text[:200])
    assert client.get('/favicon.ico').status_code==307
    token=re.search(r'name="csrf" value="([^"]+)"',client.get('/owner/login').text).group(1)
    result=client.post('/owner/login',data={'csrf':token,'username':'test-master','password':'Temporary-test-passphrase-794!'})
    assert result.status_code==303,result.text
    page=client.get('/owner/products/new'); assert page.status_code==200
    csrf=re.search(r'name="csrf" value="([^"]+)"',page.text).group(1)
    result=client.post('/owner/products/new',data={'csrf':csrf,'name':'Persistence test','price':'100',
                                                'stock':'5','status':'draft','category':'Rings'})
    assert result.status_code==303,result.text
    with database() as con:
        assert con.execute('SELECT COUNT(*) FROM owners').fetchone()[0]==1
        assert con.execute("SELECT name FROM products WHERE name='Persistence test'").fetchone()['name']=='Persistence test'
print('VERCEL-MODE HTTPS STARTUP, AUTH, DRAFT, STATIC ASSETS: PASS')
'''
    run_code(env,code,tmp)
    assert not (tmp/'no-local-data').exists()
    # A new process must use the same database, preserve the account and the draft.
    env['JAGHVI_OWNER_PASSWORD']='Must-not-overwrite-existing-passphrase!'
    run_code(env,r'''
from fastapi.testclient import TestClient
from app.main import app
from app.db import database
from app.security import password_valid
with TestClient(app,base_url='https://jaghvi-com.vercel.app') as client:
    assert client.get('/health').status_code==200
    with database() as con:
        owner=con.execute('SELECT * FROM owners').fetchone()
        assert password_valid('Temporary-test-passphrase-794!',owner['password_hash'])
        assert not password_valid('Must-not-overwrite-existing-passphrase!',owner['password_hash'])
        assert con.execute('SELECT COUNT(*) FROM products').fetchone()[0]==1
print('NEW PROCESS RETAINS OWNER AND PRODUCT: PASS')
''',tmp)


def test_missing_environment_is_503_without_process_crash(deployment):
    _,env,tmp,_=deployment
    env.pop('TURSO_DATABASE_URL');env.pop('TURSO_AUTH_TOKEN')
    run_code(env,r'''
from fastapi.testclient import TestClient
from app.main import app
with TestClient(app,base_url='https://jaghvi-com.vercel.app') as client:
    response=client.get('/health')
    assert response.status_code==503,response.text
    assert response.json()['code']=='DB_CONFIG_MISSING'
    response=client.get('/'); assert response.status_code==503
    assert 'DB_CONFIG_MISSING' in response.text
    assert client.get('/static/styles.css').status_code==200
''',tmp)
    assert not (tmp/'no-local-data').exists()


def test_bad_credentials_fail_build_and_return_safe_diagnostic(deployment):
    _,env,tmp,_=deployment
    secret='Do-not-echo-this-test-token'
    env['TURSO_AUTH_TOKEN']=secret
    result=subprocess.run([sys.executable,str(ROOT/'scripts/verify_build.py')],env=env,cwd=tmp,
                          capture_output=True,text=True,timeout=30)
    assert result.returncode==1
    assert 'DB_AUTH_REJECTED' in result.stdout
    assert secret not in result.stdout+result.stderr
    result=run_code(env,r'''
from fastapi.testclient import TestClient
from app.main import app
with TestClient(app,base_url='https://jaghvi-com.vercel.app') as client:
    response=client.get('/health')
    assert response.status_code==503,response.text
    assert response.json()['code']=='DB_AUTH_REJECTED'
    assert 'Do-not-echo-this-test-token' not in response.text
''',tmp)
    assert secret not in result.stdout+result.stderr


def test_absent_owner_and_blob_do_not_prevent_storefront_startup(deployment):
    _,env,tmp,_=deployment
    run_code(env,r'''
from fastapi.testclient import TestClient
from app.main import app
from app.db import database
with TestClient(app,base_url='https://jaghvi-com.vercel.app',follow_redirects=False) as client:
    assert client.get('/').status_code==200
    assert client.get('/owner/login').status_code==200
    assert client.get('/owner').status_code==303
    with database() as con:
        assert con.execute('SELECT COUNT(*) FROM owners').fetchone()[0]==0
''',tmp)


def test_schema_query_error_does_not_pass_readiness(deployment):
    server,env,tmp,_=deployment
    server.fail_sql='PRAGMA table_info(orders)'
    run_code(env,r'''
from fastapi.testclient import TestClient
from app.main import app
with TestClient(app,base_url='https://jaghvi-com.vercel.app') as client:
    assert client.get('/health').status_code==503
    assert client.get('/').status_code==503
''',tmp)
