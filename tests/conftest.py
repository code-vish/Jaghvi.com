"""All tests are isolated from production configuration and customer data."""
import os
import pytest

# Drop inherited live configuration before any application module is imported.
for name in ('VERCEL','TURSO_DATABASE_URL','TURSO_AUTH_TOKEN','BLOB_READ_WRITE_TOKEN',
             'JAGHVI_OWNER_ID','JAGHVI_OWNER_EMAIL','JAGHVI_OWNER_PASSWORD'):
    os.environ.pop(name, None)


@pytest.fixture(autouse=True)
def chosen_store_backend(request, monkeypatch):
    if os.environ.get('JAGHVI_TEST_BACKEND') != 'http' or request.module.__name__ != 'test_store':
        yield
        return
    from app import db
    from app.turso_http import TursoHTTPConnection
    from protocol_server import ProtocolServer
    # Use the same disposable DB path expected by the original store fixtures.
    with ProtocolServer(db.DB_PATH) as server:
        monkeypatch.setattr(db,'connect',lambda:TursoHTTPConnection(server.url,server.token,allow_http_loopback=True))
        yield
