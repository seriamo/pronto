"""Opt-in Seriamo diagnostics for Pronto.

Off unless SERIAMO_DIAGNOSTICS=1. When on, Pronto reports two events to
telemetry.seriamo.com: `service_start`, and `calendar_sync` with outcome ok/error.
No install ID, calendar URLs, event titles, names or error messages are sent.
"""

import os
import subprocess
import threading
import uuid
from datetime import datetime, timezone

import requests

PRODUCT = 'pronto'
PLATFORM = 'linux'
DEFAULT_ENDPOINT = 'https://telemetry.seriamo.com/v1/events'
TIMEOUT_SECONDS = 5

_TRUTHY = {'1', 'true', 'yes', 'on'}


def enabled():
    return os.getenv('SERIAMO_DIAGNOSTICS', '').strip().lower() in _TRUTHY


def endpoint():
    return os.getenv('SERIAMO_TELEMETRY_URL', DEFAULT_ENDPOINT)


def _detect_version():
    """Short git commit of the running checkout, or 'unknown'."""
    try:
        out = subprocess.run(
            ['git', 'rev-parse', '--short', 'HEAD'],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            capture_output=True, text=True, timeout=2, check=True,
        ).stdout.strip()
        return out if out and out.isalnum() and len(out) <= 32 else 'unknown'
    except (OSError, subprocess.SubprocessError):
        return 'unknown'


APP_VERSION = _detect_version()


def build_envelope(event, props=None, now=None):
    item = {
        'event_id': str(uuid.uuid4()),
        'product': PRODUCT,
        'event': event,
        'app_version': APP_VERSION,
        'platform': PLATFORM,
        'occurred_at': (now or datetime.now(timezone.utc)).strftime('%Y-%m-%dT%H:%M:%SZ'),
    }
    if props:
        item['props'] = props
    return {'schema_version': 1, 'events': [item]}


def _post(envelope):
    try:
        requests.post(endpoint(), json=envelope, timeout=TIMEOUT_SECONDS)
    except requests.RequestException:
        pass  # Diagnostics never affect the display.


def record(event, props=None, sync=False):
    """Send one event in the background. Returns the thread (or None when disabled)."""
    if not enabled():
        return None
    envelope = build_envelope(event, props)
    if sync:
        _post(envelope)
        return None
    thread = threading.Thread(target=_post, args=(envelope,), daemon=True)
    thread.start()
    return thread


def service_start():
    return record('service_start')


def calendar_sync(ok):
    return record('calendar_sync', {'outcome': 'ok' if ok else 'error'})
