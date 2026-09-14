from __future__ import annotations
import hashlib, hmac, secrets, time
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, InvalidHashError, VerificationError
from fastapi import HTTPException, Request
from .db import database

password_hasher = PasswordHasher(time_cost=3,memory_cost=65536,parallelism=2)
DUMMY_HASH = password_hasher.hash(secrets.token_urlsafe(32))
COOKIE = 'jaghvi_session'

def now(): return int(time.time())
def digest(value): return hashlib.sha256(value.encode()).hexdigest()

def password_valid(value, encoded):
    try: return password_hasher.verify(encoded or DUMMY_HASH, value)
    except (VerifyMismatchError, InvalidHashError, VerificationError): return False

def validate_password(value):
    if len(value) < 12 or len(value) > 128:
        raise ValueError('Choose a password between 12 and 128 characters. A long, unique passphrase works well.')
    if value.casefold() in {'password123456','jaghvi12345678','administrator'}:
        raise ValueError('Choose a less predictable password.')

def new_session(owner_id=None):
    token=secrets.token_urlsafe(48)
    session={'token_hash':digest(token),'csrf':secrets.token_urlsafe(32),'owner_id':owner_id,
             'data':'{}','expires_at':now()+(8*3600 if owner_id else 7*86400)}
    with database(True) as con:
        con.execute('INSERT INTO sessions(token_hash,csrf,owner_id,data,expires_at) VALUES (?,?,?,?,?)',
                    (session['token_hash'],session['csrf'],session['owner_id'],session['data'],session['expires_at']))
    return token,session

def require_owner(request: Request):
    owner=getattr(request.state,'owner',None)
    if not owner: raise HTTPException(303,headers={'Location':'/owner/login'})
    return owner

async def checked_form(request: Request):
    form=await request.form(max_files=12,max_fields=100,max_part_size=1024*1024)
    token=str(form.get('csrf',''))
    if not token or not hmac.compare_digest(token,request.state.session['csrf']):
        raise HTTPException(403,'Your form session has expired. Refresh the page and try again.')
    return form

def limited(kind, identity, limit=8, window=900):
    with database(True) as con:
        con.execute('DELETE FROM attempts WHERE created_at<?',(now()-86400,))
        count=con.execute('SELECT COUNT(*) FROM attempts WHERE kind=? AND identity=? AND created_at>?',
                          (kind,identity,now()-window)).fetchone()[0]
        if count>=limit: return True
        con.execute('INSERT INTO attempts(kind,identity,created_at) VALUES (?,?,?)',(kind,identity,now()))
    return False

def client_identity(request): return digest(request.client.host if request.client else 'unknown')

def audit(con,owner_id,action):
    con.execute('INSERT INTO audit(owner_id,action,created_at) VALUES (?,?,?)',(owner_id,action,now()))
