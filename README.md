# Jaghvi: complete HTTP database repair

**Build identifier:** `jaghvi-http-repair-20260914`  
**Entry point:** `app.main:app`  
**Database:** the existing Turso database, accessed through SQL over HTTPS.  
**Product images:** Vercel Blob after the store is connected.  
**Local development:** SQLite and local uploads, only outside Vercel.

Start with **REPLACE-INSTRUCTIONS.txt**. This package supersedes the previous
Vercel-ready archives and the single-file cursor patch. It retains the supplied
storefront, logo, owner studio, product model, and existing database schema.

## What this repair changes

- Removes the `libsql` Python dependency and native cursor adapter from remote
  database operations. Turso uses its documented `/v2/pipeline` HTTPS interface.
- Decodes named columns, integer IDs, prices, NULLs and other results explicitly.
  There is no assumption that a remote native cursor behaves like sqlite3.Cursor.
- Preserves connection batons, server routing, foreign keys and explicit
  transactions. A lost network response is not blindly retried as a new write.
- Batches default settings, storefront changes and image records to avoid
  unnecessary round trips inside short remote transactions.
- Retains existing data. Schema initialization is additive, transactional and
  idempotent. It does not drop tables, clear products or replace existing owners.
- Uses module-relative absolute template/static paths and disables static-file
  exclusion from the function. The old public/ directory is ignored if present.
- Runs startup outside the ASGI event loop. Setup failures remain failures, but
  return a safe HTTP 503 diagnostic instead of terminating the entire function.
- Adds an actual readiness query and build identifier at `/health`. Missing
  storage configuration never switches a Vercel store to an ephemeral database.
- Adds a build preflight that checks files, imports and the database connection.
  The database portion runs read-only queries: it does not create the schema or
  owner account during the build.

## Replace and deploy

Copy the **contents** of this folder into your existing project. Overwrite the
matching application/configuration files, and include the new `scripts/` and
Python modules. Keep `.git`, private environment files, `.vercel`, and local data.
Do not delete/recreate GitHub, Vercel or Turso.

```bash
git add -A
git commit -m "Install complete Jaghvi HTTP repair"
git push
```

Keep **FastAPI**, root **./**, and default build/output settings. The build
command is already declared in `pyproject.toml`. A custom Build Command configured
in the dashboard can override it, so retain the default setting.

Check that the new deployment uses this commit. Visit `/health`; success is:

```json
{"status":"ok","build":"jaghvi-http-repair-20260914","database":"reachable"}
```

Then open the storefront and `/owner`. The health response is not proof that
payments, email, image storage or courier integrations have been connected.

## Existing credentials

Keep these existing Vercel environment variables. Do not put their values into
GitHub, screenshots, support messages or this README:

```text
TURSO_DATABASE_URL
TURSO_AUTH_TOKEN
```

The empty storefront does not require an owner password or Blob token. Later,
create the first owner by setting the following **in Vercel**, never in GitHub:

```text
JAGHVI_OWNER_ID
JAGHVI_OWNER_EMAIL
JAGHVI_OWNER_PASSWORD
```

Use an ID of 3-64 letters, digits, dots, hyphens or underscores; a valid email;
and a private, unique password of 12-128 characters. An existing account is
never replaced by this bootstrap. After successful sign-in, remove the bootstrap
password from environment variables and redeploy. The hashed database account
is retained; change the password in Owner Studio → Security.

Connect a Public Vercel Blob store before cloud photo uploads. It supplies
`BLOB_READ_WRITE_TOKEN`. Until then, uploads are rejected with an explanation,
not saved to a temporary Vercel filesystem. Existing stored image URLs are kept.

Preview deployments currently use whichever database is connected to Preview.
When that is the production database, preview writes affect production. Use a
separate database for future development/testing; this repair does not change
your current integration or create a branch.

## Diagnostic codes

`DB_CONFIG_MISSING`: the database variables are not available to that deployment.  
`DB_URL_INVALID`: the database URL is malformed or the endpoint was not found.  
`DB_AUTH_REJECTED`: the database rejected its configured credential.  
`DB_NETWORK`: the database could not be reached.  
`DB_LIMIT`: the database is enforcing usage/rate limits.  
`DB_BUSY`: a lock or competing write prevented the query.  
`DB_CONNECTION_EXPIRED`: the interactive database connection expired.  
`DB_QUERY_FAILED`: a SQL operation failed.  
`OWNER_SETUP_INVALID`: owner bootstrap values failed validation.  
`APP_STARTUP_ERROR`: an unexpected initialization error occurred.

An unavailable response is HTTP 503 and is never reported as database-ready.
Do not resolve these errors by resetting the database or committing credentials.

## Local development and tests

Use Python 3.12 or 3.13. The deployment requests Python 3.12 in `.python-version`.
Dependencies remain pinned in both requirements.txt and pyproject.toml.

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q tests
```

Run the same storefront suite over the HTTP adapter using a disposable local
protocol server:

```bash
# macOS / Linux
JAGHVI_TEST_BACKEND=http python -m pytest -q tests/test_store.py
```

```powershell
# Windows PowerShell
$env:JAGHVI_TEST_BACKEND = "http"
python -m pytest -q tests/test_store.py
Remove-Item Env:JAGHVI_TEST_BACKEND
```

Tests deliberately remove inherited live credentials before importing the app.
They never use a customer's database. Six subprocess tests generate temporary
HTTPS certificates using OpenSSL; those tests skip where OpenSSL is unavailable.
No certificate, private key or test database is included in this package.

For local store operation without production variables:

```bash
python manage.py owner
python start.py
```

## Verification limits

See TEST-RESULTS.txt. Tests use local SQLite and a disposable HTTP/HTTPS protocol
emulator, **not a live Turso service**. Vercel's actual build/bundle/runtime, real
Turso credentials, Blob uploads, payments, domain DNS and email delivery were not
accessible here and have not been claimed as verified. Native libsql could not be
downloaded in the build environment; it is removed from this replacement.

This package retains unpaid order requests, not a live payment integration.
Confirm your policies, owner access, images, backups and business settings before
accepting customer orders. Local backup commands intentionally refuse a remote
store; use Turso and Blob backup/export facilities for its real data.

## Implementation references

- Turso SQL over HTTP: https://docs.turso.tech/sdk/http/reference
- Turso HTTP quickstart: https://docs.turso.tech/sdk/http/quickstart
- Vercel FastAPI entrypoints, static configuration, lifespan and build scripts:
  https://vercel.com/docs/frameworks/backend/fastapi
- Git-connected deployments: https://vercel.com/docs/git

These references describe supported interfaces. They are not evidence that this
specific store has already deployed successfully.
