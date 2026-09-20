"""One disposable process per analysis; its process group belongs to the supervisor."""
import json
import os
import sys
# If Gunicorn crashes, kill this entire analysis group rather than orphaning
# yt-dlp/ffmpeg and allowing an old process to overwrite restart recovery.
if sys.platform == 'linux':
    import ctypes
    import signal
    parent = os.getppid()
    def parent_died(*_):
        os.killpg(os.getpgrp(), signal.SIGKILL)
    signal.signal(signal.SIGTERM, parent_died)
    if ctypes.CDLL(None).prctl(1, signal.SIGTERM) != 0:
        raise RuntimeError('Could not configure worker parent-death protection')
    if os.getppid() != parent or parent == 1:
        parent_died()
os.environ['ADSCAN_JOB_WORKER'] = '1'
from network_guard import start_proxy
proxy, address = start_proxy()
os.environ['ADSCAN_DOWNLOAD_PROXY'] = address
os.environ['http_proxy'] = address
os.environ['https_proxy'] = address
# Only downloads use the guard; requests to OpenRouter use their explicit session below.
from app import run_analysis
payload = json.load(sys.stdin)
run_analysis(**payload)
proxy.shutdown()
