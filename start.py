#!/usr/bin/env python3
"""One-command local start. Creates a private environment and your owner account."""
import os, pathlib, subprocess, sys, threading, venv, webbrowser
ROOT=pathlib.Path(__file__).resolve().parent
os.chdir(ROOT)
if sys.version_info<(3,11):raise SystemExit('Install Python 3.11 or newer, then run this file again.')
virtual=ROOT/'.venv'
python=virtual/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
try:
    if not python.exists():
        print('Setting up your private Jaghvi environment. This first step requires internet access...')
        venv.create(virtual,with_pip=True)
    fingerprint=(ROOT/'requirements.txt').read_text()
    marker=virtual/'.jaghvi-requirements'
    if not marker.exists() or marker.read_text()!=fingerprint:
        subprocess.run([str(python),'-m','pip','install','-r','requirements.txt'],check=True)
        marker.write_text(fingerprint)
    subprocess.run([str(python),'manage.py','init'],check=True)
    subprocess.run([str(python),'manage.py','owner'],check=True)
    print('\nJaghvi storefront: http://127.0.0.1:8000\nOwner studio: http://127.0.0.1:8000/owner\nPress Ctrl+C to stop.\n')
    threading.Timer(2.0,lambda:webbrowser.open('http://127.0.0.1:8000/owner')).start()
    subprocess.run([str(python),'-m','uvicorn','app.main:app','--host','127.0.0.1','--port','8000'],check=True)
except KeyboardInterrupt: print('\nJaghvi stopped. Your products and images remain saved in the data folder.')
except subprocess.CalledProcessError as exc:
    print('\nSetup or startup failed. Check your internet connection and the error above. No product data was deleted.')
    sys.exit(exc.returncode)
