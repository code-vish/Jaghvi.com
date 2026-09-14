"""Persistent Turso over HTTPS on Vercel; standard SQLite only for local use."""
from __future__ import annotations
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from .db_common import (DatabaseError, IntegrityError, Row, CursorAdapter, split_sql_script)

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get('JAGHVI_DATA_DIR', ROOT / 'data'))
UPLOADS = DATA / 'uploads'
DB_PATH = DATA / 'jaghvi.sqlite3'
IS_VERCEL = os.environ.get('VERCEL') == '1'
PRODUCTION = os.environ.get('APP_ENV', 'development') == 'production' or IS_VERCEL
TURSO_URL = os.environ.get('TURSO_DATABASE_URL', '').strip()
TURSO_TOKEN = os.environ.get('TURSO_AUTH_TOKEN', '').strip()


class ConnectionAdapter:
    """Local SQLite wrapper. Remote queries do not pass through a native driver."""
    def __init__(self, raw, remote=False):
        self._raw, self.remote = raw, remote

    @staticmethod
    def _translate_error(exc):
        if isinstance(exc, sqlite3.IntegrityError):
            raise IntegrityError() from exc
        raise DatabaseError('A local database operation failed.', code='DB_QUERY_FAILED') from exc

    def execute(self, sql, params=()):
        try:
            return CursorAdapter(self._raw.execute(sql, params))
        except sqlite3.Error as exc:
            self._translate_error(exc)

    def executemany(self, sql, seq):
        try:
            return CursorAdapter(self._raw.executemany(sql, seq))
        except sqlite3.Error as exc:
            self._translate_error(exc)

    def executescript(self, script):
        # sqlite3.executescript commits an active transaction implicitly.
        # Executing parsed statements individually preserves our atomic migration.
        for statement in split_sql_script(script):
            self.execute(statement)

    def commit(self):
        try:
            self._raw.commit()
        except sqlite3.Error as exc:
            self._translate_error(exc)

    def rollback(self):
        try:
            self._raw.rollback()
        except sqlite3.Error:
            pass

    def close(self):
        self._raw.close()


DEFAULTS = {
 'brand_name':'Jaghvi', 'announcement':'The world of Jaghvi. A quieter kind of luxury.',
 'hero_eyebrow':'SILVER & FASHION JEWELRY', 'hero_title':'Elegance,',
 'hero_accent':'in its own light.',
 'hero_description':'For the everyday. For the unforgettable. Discover a world of considered jewelry, made to feel entirely your own.',
 'hero_button':'Explore the collections', 'hero_caption':'THE JAGHVI SIGNATURE',
 'collections_title':'Two expressions. One sensibility.',
 'featured_title':'The considered edit',
 'empty_title':'Something beautiful is taking shape.',
 'empty_description':'Our first edit is being thoughtfully curated. Until then, step inside the world of Jaghvi.',
 'story_eyebrow':'THE WORLD OF JAGHVI', 'story_title':'Not more.\nMore meaningful.',
 'story_body':'We believe elegance is a feeling, not an occasion. In the quiet confidence of silver. In the expressive beauty of a statement piece. In the little details that become a part of you.\n\nJaghvi brings silver and fashion jewelry into one considered world. For the way you dress. For the way you feel. For you.',
 'story_quote':'Less noise. More you.', 'footer_line':'A quieter kind of luxury.',
 'newsletter_title':'A little closer to Jaghvi.',
 'newsletter_description':'Collection notes, new arrivals, and a little inspiration. Only when there is something beautiful to share.',
 'logo':'/static/assets/jaghvi-logo.webp', 'hero_image':'', 'story_image':'',
 'background_color':'#f6f6f4', 'text_color':'#292b2a', 'accent_color':'#555b57',
 'type_style':'editorial', 'contact_email':'', 'contact_phone':'', 'instagram_url':'',
 'business_address':'', 'currency':'INR', 'currency_symbol':'₹', 'shipping_fee':'0',
 'shipping_note':'Shipping and availability will be confirmed before payment.',
 'order_requests_enabled':False, 'newsletter_enabled':False,
 'show_collections':True, 'show_featured':True, 'show_story':True,
 'section_order':'collections,featured,story,newsletter',
 'privacy_policy':'', 'shipping_policy':'', 'returns_policy':'', 'terms_policy':'',
 'seo_title':'Jaghvi | Silver & Fashion Jewelry',
 'seo_description':'Enter the world of Jaghvi. Considered silver and fashion jewelry. A quieter kind of luxury.',
}

