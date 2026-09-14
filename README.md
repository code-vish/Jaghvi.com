# Jaghvi — Vercel deployment repository

This repository contains the Jaghvi storefront and private owner studio prepared for Vercel.

## Production architecture

- **FastAPI on Vercel** serves the storefront and owner dashboard.
- **Turso** stores products, collections, owner accounts, sessions, orders, messages, subscribers, and site settings.
- **Vercel Blob (Public)** stores product, collection, logo, hero, and story images.
- **GitHub** stores only application code and built-in brand assets. Customer data, product data, uploaded images, and passwords do not belong in GitHub.

## 1. What goes in GitHub

Push the contents of this folder as the repository root. The root of the GitHub repository must contain `pyproject.toml`, `requirements.txt`, `app/`, and `public/`.

Recommended repository contents:

```text
.gitignore
.vercelignore
.env.example
README.md
pyproject.toml
requirements.txt
manage.py
start.py
app/
  __init__.py
  db.py
  main.py
  media.py
  security.py
  templates/
public/
  static/
tests/                 # optional in GitHub; excluded from the Vercel bundle
```

Do **not** push `.env`, `.env.local`, `data/`, `.vercel/`, local SQLite files, passwords, Turso tokens, or Blob tokens.

## 2. Create/import the Vercel project

Import the GitHub repository in Vercel. Vercel should detect **FastAPI**. Keep the root directory as `./` and do not set a custom build command or output directory.

The first deployment can be left until storage is connected. If Vercel creates the project and the first build fails because storage variables are missing, that is expected; connect storage and redeploy.

## 3. Connect the persistent database

In the Vercel project, open **Storage** (or Marketplace) and add **Turso**. Connect/create a database for this project. The integration should add:

```text
TURSO_DATABASE_URL
TURSO_AUTH_TOKEN
```

The website intentionally refuses to use an ephemeral local SQLite database when it is running on Vercel.

## 4. Connect persistent image storage

In **Storage**, create a **Vercel Blob** store and choose **Public** access because product photographs are public storefront media. Connect it to the same project. Vercel adds:

```text
BLOB_READ_WRITE_TOKEN
```

Do not put that token in GitHub.

## 5. Create the first private owner login

In **Project → Settings → Environment Variables**, add these for Production (and Preview if you want the owner studio to work in previews):

```text
JAGHVI_OWNER_ID=your-master-id
JAGHVI_OWNER_EMAIL=your-email@example.com
JAGHVI_OWNER_PASSWORD=use-a-long-unique-password
```

The password must be 12–128 characters. On the first successful startup, Jaghvi hashes the password and creates the owner only if no owner already exists. It never overwrites an existing owner from these values.

After you have signed in successfully, you may remove `JAGHVI_OWNER_PASSWORD` from Vercel and redeploy. The database owner account remains intact. Password changes are available inside **Owner Studio → Security**.

## 6. Redeploy

Open **Deployments**, choose the latest deployment, and redeploy after the database, Blob store, and owner environment variables are connected.

Then open:

```text
https://YOUR-PROJECT.vercel.app/
https://YOUR-PROJECT.vercel.app/owner
```

## 7. Add jaghvi.com

After the Vercel URL works, add `jaghvi.com` under **Project → Settings → Domains** and follow the DNS records shown by Vercel. The application already allows `jaghvi.com`, `*.jaghvi.com`, and `*.vercel.app` as trusted hosts.

## Image-upload note

Vercel server functions have a request-body limit. This build therefore keeps owner-studio server uploads conservative. For the initial catalog, upload compressed JPG/PNG/WebP images with a combined form-upload size below roughly 3 MB. The server re-encodes accepted images as WebP and stores them in Vercel Blob.

If you later want original 10–30 MB camera files uploaded directly from the browser, switch the owner uploader to Vercel Blob client uploads. That is a separate improvement and does not affect the database or product model.

## Local development

Without Vercel/Turso environment variables, the same code falls back to local SQLite and local `data/uploads` storage:

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python manage.py owner
python start.py
```

Open `http://127.0.0.1:8000/` and `http://127.0.0.1:8000/owner`.

## Security notes

- Never commit environment-variable values or passwords.
- The owner password is Argon2-hashed before it is stored.
- Owner sessions are stored server-side in the persistent database.
- Product/customer data is not kept in GitHub.
- Uploaded images are validated, stripped of EXIF/active metadata, resized, and re-encoded to WebP before storage.
