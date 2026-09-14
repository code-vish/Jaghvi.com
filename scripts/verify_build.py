"""Deployment preflight. Does not create tables or modify customer/store data."""
from __future__ import annotations
import importlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main():
    from app.release import BUILD_ID
    from app.db import IS_VERCEL, TURSO_URL, TURSO_TOKEN, DatabaseError, connect
    print(f'Jaghvi deployment preflight: {BUILD_ID}', flush=True)
    required = [
        'app/templates/base.html', 'app/templates/home.html', 'app/templates/owner/login.html',
        'app/templates/owner/dashboard.html', 'app/static/styles.css', 'app/static/owner.css',
        'app/static/store.js', 'app/static/owner.js', 'app/static/icons.svg',
        'app/static/assets/jaghvi-logo.webp', 'app/static/assets/favicon.png',
    ]
    absent = [p for p in required if not (ROOT / p).is_file()]
    if absent:
        print('FAILED: missing project files: ' + ', '.join(absent), flush=True)
        return 1
    # Import check also verifies FastAPI, template, password and image dependencies.
    importlib.import_module('app.main')
    print('PASS: required assets, templates and application imports.', flush=True)
    if not (IS_VERCEL or TURSO_URL or TURSO_TOKEN):
        print('LOCAL ONLY: no Turso credentials configured; remote check not run.', flush=True)
        return 0
    con = None
    try:
        con = connect()
        result = con.execute("SELECT 'jaghvi' AS label, 42 AS answer, NULL AS empty_value, 0.25 AS fraction").fetchone()
        assert result is not None and dict(result) == {'label':'jaghvi','answer':42,'empty_value':None,'fraction':0.25}
        columns = list(con.execute('PRAGMA table_info(orders)'))
        assert all('name' in row.keys() for row in columns)
        # No schema writes or account creation in the deployment build.
        print('PASS: live Turso authentication, HTTP query, row decoding and table inspection.', flush=True)
    except DatabaseError as exc:
        print(f'FAILED [{exc.code}]: {exc}', flush=True)
        print('No database was reset. Check this project\'s connected integration/environment, then redeploy.', flush=True)
        return 1
    except Exception as exc:
        print(f'FAILED [PREFLIGHT_ERROR]: {type(exc).__name__}. No secrets were printed.', flush=True)
        return 1
    finally:
        if con is not None:
            con.close()
    print('PASS: Jaghvi deployment preflight finished.', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