SCHEMA = '''
CREATE TABLE IF NOT EXISTS schema_version(version INTEGER NOT NULL);
INSERT INTO schema_version SELECT 1 WHERE NOT EXISTS(SELECT 1 FROM schema_version);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS owners (
 id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
 email TEXT NOT NULL, password_hash TEXT NOT NULL, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (
 token_hash TEXT PRIMARY KEY, csrf TEXT NOT NULL, owner_id INTEGER REFERENCES owners(id),
 data TEXT NOT NULL DEFAULT '{}', expires_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS sessions_expiry ON sessions(expires_at);
CREATE TABLE IF NOT EXISTS attempts (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, identity TEXT NOT NULL, created_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS attempts_lookup ON attempts(kind,identity,created_at);
CREATE TABLE IF NOT EXISTS collections (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL, slug TEXT NOT NULL UNIQUE,
 description TEXT NOT NULL DEFAULT '', image TEXT NOT NULL DEFAULT '',
 position INTEGER NOT NULL DEFAULT 0, visible INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS products (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL, slug TEXT NOT NULL UNIQUE,
 sku TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '',
 material TEXT NOT NULL DEFAULT '', category TEXT NOT NULL DEFAULT 'Other',
 collection_id INTEGER REFERENCES collections(id) ON DELETE SET NULL,
 price INTEGER NOT NULL CHECK(price>=0), stock INTEGER NOT NULL DEFAULT 0 CHECK(stock>=0),
 status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','published','archived')),
 featured INTEGER NOT NULL DEFAULT 0, care TEXT NOT NULL DEFAULT '',
 dimensions TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS products_status ON products(status,collection_id);
CREATE TABLE IF NOT EXISTS images (
 id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
 path TEXT NOT NULL, alt TEXT NOT NULL DEFAULT '', position INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS variants (
 id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
 label TEXT NOT NULL, sku TEXT NOT NULL DEFAULT '', price INTEGER NOT NULL CHECK(price>0),
 stock INTEGER NOT NULL DEFAULT 0 CHECK(stock>=0), UNIQUE(product_id,label));
CREATE TABLE IF NOT EXISTS orders (
 id INTEGER PRIMARY KEY, reference TEXT NOT NULL UNIQUE, session_hash TEXT NOT NULL, checkout_key TEXT,
 name TEXT NOT NULL, email TEXT NOT NULL, phone TEXT NOT NULL,
 address TEXT NOT NULL, city TEXT NOT NULL, region TEXT NOT NULL,
 postal_code TEXT NOT NULL, country TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
 subtotal INTEGER NOT NULL, shipping INTEGER NOT NULL, total INTEGER NOT NULL,
 currency TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'requested', tracking TEXT NOT NULL DEFAULT '',
 inventory_reserved INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS order_items (
 id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
 product_id INTEGER REFERENCES products(id) ON DELETE SET NULL,
 variant_id INTEGER REFERENCES variants(id) ON DELETE SET NULL,
 name TEXT NOT NULL, variant TEXT NOT NULL DEFAULT '', sku TEXT NOT NULL DEFAULT '',
 quantity INTEGER NOT NULL CHECK(quantity>0), price INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS messages (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL,
 message TEXT NOT NULL, is_read INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS subscribers (
 id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE COLLATE NOCASE,
 consent_text TEXT NOT NULL, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS audit (
 id INTEGER PRIMARY KEY, owner_id INTEGER, action TEXT NOT NULL, created_at INTEGER NOT NULL);
'''


def _remote_connect():
    from .turso_http import TursoHTTPConnection
    return TursoHTTPConnection(TURSO_URL, TURSO_TOKEN)


def connect():
    # No filesystem fallback on Vercel or when remote credentials are configured.
    if IS_VERCEL or TURSO_URL or TURSO_TOKEN:
        return _remote_connect()
    DATA.mkdir(parents=True, exist_ok=True)
    try:
        raw = sqlite3.connect(DB_PATH, timeout=15, isolation_level=None)
        raw.row_factory = sqlite3.Row
        raw.execute('PRAGMA foreign_keys=ON')
        raw.execute('PRAGMA busy_timeout=15000')
        return ConnectionAdapter(raw)
    except sqlite3.Error as exc:
        raise DatabaseError('The local database could not be opened.', code='DB_LOCAL_UNAVAILABLE') from exc


@contextmanager
def database(write=False):
    con = connect()
    try:
        if write:
            # Serialize read-then-write decisions, including owner provisioning.
            con.execute('BEGIN IMMEDIATE')
        yield con
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()


def initialize():
    """Additive, atomic, idempotent initialization; preserves all existing data."""
    if not IS_VERCEL:
        UPLOADS.mkdir(parents=True, exist_ok=True)
    with database(write=True) as con:
        con.executescript(SCHEMA)
        columns = {row['name'] for row in con.execute('PRAGMA table_info(orders)')}
        if 'checkout_key' not in columns:
            con.execute('ALTER TABLE orders ADD COLUMN checkout_key TEXT')
        con.execute('CREATE UNIQUE INDEX IF NOT EXISTS orders_checkout_key ON orders(checkout_key)')
        # Defaults are inserted together, not as dozens of HTTP round trips.
        con.executemany('INSERT OR IGNORE INTO settings(key,value) VALUES (?,?)',
                        [(key, json.dumps(value)) for key, value in DEFAULTS.items()])
        marker = con.execute("SELECT value FROM settings WHERE key='_catalog_seeded'").fetchone()
        if marker is None:
            if not con.execute('SELECT 1 FROM collections').fetchone():
                con.executemany('INSERT INTO collections(name,slug,description,position) VALUES (?,?,?,?)', [
                    ('The Silver Collection','silver','A quiet luminosity. An enduring point of view.',0),
                    ('Fashion Jewelry','fashion','A little expression. An entirely different feeling.',1),
                ])
            con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES ('_catalog_seeded','true')")
        con.execute('UPDATE schema_version SET version=2 WHERE version<2')


def site_settings(con):
    stored = {}
    for row in con.execute('SELECT key,value FROM settings'):
        if row['key'].startswith('_'):
            continue
        try:
            stored[row['key']] = json.loads(row['value'])
        except (json.JSONDecodeError, TypeError):
            raise DatabaseError('A saved setting contains invalid JSON.', code='DB_SETTINGS_INVALID') from None
    return {**DEFAULTS, **stored}


def rows(cursor):
    return [dict(row) for row in cursor.fetchall()]
