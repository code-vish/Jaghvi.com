#!/usr/bin/env python3
"""Local/server-only owner provisioning, password recovery, and backup tools."""
from __future__ import annotations
import argparse, getpass, json, re, secrets, sqlite3, sys, time, zipfile
from pathlib import Path
from app.db import DATA, DB_PATH, UPLOADS, database, initialize
from app.security import password_hasher, validate_password

def read_password():
    while True:
        value=getpass.getpass('Choose a private password (at least 12 characters): ')
        try: validate_password(value)
        except ValueError as exc: print(exc);continue
        if value!=getpass.getpass('Confirm password: '): print('Passwords did not match. Try again.');continue
        return value

def create_owner():
    initialize()
    with database() as con:
        if con.execute('SELECT 1 FROM owners').fetchone():
            print('An owner already exists. Use reset-password to recover access.');return
    print('\nCreate your private Jaghvi master account. There is no default password.\n')
    while True:
        username=input('Master ID (letters, digits, dots, hyphens or underscores): ').strip()
        if re.fullmatch(r'[A-Za-z0-9_.-]{3,64}',username):break
        print('Use 3–64 letters, digits, dots, hyphens, or underscores.')
    while True:
        email=input('Owner email address: ').strip().lower()
        if re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',email):break
        print('Enter a valid email address.')
    encoded=password_hasher.hash(read_password())
    with database(True) as con:
        if con.execute('SELECT 1 FROM owners').fetchone(): raise SystemExit('An owner was created in another session. No changes made.')
        con.execute('INSERT INTO owners(username,email,password_hash,created_at) VALUES (?,?,?,?)',(username,email,encoded,int(time.time())))
    print('\nOwner created. Sign in at /owner/login after starting your website.\n')

def reset_password():
    initialize()
    with database() as con: owner=con.execute('SELECT id,username FROM owners LIMIT 1').fetchone()
    if not owner: print('No owner exists. Run python manage.py owner first.');return
    print(f'Resetting the password for {owner["username"]}. This signs out all owner sessions.')
    encoded=password_hasher.hash(read_password())
    with database(True) as con:
        con.execute('UPDATE owners SET password_hash=? WHERE id=?',(encoded,owner['id']))
        con.execute('DELETE FROM sessions WHERE owner_id=?',(owner['id'],))
        con.execute("DELETE FROM attempts WHERE kind LIKE 'login-%'")
    print('Password reset. All previous owner sessions were revoked.')

def backup():
    initialize();directory=DATA/'backups';directory.mkdir(exist_ok=True)
    stamp=time.strftime('%Y%m%d-%H%M%S');snapshot=directory/f'jaghvi-{stamp}.sqlite3'
    with sqlite3.connect(DB_PATH) as source,sqlite3.connect(snapshot) as target:source.backup(target)
    dest=directory/f'jaghvi-{stamp}.zip'
    with zipfile.ZipFile(dest,'w',zipfile.ZIP_DEFLATED) as z:
        z.write(snapshot,'jaghvi.sqlite3')
        for path in UPLOADS.iterdir():
            if path.is_file():z.write(path,'uploads/'+path.name)
    snapshot.unlink();dest.chmod(0o600)
    print(f'Backup written: {dest}')
    print('This archive contains private business/customer data. Store it encrypted, away from the public server. Pause writes for a file-and-database-consistent backup.')

def cleanup():
    initialize()
    with database(True) as con:
        con.execute('DELETE FROM sessions WHERE expires_at<?',(int(time.time()),))
        con.execute('DELETE FROM attempts WHERE created_at<?',(int(time.time())-86400,))
    print('Expired sessions and old rate-limit records removed.')

def main():
    parser=argparse.ArgumentParser(description='Manage your Jaghvi website locally or on your own server.')
    parser.add_argument('command',choices=['init','owner','reset-password','backup','cleanup'])
    args=parser.parse_args()
    {'init':initialize,'owner':create_owner,'reset-password':reset_password,'backup':backup,'cleanup':cleanup}[args.command]()

if __name__=='__main__':main()
