#!/usr/bin/env python3
"""
Pronto: Flask web application for a desk availability display
Renders status as HTML instead of generating images
"""

from flask import Flask, render_template, jsonify, request, abort, Response, g, url_for
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
import requests
import json
import pytz
import re
import os
import time
import threading
import hashlib
import ipaddress
import jwt
from dotenv import load_dotenv
from dateutil import rrule
from dateutil.rrule import rrulestr

# Load environment variables
load_dotenv()

app = Flask(__name__)

# Get configuration from environment variables
ICS_URL = os.getenv('ICS_URL')
ICS_URLS_EXTRA = [u.strip() for u in os.getenv('ICS_URLS_EXTRA', '').split(',') if u.strip()]
USER_NAME = os.getenv('USER_NAME', '').strip()
TIMEZONE = os.getenv('TIMEZONE', 'America/Los_Angeles')
LOCAL_TZ = pytz.timezone(TIMEZONE)

# Display personalization (all optional)
ROOM_NUMBER = os.getenv('ROOM_NUMBER', '').strip()
# Events whose title starts with this keyword (case-insensitive) count as OOO, e.g. "jdoe ooo"
OOO_KEYWORD = os.getenv('OOO_KEYWORD', '').strip().lower()
LOGO_FILE = os.getenv('LOGO_FILE', 'logo.svg').strip().lstrip('/')        # relative to static/
QR_FILE = os.getenv('QR_FILE', '').strip().lstrip('/')                    # relative to static/
PUBLIC_URL = os.getenv('PUBLIC_URL', '').strip().rstrip('/')              # e.g. https://desk.example.com
EASTER_EGG_MESSAGE = os.getenv('EASTER_EGG_MESSAGE', '👋 Thanks for stopping by!').strip()

# Server binding — localhost only by default; cloudflared reaches the app via localhost
HOST = os.getenv('HOST', '127.0.0.1')
PORT = int(os.getenv('PORT', '5001'))

# Cloudflare Access — remote requests must carry a valid Access JWT
CF_ACCESS_TEAM_DOMAIN = re.sub(r'^https?://', '', os.getenv('CF_ACCESS_TEAM_DOMAIN', '').strip()).rstrip('/')
CF_ACCESS_AUD = [a.strip() for a in os.getenv('CF_ACCESS_AUD', '').split(',') if a.strip()]
OWNER_EMAILS = {e.strip().lower() for e in os.getenv('OWNER_EMAILS', '').split(',') if e.strip()}
OWNER_SERVICE_TOKENS = {t.strip() for t in os.getenv('OWNER_SERVICE_TOKENS', '').split(',') if t.strip()}

if not ICS_URL:
    raise ValueError("Missing ICS_URL environment variable. Please check your .env file.")

# All calendar URLs (primary + extras)
ALL_ICS_URLS = [ICS_URL] + ICS_URLS_EXTRA

# Cache settings — one cache file per calendar
CACHE_DIR = "calendar_caches"
CACHE_DURATION_BUSINESS = 1800   # 30 minutes during business hours (7am-6pm)
CACHE_DURATION_OFFHOURS = 14400  # 4 hours outside business hours
PARSED_CACHE_FILE = "calendar_parsed_cache.json"
REQUESTS_TIMEOUT = 15  # seconds - prevent indefinite hangs on HTTP requests

os.makedirs(CACHE_DIR, exist_ok=True)


def _is_business_window():
    """Check if current time is within the active refresh window (7am-6pm local)."""
    now = datetime.now(LOCAL_TZ)
    return 7 <= now.hour < 18


def _get_cache_duration():
    """Return cache duration based on time of day: 30 min during 7am-6pm, 4 hrs otherwise."""
    return CACHE_DURATION_BUSINESS if _is_business_window() else CACHE_DURATION_OFFHOURS

# Performance optimization: Pre-compile regex patterns
UID_PATTERN = re.compile(r'UID:(.*?)(?:\r?\n|$)')
RRULE_PATTERN = re.compile(r'RRULE:(.*?)(?:\r?\n|$)')
DTSTART_PATTERN = re.compile(r'DTSTART(?:;TZID=([^:]+))?:(\d{8}T\d{6})')
DTEND_PATTERN = re.compile(r'DTEND(?:;TZID=([^:]+))?:(\d{8}T\d{6})')
SUMMARY_PATTERN = re.compile(r'SUMMARY:(.*?)(?:\r?\n|$)')
TRANSP_PATTERN = re.compile(r'TRANSP:(.*?)(?:\r?\n|$)')
BUSYSTATUS_PATTERN = re.compile(r'X-MICROSOFT-CDO-BUSYSTATUS:(.*?)(?:\r?\n|$)')
RECURRENCE_ID_PATTERN = re.compile(r'RECURRENCE-ID(?:;TZID=([^:]+))?:(\d{8}T\d{6})')
EXDATE_PATTERN = re.compile(r'EXDATE(?:;TZID=([^:]+))?:(.*?)(?:\r?\n|$)')

# Performance optimization: In-memory parsed events cache
_parsed_events_cache = {
    'date': None,
    'events': None,
    'parsed_data': None,
    'timestamp': 0,
    'cal_mtime': 0
}

# Location system: schedule-based defaults + per-day overrides
LOCATION_OVERRIDE_FILE = "location_overrides.json"
WEEKDAY_NAMES = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
# Default: office Tue/Wed/Thu 10am-3pm, remote otherwise (override with OFFICE_DAYS / OFFICE_HOURS)
OFFICE_DAYS = {WEEKDAY_NAMES.index(d.strip()[:3].title())
               for d in os.getenv('OFFICE_DAYS', 'Tue,Wed,Thu').split(',')
               if d.strip()[:3].title() in WEEKDAY_NAMES}
BUSINESS_START_HOUR = int(os.getenv('BUSINESS_START_HOUR', '8'))
BUSINESS_END_HOUR = int(os.getenv('BUSINESS_END_HOUR', '18'))
# Location defaults: office on these days/hours (separate from business hours)
OFFICE_LOCATION_START, OFFICE_LOCATION_END = (int(h) for h in os.getenv('OFFICE_HOURS', '10-15').split('-', 1))

# Calendar health — stale after 6 hours without a refresh
CALENDAR_STALE_THRESHOLD = 6 * 3600
_last_fetch_error = None  # Stores last calendar fetch error message + timestamp

# Refresh version — bumped on force refresh so displays know to reload
_refresh_version = 0

# Dev mode — disables sleep screen for 4 hours
_dev_mode_until = 0

# Message system — in-memory with TTL + persistent history
MESSAGE_TTL = 180  # 3 minutes
MESSAGE_MAX_LEN = 160
MESSAGE_LOG_FILE = "message_history.json"
# Denylist: block control chars (except tab/newline/CR), HTML-dangerous chars, backslash, backtick
MESSAGE_PATTERN = re.compile(r"\A[^\x00-\x08\x0b\x0c\x0e-\x1f\x7f<>{}\\`]+\Z")
_active_message = {
    'text': None,
    'sender': None,
    'expires': 0,
}

# OOO override — persisted to file, checked before calendar
OOO_OVERRIDE_FILE = "ooo_override.json"


class SimpleEvent:
    """Lightweight event wrapper for status checks."""
    __slots__ = ('name', 'uid', 'end', 'all_day', 'busystatus')

    def __init__(self, name, uid, end, all_day=False, busystatus='BUSY'):
        self.name = name
        self.uid = uid
        self.end = end
        self.all_day = all_day
        self.busystatus = busystatus


# --- Calendar helpers ---

def _cache_file_for(url):
    """Get the cache filename for a given ICS URL."""
    url_hash = hashlib.md5(url.encode()).hexdigest()[:10]
    return os.path.join(CACHE_DIR, f"cal_{url_hash}.ics")


def _get_newest_cache_mtime():
    """Get the newest mtime across all calendar cache files (for parsed cache invalidation)."""
    mtimes = []
    for url in ALL_ICS_URLS:
        cf = _cache_file_for(url)
        if os.path.exists(cf):
            mtimes.append(os.path.getmtime(cf))
    return max(mtimes) if mtimes else 0


def _get_calendar_health():
    """Get calendar health info — age, staleness, per-calendar details."""
    now = time.time()
    calendars = []
    for url in ALL_ICS_URLS:
        cf = _cache_file_for(url)
        if os.path.exists(cf):
            mtime = os.path.getmtime(cf)
            age = now - mtime
            calendars.append({
                'file': os.path.basename(cf),
                'last_refresh': datetime.fromtimestamp(mtime).astimezone().isoformat(),
                'age_seconds': int(age),
                'age_human': f"{int(age // 3600)}h {int((age % 3600) // 60)}m ago",
                'stale': age > CALENDAR_STALE_THRESHOLD,
            })
        else:
            calendars.append({
                'file': os.path.basename(cf),
                'last_refresh': None,
                'age_seconds': None,
                'age_human': 'never fetched',
                'stale': True,
            })
    newest_mtime = _get_newest_cache_mtime()
    overall_age = now - newest_mtime if newest_mtime else None
    return {
        'stale': overall_age is None or overall_age > CALENDAR_STALE_THRESHOLD,
        'age_seconds': int(overall_age) if overall_age else None,
        'last_refresh': datetime.fromtimestamp(newest_mtime).astimezone().isoformat() if newest_mtime else None,
        'calendars': calendars,
    }


def _download_calendar(index, url):
    """Download one ICS feed into its cache file. Returns the text, or None on failure.

    ICS URLs embed a secret token, so errors are logged and exposed by calendar
    number and exception type only — never the URL or the exception message.
    """
    global _last_fetch_error
    try:
        resp = requests.get(url, timeout=REQUESTS_TIMEOUT)
        resp.raise_for_status()
        cal_text = resp.text
        with open(_cache_file_for(url), 'w') as f:
            f.write(cal_text)
        if _last_fetch_error and _last_fetch_error.get('calendar') == index + 1:
            _last_fetch_error = None
        return cal_text
    except Exception as e:
        _last_fetch_error = {
            'calendar': index + 1,
            'error': type(e).__name__,
            'timestamp': datetime.now(LOCAL_TZ).isoformat(),
        }
        print(f"⚠️ Calendar #{index + 1} fetch failed: {type(e).__name__}")
        return None


def _background_calendar_refresh():
    """Background thread that pre-fetches all ICS calendars.
    
    Schedule: every 30 min during 7am-6pm, every 4 hrs off-hours.
    Forces a refresh at 7am for the new day.
    """
    last_7am_refresh_date = None

    while True:
        try:
            now = datetime.now(LOCAL_TZ)
            sleep_duration = _get_cache_duration()

            if now.hour >= 7 and last_7am_refresh_date != now.date():
                print(f"🌅 7am refresh: fetching {len(ALL_ICS_URLS)} calendar(s)...")
                last_7am_refresh_date = now.date()
            else:
                time.sleep(sleep_duration)

            for i, url in enumerate(ALL_ICS_URLS):
                _download_calendar(i, url)

            label = "business-hours" if _is_business_window() else "off-hours"
            print(f"✅ Background refresh: {len(ALL_ICS_URLS)} cal(s) ({label}, next in {sleep_duration // 60}min)")
        except Exception as e:
            print(f"⚠️ Background refresh failed: {e}")
            time.sleep(60)


def get_cached_calendar():
    """Get merged calendar text from all configured ICS sources.
    
    Each calendar is cached individually. Returns concatenated text
    so the single-pass parser sees events from all calendars.
    """
    all_cal_text = []
    
    for i, url in enumerate(ALL_ICS_URLS):
        cache_file = _cache_file_for(url)
        cal_text = None
        
        if os.path.exists(cache_file):
            cache_age = time.time() - os.path.getmtime(cache_file)
            cache_date = datetime.fromtimestamp(os.path.getmtime(cache_file)).date()
            today = datetime.now().date()
            
            if cache_date != today:
                os.remove(cache_file)
            elif cache_age < _get_cache_duration():
                with open(cache_file, 'r') as f:
                    cal_text = f.read()
        
        if cal_text is None:
            cal_text = _download_calendar(i, url)
            if cal_text is None and os.path.exists(cache_file):
                with open(cache_file, 'r') as f:
                    cal_text = f.read()
        
        if cal_text:
            all_cal_text.append(cal_text)
    
    if not all_cal_text:
        raise RuntimeError("No calendars available")
    
    print(f"📦 Loaded {len(all_cal_text)} calendar(s)")
    return '\n'.join(all_cal_text)


def get_cached_parsed_events(target_date, cal_text):
    """
    Get parsed events and parsed_data from cache if available and fresh.
    Returns (events, parsed_data) or (None, None).
    """
    global _parsed_events_cache
    
    # Check in-memory cache first
    cal_mtime = _get_newest_cache_mtime()
    if (_parsed_events_cache['date'] != target_date):
        print(f"📅 Date changed from {_parsed_events_cache['date']} to {target_date}, invalidating cache")
        return None, None
    
    if (_parsed_events_cache['date'] == target_date and
        _parsed_events_cache['cal_mtime'] == cal_mtime):
        print(f"⚡ Using in-memory parsed events cache")
        return _parsed_events_cache['events'], _parsed_events_cache['parsed_data']
    
    # Check disk cache (events only — parsed_data is not persisted to disk)
    if os.path.exists(PARSED_CACHE_FILE):
        try:
            with open(PARSED_CACHE_FILE, 'r') as f:
                cache = json.load(f)
                cache_key = target_date.isoformat()
                if cache_key in cache:
                    entry = cache[cache_key]
                    if entry['cal_mtime'] == cal_mtime:
                        print(f"⚡ Using disk parsed events cache")
                        # Deserialize datetime objects
                        events = []
                        for e in entry['events']:
                            e['start'] = datetime.fromisoformat(e['start'])
                            e['end'] = datetime.fromisoformat(e['end'])
                            events.append(e)
                        
                        # Update in-memory cache (no parsed_data from disk)
                        _parsed_events_cache = {
                            'date': target_date,
                            'events': events,
                            'parsed_data': None,
                            'timestamp': time.time(),
                            'cal_mtime': cal_mtime
                        }
                        return events, None
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            print(f"⚠️ Corrupt parsed cache, ignoring: {e}")
        except Exception as e:
            print(f"⚠️ Error loading parsed cache: {e}")
    
    return None, None


def save_parsed_events_cache(target_date, events, cal_mtime, parsed_data=None):
    """Save parsed events to both in-memory and disk cache."""
    global _parsed_events_cache
    
    # Update in-memory cache (includes parsed_data for zero-cost cache hits)
    _parsed_events_cache = {
        'date': target_date,
        'events': events,
        'parsed_data': parsed_data,
        'timestamp': time.time(),
        'cal_mtime': cal_mtime
    }
    
    # Save to disk
    try:
        cache = {}
        if os.path.exists(PARSED_CACHE_FILE):
            try:
                with open(PARSED_CACHE_FILE, 'r') as f:
                    cache = json.load(f)
            except (json.JSONDecodeError, TypeError):
                cache = {}  # Reset if corrupt
        
        # Serialize datetime objects
        serialized_events = []
        for e in events:
            e_copy = e.copy()
            e_copy['start'] = e['start'].isoformat()
            e_copy['end'] = e['end'].isoformat()
            serialized_events.append(e_copy)
        
        cache_key = target_date.isoformat()
        cache[cache_key] = {
            'events': serialized_events,
            'timestamp': time.time(),
            'cal_mtime': cal_mtime
        }
        
        # Keep only last 7 days
        if len(cache) > 7:
            sorted_keys = sorted(cache.keys())
            for old_key in sorted_keys[:-7]:
                del cache[old_key]
        
        with open(PARSED_CACHE_FILE, 'w') as f:
            json.dump(cache, f)
        print(f"💾 Saved parsed events to cache")
    except Exception as e:
        print(f"⚠️ Error saving parsed cache: {e}")


def _load_location_overrides():
    """Load per-day location overrides from disk."""
    if os.path.exists(LOCATION_OVERRIDE_FILE):
        try:
            with open(LOCATION_OVERRIDE_FILE, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


def _save_location_overrides(overrides):
    """Save per-day location overrides to disk."""
    # Keep only last 30 days
    if len(overrides) > 30:
        sorted_keys = sorted(overrides.keys())
        for old_key in sorted_keys[:-30]:
            del overrides[old_key]
    with open(LOCATION_OVERRIDE_FILE, 'w') as f:
        json.dump(overrides, f)


def get_location():
    """Get work location based on schedule defaults + overrides.
    
    Default: office on Tue/Wed/Thu 10am-3pm, remote otherwise.
    Overrides: per-day overrides set via /api/location/set.
    """
    now = datetime.now(LOCAL_TZ)
    today_key = now.date().isoformat()
    
    # Check for per-day override first
    overrides = _load_location_overrides()
    if today_key in overrides:
        return overrides[today_key]
    
    # Default schedule: office on Tue/Wed/Thu during work hours
    if now.weekday() in OFFICE_DAYS and OFFICE_LOCATION_START <= now.hour < OFFICE_LOCATION_END:
        return "office"
    
    return "remote"


def _get_ooo_override():
    """Check if there's an active OOO override. Returns return_date (datetime) or None."""
    if not os.path.exists(OOO_OVERRIDE_FILE):
        return None
    try:
        with open(OOO_OVERRIDE_FILE, 'r') as f:
            data = json.load(f)
        return_date = datetime.fromisoformat(data['return_date']).astimezone()
        if datetime.now().astimezone() < return_date:
            return return_date
        # Expired — clean up
        os.remove(OOO_OVERRIDE_FILE)
    except (json.JSONDecodeError, KeyError, ValueError, OSError):
        pass
    return None


def _set_ooo_override(return_date_str):
    """Set OOO override until return_date. Pass None to clear."""
    if return_date_str is None:
        if os.path.exists(OOO_OVERRIDE_FILE):
            os.remove(OOO_OVERRIDE_FILE)
        return True
    try:
        return_date = datetime.fromisoformat(return_date_str)
        if return_date.tzinfo is None:
            return_date = LOCAL_TZ.localize(return_date)
        with open(OOO_OVERRIDE_FILE, 'w') as f:
            json.dump({'return_date': return_date.isoformat(), 'set_at': datetime.now().astimezone().isoformat()}, f)
        return True
    except (ValueError, OSError) as e:
        print(f"⚠️ Error setting OOO override: {e}")
        return False


def is_user_ooo_event(event):
    """Check if event is user's OOO — by busystatus OOF or event name."""
    if not event:
        return False
    # Check busystatus field (Outlook sets OOF for Out of Facility/Office)
    busystatus = getattr(event, 'busystatus', None) or ''
    if busystatus.upper() == 'OOF':
        return True
    # Fallback: check event name
    if not event.name:
        return False
    return _is_ooo_title(event.name)


def _is_ooo_title(name):
    """Check whether an event title marks the user as out of office."""
    if not name:
        return False
    title = name.strip().lower()
    if OOO_KEYWORD and title.startswith(OOO_KEYWORD):
        return True
    return 'out of office' in title


def parse_ics_single_pass_optimized(cal_text, local_tz):
    """
    HIGH IMPACT OPTIMIZATION: Single-pass parser that extracts everything we need.
    Builds tz_cache, recurrence_map, busystatus_map, recurring_uids, and event_blocks
    in one pass instead of multiple O(n) scans.
    """
    result = {
        'tz_cache': {},
        'recurrence_map': {},
        'busystatus_map': {},
        'recurring_uids': set(),
        'event_blocks': []
    }
    
    try:
        # Unfold and split once
        unfolded = cal_text.replace('\r\n ', '').replace('\n ', '')
        events = unfolded.split('BEGIN:VEVENT')[1:]
        
        for event_block in events:
            if not event_block.strip():
                continue
            
            # Extract all fields using pre-compiled patterns
            uid_match = UID_PATTERN.search(event_block)
            rrule_match = RRULE_PATTERN.search(event_block)
            dtstart_match = DTSTART_PATTERN.search(event_block)
            recurrence_id_match = RECURRENCE_ID_PATTERN.search(event_block)
            busystatus_match = BUSYSTATUS_PATTERN.search(event_block)
            
            if not uid_match:
                continue
            
            uid = uid_match.group(1).strip()
            
            # Build timezone cache
            if dtstart_match and dtstart_match.group(1):
                result['tz_cache'][uid] = dtstart_match.group(1)
            
            # Build busystatus map
            result['busystatus_map'][uid] = busystatus_match.group(1).strip() if busystatus_match else 'BUSY'
            
            # Track recurring master UIDs (has RRULE but no RECURRENCE-ID)
            has_rrule = rrule_match is not None
            has_recurrence_id = recurrence_id_match is not None
            if has_rrule and not has_recurrence_id:
                result['recurring_uids'].add(uid)
            
            # Build recurrence ID map
            if recurrence_id_match:
                recur_tz = recurrence_id_match.group(1)
                recur_str = recurrence_id_match.group(2)
                try:
                    recur_naive = datetime.strptime(recur_str, '%Y%m%dT%H%M%S')
                    if recur_tz:
                        pytz_tz_name = MICROSOFT_TZ_MAP.get(recur_tz, recur_tz)
                        source_tz = pytz.timezone(pytz_tz_name)
                        recur_dt = source_tz.localize(recur_naive).astimezone(local_tz)
                    else:
                        recur_dt = local_tz.localize(recur_naive)
                    
                    if uid not in result['recurrence_map']:
                        result['recurrence_map'][uid] = []
                    result['recurrence_map'][uid].append(recur_dt)
                except Exception:
                    pass
            
            # Store event block for later processing
            result['event_blocks'].append({
                'uid': uid,
                'block': event_block,
                'has_rrule': has_rrule,
                'has_recurrence_id': has_recurrence_id
            })
    
    except Exception as e:
        print(f"⚠️ Error in single-pass parsing: {e}")
    
    return result


def parse_all_events(cal_text, target_date, local_tz):
    """Parse ALL events (both recurring and non-recurring) for the target date.
    
    This function combines:
    1. Recurring events expanded from RRULE
    2. Non-recurring single events
    3. Modified occurrences (RECURRENCE-ID)
    
    Returns: (events_list, parsed_data) where parsed_data contains tz_cache,
    busystatus_map, and recurring_uids for callers that need them.
    """
    # Check parsed events cache first
    cal_mtime = _get_newest_cache_mtime()
    cached_events, cached_parsed_data = get_cached_parsed_events(target_date, cal_text)
    if cached_events is not None:
        # If parsed_data was cached in-memory, use it; otherwise rebuild
        if cached_parsed_data is not None:
            return cached_events, cached_parsed_data
        parsed_data = parse_ics_single_pass_optimized(cal_text, local_tz)
        # Store parsed_data back into in-memory cache for next hit
        _parsed_events_cache['parsed_data'] = parsed_data
        return cached_events, parsed_data
    
    print("🔄 Parsing events (not in cache)...")
    all_events = []
    
    # Use single-pass parser for better performance
    parsed_data = parse_ics_single_pass_optimized(cal_text, local_tz)
    tz_cache = parsed_data['tz_cache']
    busystatus_map = parsed_data['busystatus_map']
    recurring_uids = parsed_data['recurring_uids']
    
    # Get recurring events using the pre-computed caches
    recurring_events = parse_recurring_events_optimized(
        cal_text, target_date, local_tz, 
        parsed_data['tz_cache'],
        parsed_data['recurrence_map'],
        parsed_data['event_blocks']
    )
    all_events.extend(recurring_events)
    
    # Date boundaries for filtering
    day_start = local_tz.localize(datetime.combine(target_date, datetime.min.time()))
    day_end = day_start + timedelta(days=1)
    
    # Parse non-recurring events from single-pass data (avoids buggy ics library)
    for event_data in parsed_data['event_blocks']:
        if event_data['has_rrule'] and not event_data['has_recurrence_id']:
            continue  # Recurring masters already handled above
        
        event_block = event_data['block']
        uid = event_data['uid']
        
        # Extract fields using pre-compiled patterns
        summary_match = SUMMARY_PATTERN.search(event_block)
        dtstart_match = DTSTART_PATTERN.search(event_block)
        dtend_match = DTEND_PATTERN.search(event_block)
        transp_match = TRANSP_PATTERN.search(event_block)
        busystatus_match = BUSYSTATUS_PATTERN.search(event_block)
        
        if not (summary_match and dtstart_match):
            continue
        
        summary = summary_match.group(1).strip()
        dtstart_tz = dtstart_match.group(1)
        dtstart_str = dtstart_match.group(2)
        transparent = transp_match and transp_match.group(1).strip() == 'TRANSPARENT'
        busystatus = busystatus_match.group(1).strip() if busystatus_match else 'BUSY'
        
        # Check for all-day events (DATE format without time)
        is_all_day = 'DTSTART;VALUE=DATE:' in event_block
        
        try:
            start_naive = datetime.strptime(dtstart_str, '%Y%m%dT%H%M%S')
            if dtstart_tz:
                pytz_tz_name = MICROSOFT_TZ_MAP.get(dtstart_tz, dtstart_tz)
                source_tz = pytz.timezone(pytz_tz_name)
                event_start = source_tz.localize(start_naive).astimezone(local_tz)
            else:
                event_start = local_tz.localize(start_naive)
            
            if dtend_match:
                end_naive = datetime.strptime(dtend_match.group(2), '%Y%m%dT%H%M%S')
                dtend_tz = dtend_match.group(1)
                if dtend_tz:
                    pytz_tz_name = MICROSOFT_TZ_MAP.get(dtend_tz, dtend_tz)
                    end_source_tz = pytz.timezone(pytz_tz_name)
                    event_end = end_source_tz.localize(end_naive).astimezone(local_tz)
                elif dtstart_tz:
                    event_end = source_tz.localize(end_naive).astimezone(local_tz)
                else:
                    event_end = local_tz.localize(end_naive)
            else:
                event_end = event_start + timedelta(hours=1)
        except Exception as e:
            print(f"⚠️ Error parsing event '{summary}': {e}")
            continue
        
        # Filter to events that overlap with target date
        if event_start >= day_end or event_end <= day_start:
            continue
        
        if busystatus == 'TENTATIVE':
            summary = f"Tentative: {summary}"
        
        all_events.append({
            'name': summary,
            'start': event_start,
            'end': event_end,
            'transparent': transparent,
            'uid': uid,
            'all_day': is_all_day,
            'busystatus': busystatus
        })
    
    # Save to cache for next time (includes parsed_data for in-memory)
    save_parsed_events_cache(target_date, all_events, cal_mtime, parsed_data)
    
    return all_events, parsed_data


def parse_recurring_events_optimized(cal_text, target_date, local_tz, tz_cache, recurrence_map, event_blocks):
    """
    HIGH IMPACT OPTIMIZATION: Parse recurring events using pre-computed data.
    This version receives pre-computed caches instead of scanning the ICS again.
    """
    recurring_events = []
    
    try:
        for event_data in event_blocks:
            if not event_data['has_rrule'] or event_data['has_recurrence_id']:
                continue
            
            event_block = event_data['block']
            uid = event_data['uid']
            
            # Extract key fields using pre-compiled patterns
            summary_match = SUMMARY_PATTERN.search(event_block)
            rrule_match = RRULE_PATTERN.search(event_block)
            dtstart_match = DTSTART_PATTERN.search(event_block)
            dtend_match = DTEND_PATTERN.search(event_block)
            transp_match = TRANSP_PATTERN.search(event_block)
            busystatus_match = BUSYSTATUS_PATTERN.search(event_block)
            
            if not (summary_match and rrule_match and dtstart_match):
                continue
            
            summary = summary_match.group(1).strip()
            rrule_str = rrule_match.group(1).strip()
            dtstart_tz = dtstart_match.group(1) if dtstart_match.group(1) else None
            dtstart_str = dtstart_match.group(2)
            transparent = transp_match and transp_match.group(1).strip() == 'TRANSPARENT'
            busystatus = busystatus_match.group(1).strip() if busystatus_match else 'BUSY'
            
            # Prepend "Tentative: " for tentative events
            if busystatus == 'TENTATIVE':
                summary = f"Tentative: {summary}"
            
            # Parse start time
            start_naive = datetime.strptime(dtstart_str, '%Y%m%dT%H%M%S')
            
            # Localize to source timezone
            if dtstart_tz:
                pytz_tz_name = MICROSOFT_TZ_MAP.get(dtstart_tz, dtstart_tz)
                source_tz = pytz.timezone(pytz_tz_name)
            else:
                source_tz = local_tz
            
            # Parse end time if available
            duration = timedelta(hours=1)  # Default duration
            if dtend_match:
                dtend_str = dtend_match.group(2)
                end_naive = datetime.strptime(dtend_str, '%Y%m%dT%H%M%S')
                if dtstart_tz:
                    end_dt = source_tz.localize(end_naive).astimezone(local_tz)
                else:
                    end_dt = local_tz.localize(end_naive)
                start_dt = source_tz.localize(start_naive).astimezone(local_tz)
                duration = end_dt - start_dt
            
            # Extract EXDATE
            exdate_matches = EXDATE_PATTERN.findall(event_block)
            exdates = []
            for exdate_tz, exdate_str in exdate_matches:
                for date_str in exdate_str.split(','):
                    date_str = date_str.strip()
                    if not date_str:
                        continue
                    try:
                        if date_str.endswith('Z'):
                            exdate_dt = datetime.strptime(date_str, '%Y%m%dT%H%M%SZ')
                            exdate_dt = exdate_dt.replace(tzinfo=timezone.utc).astimezone(local_tz)
                        else:
                            exdate_naive = datetime.strptime(date_str, '%Y%m%dT%H%M%S')
                            if exdate_tz:
                                pytz_tz_name = MICROSOFT_TZ_MAP.get(exdate_tz, exdate_tz)
                                source_tz_ex = pytz.timezone(pytz_tz_name)
                                exdate_dt = source_tz_ex.localize(exdate_naive).astimezone(local_tz)
                            elif dtstart_tz:
                                exdate_dt = source_tz.localize(exdate_naive).astimezone(local_tz)
                            else:
                                exdate_dt = local_tz.localize(exdate_naive)
                        exdates.append(exdate_dt)
                    except Exception:
                        pass
            
            # Parse RRULE and expand
            try:
                start_naive = datetime.strptime(dtstart_str, '%Y%m%dT%H%M%S')
                rr = rrulestr(f"RRULE:{rrule_str}", dtstart=start_naive, ignoretz=True)
                
                # Only expand recurring events for the target date (for performance)
                day_start = datetime.combine(target_date, datetime.min.time())
                day_end = day_start + timedelta(days=1)
                
                for occurrence in rr.between(day_start, day_end, inc=True):
                    if dtstart_tz:
                        occurrence_dt = source_tz.localize(occurrence).astimezone(local_tz)
                    else:
                        occurrence_dt = local_tz.localize(occurrence)
                    
                    # Check if cancelled or modified
                    is_cancelled = any(abs((occurrence_dt - exdate).total_seconds()) < 60 for exdate in exdates)
                    is_modified = False
                    if uid in recurrence_map:
                        is_modified = any(abs((occurrence_dt - recur_dt).total_seconds()) < 60 
                                        for recur_dt in recurrence_map[uid])
                    
                    if is_cancelled or is_modified:
                        continue
                    
                    end_dt = occurrence_dt + duration
                    recurring_events.append({
                        'name': summary,
                        'start': occurrence_dt,
                        'end': end_dt,
                        'transparent': transparent,
                        'uid': uid,
                        'busystatus': busystatus
                    })
            except Exception as e:
                print(f"⚠️ Error parsing RRULE for '{summary}': {e}")
    
    except Exception as e:
        print(f"⚠️ Error parsing recurring events: {e}")
    
    return recurring_events


MICROSOFT_TZ_MAP = {
    # North America
    'Pacific Standard Time': 'America/Los_Angeles',
    'Central Standard Time': 'America/Chicago',
    'Eastern Standard Time': 'America/New_York',
    'Mountain Standard Time': 'America/Denver',
    'Atlantic Standard Time': 'America/Halifax',
    'Newfoundland Standard Time': 'America/St_Johns',
    'Alaskan Standard Time': 'America/Anchorage',
    'Hawaiian Standard Time': 'Pacific/Honolulu',
    'US Mountain Standard Time': 'America/Phoenix',
    'US Eastern Standard Time': 'America/New_York',
    'US Central Standard Time': 'America/Chicago',
    'Canada Central Standard Time': 'America/Regina',
    'Central Standard Time (Mexico)': 'America/Mexico_City',
    'Mountain Standard Time (Mexico)': 'America/Chihuahua',
    'Pacific Standard Time (Mexico)': 'America/Tijuana',
    # South America
    'SA Pacific Standard Time': 'America/Bogota',
    'SA Eastern Standard Time': 'America/Cayenne',
    'SA Western Standard Time': 'America/La_Paz',
    'E. South America Standard Time': 'America/Sao_Paulo',
    'Argentina Standard Time': 'America/Buenos_Aires',
    'Venezuela Standard Time': 'America/Caracas',
    # Europe
    'GMT Standard Time': 'Europe/London',
    'W. Europe Standard Time': 'Europe/Berlin',
    'Romance Standard Time': 'Europe/Paris',
    'Central Europe Standard Time': 'Europe/Warsaw',
    'Central European Standard Time': 'Europe/Budapest',
    'E. Europe Standard Time': 'Europe/Chisinau',
    'FLE Standard Time': 'Europe/Kiev',
    'GTB Standard Time': 'Europe/Bucharest',
    'Russian Standard Time': 'Europe/Moscow',
    'Turkey Standard Time': 'Europe/Istanbul',
    'Greenwich Standard Time': 'Atlantic/Reykjavik',
    # Middle East / Africa
    'Israel Standard Time': 'Asia/Jerusalem',
    'Arabic Standard Time': 'Asia/Baghdad',
    'Arab Standard Time': 'Asia/Riyadh',
    'Iran Standard Time': 'Asia/Tehran',
    'South Africa Standard Time': 'Africa/Johannesburg',
    'Egypt Standard Time': 'Africa/Cairo',
    # Asia
    'India Standard Time': 'Asia/Kolkata',
    'Sri Lanka Standard Time': 'Asia/Colombo',
    'Nepal Standard Time': 'Asia/Kathmandu',
    'Central Asia Standard Time': 'Asia/Almaty',
    'Bangladesh Standard Time': 'Asia/Dhaka',
    'SE Asia Standard Time': 'Asia/Bangkok',
    'China Standard Time': 'Asia/Shanghai',
    'Singapore Standard Time': 'Asia/Singapore',
    'Taipei Standard Time': 'Asia/Taipei',
    'Korea Standard Time': 'Asia/Seoul',
    'Tokyo Standard Time': 'Asia/Tokyo',
    # Australia / Pacific
    'AUS Eastern Standard Time': 'Australia/Sydney',
    'AUS Central Standard Time': 'Australia/Darwin',
    'Cen. Australia Standard Time': 'Australia/Adelaide',
    'W. Australia Standard Time': 'Australia/Perth',
    'New Zealand Standard Time': 'Pacific/Auckland',
    'Fiji Standard Time': 'Pacific/Fiji',
    'Samoa Standard Time': 'Pacific/Apia',
    # Special
    'UTC': 'UTC',
    'Customized Time Zone': TIMEZONE,
}


def convert_event_time(event_arrow, uid, tz_cache, target_tz):
    """Convert event time to target timezone.
    
    The ics library incorrectly parses times with TZID as UTC.
    When we have a TZID in the cache, we need to treat the time components
    as being in the source timezone, not UTC.
    """
    tz_name = tz_cache.get(uid)
    if tz_name:
        pytz_name = MICROSOFT_TZ_MAP.get(tz_name, tz_name)
        try:
            source_tz = pytz.timezone(pytz_name)
            # The ics library gives us a UTC time, but it should be interpreted
            # as the source timezone time. Extract the naive datetime.
            naive_dt = event_arrow.naive
            # Localize it as the source timezone
            localized = source_tz.localize(naive_dt)
            # Convert to target timezone if different
            if localized.tzinfo == target_tz:
                return localized
            return localized.astimezone(target_tz)
        except Exception as e:
            print(f"⚠️ Error converting timezone for {uid}: {e}")
            pass
    
    # Fallback: use arrow's built-in conversion
    try:
        return event_arrow.to(target_tz.zone).datetime
    except Exception:
        return event_arrow.datetime


def normalize_meeting_time(dt):
    """Normalize meeting times by rounding 5-minute gaps to the nearest half-hour.
    
    For example:
    - 10:05 AM -> 10:00 AM
    - 11:05 AM -> 11:00 AM  
    - 1:35 PM -> 1:30 PM
    
    This accounts for Microsoft's default 5-minute gap between meetings.
    """
    minute = dt.minute
    
    # If minute is 5 (xx:05), round down to xx:00
    if minute == 5:
        return dt.replace(minute=0, second=0, microsecond=0)
    # If minute is 35 (xx:35), round down to xx:30
    elif minute == 35:
        return dt.replace(minute=30, second=0, microsecond=0)
    
    # Otherwise return as-is
    return dt


def get_status():
    """Return (status, meeting, return_time, location) based on calendar."""
    now = datetime.now().astimezone()
    
    try:
        cal_text = get_cached_calendar()
    except Exception as e:
        print(f"Error fetching calendar: {e}")
        return "error", None, None, "remote"

    location = get_location()

    # Check OOO override first (set via /api/ooo/set)
    ooo_override = _get_ooo_override()
    if ooo_override:
        return "ooo", "Out of office", ooo_override, location

    local_tz = LOCAL_TZ
    
    # Use the optimized parse_all_events (builds tz_cache internally via single-pass)
    all_events_parsed, _ = parse_all_events(cal_text, now.date(), local_tz)
    
    regular_events = []
    all_day_events = []
    
    for event in all_events_parsed:
        # Skip transparent or tentative events (but keep OOF — checked for OOO later)
        is_oof = event.get('busystatus', '').upper() == 'OOF'
        if not is_oof and (event.get('transparent') or event.get('busystatus') == 'TENTATIVE'):
            continue
        
        e = SimpleEvent(event['name'], event.get('uid', ''), event['end'], event.get('all_day', False), event.get('busystatus', 'BUSY'))
        start = normalize_meeting_time(event['start']) if not event.get('all_day') else event['start']
        end = event['end']
        
        if event.get('all_day'):
            all_day_events.append((e, start, end))
        else:
            regular_events.append((e, start, end))
    
    # Check for OOO status
    all_events = all_day_events + regular_events
    today = now.date()
    ooo_event = None
    
    for (e, start, end) in all_events:
        if is_user_ooo_event(e):
            if e.all_day:
                start_date = start.date()
                end_date_exclusive = (end + timedelta(microseconds=1)).date()
                if start_date <= today < end_date_exclusive:
                    ooo_event = e
                    break
            else:
                if start <= now < end:
                    ooo_event = e
                    break

    if ooo_event:
        # Calculate smart return time
        ooo_end = ooo_event.end
        eod = now.replace(hour=BUSINESS_END_HOUR, minute=0, second=0, microsecond=0)
        
        if ooo_event.all_day:
            # All-day/vacation OOO → "ooo" status (purple, airplane)
            return "ooo", ooo_event.name, ooo_end, location
        elif ooo_end <= eod:
            # Timed OOO ending within business hours → "away" status (yellow)
            return "away", ooo_event.name, ooo_end, location
        else:
            # Timed OOO ending after business hours — check tomorrow for chained OOO
            back_time = _find_first_non_ooo_time(cal_text, now, local_tz)
            return "away", ooo_event.name, back_time, location

    # Check regular events
    regular_events.sort(key=lambda x: x[1])
    
    current_events = [
        (e, start, end) for (e, start, end) in regular_events 
        if start <= now < end
    ]
    
    next_meeting = next((
        (e, start, end) for (e, start, end) in regular_events 
        if start > now
    ), None)

    if current_events:
        # Find next free time: walk forward through sorted events to find the first gap
        current_ends = [end for (_, _, end) in current_events]
        free_at = max(current_ends)
        
        # Check if back-to-back meetings extend the busy period
        for (e, start, end) in regular_events:
            if start <= free_at and end > free_at:
                free_at = end
        
        # If free_at is past business hours, find actual next free slot in future days
        eod = now.replace(hour=BUSINESS_END_HOUR, minute=0, second=0, microsecond=0)
        if free_at >= eod:
            free_at = _find_next_free_slot(cal_text, now, local_tz)
        
        return "busy", None, free_at, location
    else:
        meeting_info = (next_meeting[0].name, next_meeting[1]) if next_meeting else None
        return "free", meeting_info, None, location


def is_business_hours():
    """Check if current time is within business hours (Mon-Fri, 8am-6pm)."""
    now = datetime.now()
    if now.weekday() > 4:
        return False
    if now.hour < BUSINESS_START_HOUR or now.hour >= BUSINESS_END_HOUR:
        return False
    return True


def _next_business_start(from_dt):
    """Get the next business day start (8am) from the given datetime."""
    dt = from_dt
    while True:
        dt = dt + timedelta(days=1)
        if dt.weekday() <= 4:  # Mon-Fri
            return dt.replace(hour=BUSINESS_START_HOUR, minute=0, second=0, microsecond=0)


def _find_first_non_ooo_time(cal_text, from_dt, local_tz):
    """Check next business morning for chained OOO blocks. Returns actual back time."""
    next_biz = _next_business_start(from_dt)
    # Check up to 7 days ahead
    for day_offset in range(7):
        check_date = (next_biz + timedelta(days=day_offset)).date()
        day_start = datetime(check_date.year, check_date.month, check_date.day,
                           tzinfo=local_tz)
        biz_start = day_start.replace(hour=BUSINESS_START_HOUR)
        biz_end = day_start.replace(hour=BUSINESS_END_HOUR)

        if check_date.weekday() > 4:
            continue

        events_for_day, _ = parse_all_events(cal_text, check_date, local_tz)
        # Get OOF events sorted by start
        ooo_blocks = []
        for ev in events_for_day:
            bs = ev.get('busystatus', '').upper()
            name = ev.get('name') or ''
            is_oof = bs == 'OOF' or _is_ooo_title(name)
            if not is_oof or ev.get('all_day'):
                continue
            s = ev['start']
            e = ev['end']
            if e > biz_start:
                ooo_blocks.append((max(s, biz_start), e))

        if not ooo_blocks:
            return biz_start

        # Walk through OOO blocks to find first gap at/after business start
        ooo_blocks.sort()
        free_at = biz_start
        for (s, e) in ooo_blocks:
            if s <= free_at:
                free_at = max(free_at, e)
            else:
                break

        if free_at < biz_end:
            return free_at

    return _next_business_start(from_dt)


def _find_next_free_slot(cal_text, from_dt, local_tz):
    """Find the next free time slot, looking into future days if needed.
    
    Walks forward from from_dt through today and up to 5 future business days,
    parsing each day's events to find a gap during business hours.
    """
    check_date = from_dt.date()
    
    for _ in range(6):  # Check up to 6 days ahead
        day_start = local_tz.localize(datetime.combine(check_date, datetime.min.time()))
        biz_start = day_start.replace(hour=BUSINESS_START_HOUR)
        biz_end = day_start.replace(hour=BUSINESS_END_HOUR)
        
        # Skip weekends
        if check_date.weekday() > 4:
            check_date += timedelta(days=1)
            continue
        
        # Parse events for this day
        events_for_day, _ = parse_all_events(cal_text, check_date, local_tz)
        
        # Get non-transparent, non-tentative events sorted by start
        busy_blocks = []
        for ev in events_for_day:
            if ev.get('transparent') or ev.get('busystatus') == 'TENTATIVE' or ev.get('all_day'):
                continue
            s = normalize_meeting_time(ev['start'])
            e = ev['end']
            # Clip to business hours
            s = max(s, biz_start)
            e = min(e, biz_end)
            if s < e:
                busy_blocks.append((s, e))
        
        busy_blocks.sort(key=lambda x: x[0])
        
        # Find the first free slot — start scanning from the later of biz_start or from_dt
        scan_from = biz_start
        if check_date == from_dt.date():
            scan_from = max(from_dt, biz_start)
        
        # If we're past business hours today, skip to next day
        if scan_from >= biz_end:
            check_date += timedelta(days=1)
            continue
        
        # Walk through busy blocks to find a gap
        cursor = scan_from
        for block_start, block_end in busy_blocks:
            if block_start > cursor:
                return cursor  # Found a gap
            cursor = max(cursor, block_end)
        
        # Check if there's free time after all meetings but before EOD
        if cursor < biz_end:
            return cursor
        
        check_date += timedelta(days=1)
    
    # Fallback: couldn't find free time in 6 days
    return _next_business_start(from_dt)


# --- Security: authentication, CSRF and response headers ---
#
# Deny by default. A request is one of:
#   local  — from loopback (or TRUSTED_NETWORKS) with no proxy headers: the kiosk, SSH, LAN admin
#   remote — via cloudflared; must carry a valid Cloudflare Access JWT
# Roles: owner (everything), user (send messages), viewer (display + its polling APIs).

VIEWER_ENDPOINTS = {'static', 'index', 'api_status', 'api_version', 'api_message_get', 'api_easter_eggs', 'api_qr'}
USER_ENDPOINTS = {'message_ui', 'api_message_post'}
PROXY_HEADERS = ('Cf-Connecting-IP', 'X-Forwarded-For', 'X-Real-IP', 'Forwarded', 'X-Forwarded-Host')
EMAIL_RE = re.compile(r'\A[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]{1,190}\.[A-Za-z]{2,24}\Z')
PUBLIC_DISPLAY = os.getenv('PUBLIC_DISPLAY', 'false').strip().lower() in ('1', 'true', 'yes')
TRUSTED_NETWORKS = [ipaddress.ip_network(n.strip(), strict=False)
                    for n in os.getenv('TRUSTED_NETWORKS', '').split(',') if n.strip()]

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
    "frame-ancestors 'none'; form-action 'self'"
)

_jwk_client = None


def _is_trusted_host_header():
    """Local trust also requires an IP/localhost Host header, which blocks DNS-rebinding attacks."""
    hostname = (urlsplit(f"//{request.host}").hostname or '').lower()
    if hostname == 'localhost' or hostname.endswith('.local'):
        return True
    try:
        ipaddress.ip_address(hostname)
        return True
    except ValueError:
        return False


def _is_local_request():
    """True for direct connections from loopback/trusted networks that did not come through a proxy."""
    if any(h in request.headers for h in PROXY_HEADERS):
        return False
    try:
        addr = ipaddress.ip_address(request.remote_addr or '')
    except ValueError:
        return False
    if getattr(addr, 'ipv4_mapped', None):
        addr = addr.ipv4_mapped
    if not (addr.is_loopback or any(addr in net for net in TRUSTED_NETWORKS)):
        return False
    return _is_trusted_host_header()


def _verify_access_jwt():
    """Validate the Cloudflare Access JWT (header or cookie). Returns claims, or None."""
    global _jwk_client
    if not (CF_ACCESS_TEAM_DOMAIN and CF_ACCESS_AUD):
        return None
    token = request.headers.get('Cf-Access-Jwt-Assertion') or request.cookies.get('CF_Authorization')
    if not token:
        return None
    try:
        if _jwk_client is None:
            _jwk_client = jwt.PyJWKClient(f"https://{CF_ACCESS_TEAM_DOMAIN}/cdn-cgi/access/certs",
                                          cache_keys=True, lifespan=3600, timeout=5)
        key = _jwk_client.get_signing_key_from_jwt(token).key
        return jwt.decode(
            token, key, algorithms=['RS256'],
            audience=CF_ACCESS_AUD, issuer=f"https://{CF_ACCESS_TEAM_DOMAIN}",
            options={'require': ['exp', 'iat', 'aud', 'iss']},
        )
    except Exception as e:
        print(f"🔒 Rejected Cloudflare Access token: {type(e).__name__}")
        return None


def _identity():
    """Resolve (once per request) who is calling and what they may do."""
    if 'identity' in g:
        return g.identity
    ident = {'local': _is_local_request(), 'authenticated': False, 'email': None, 'service_token': None}
    if ident['local']:
        ident['authenticated'] = True
    else:
        claims = _verify_access_jwt()
        if claims:
            ident['authenticated'] = True
            email = claims.get('email')
            if isinstance(email, str) and EMAIL_RE.match(email):
                ident['email'] = email.lower()
            common_name = claims.get('common_name')
            if not ident['email'] and isinstance(common_name, str) and common_name:
                ident['service_token'] = common_name
    ident['owner'] = (ident['local']
                      or (ident['email'] is not None and ident['email'] in OWNER_EMAILS)
                      or (ident['service_token'] is not None and ident['service_token'] in OWNER_SERVICE_TOKENS))
    g.identity = ident
    return ident


def _is_cross_site_request():
    """Browser-originated requests from another site (CSRF). Non-browser clients send neither header."""
    fetch_site = request.headers.get('Sec-Fetch-Site')
    if fetch_site:
        return fetch_site not in ('same-origin', 'none')
    origin = request.headers.get('Origin')
    if not origin:
        return False
    return (urlsplit(origin).netloc or '').lower() != request.host.lower()


@app.before_request
def _enforce_access():
    if request.method not in ('GET', 'HEAD', 'OPTIONS') and _is_cross_site_request():
        print(f"🔒 Blocked cross-site {request.method} {request.path}")
        abort(403)
    if request.endpoint is None:
        return None  # Let Flask return 404/405
    ident = _identity()
    if ident['owner']:
        return None
    if request.endpoint in VIEWER_ENDPOINTS and (PUBLIC_DISPLAY or ident['authenticated']):
        return None
    if request.endpoint in USER_ENDPOINTS and ident['email']:
        return None
    abort(403)


@app.after_request
def _security_headers(resp):
    resp.headers.setdefault('Content-Security-Policy', CONTENT_SECURITY_POLICY)
    resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
    resp.headers.setdefault('X-Frame-Options', 'DENY')
    resp.headers.setdefault('Referrer-Policy', 'no-referrer')
    if request.endpoint != 'static':
        resp.headers.setdefault('Cache-Control', 'no-store')
    return resp


def _display_context():
    """Template variables for personalizing the display (all optional)."""
    if QR_FILE:
        qr_url = url_for('static', filename=QR_FILE)
    elif PUBLIC_URL:
        qr_url = url_for('api_qr')
    else:
        qr_url = None
    return {
        'user_name': USER_NAME,
        'first_name': USER_NAME.split()[0] if USER_NAME else '',
        'room_number': ROOM_NUMBER,
        'logo_url': url_for('static', filename=LOGO_FILE) if LOGO_FILE else None,
        'qr_url': qr_url,
        'easter_egg_message': EASTER_EGG_MESSAGE,
    }


# --- Flask Routes ---

@app.route('/api/refresh', methods=['POST'])
def api_refresh():
    """Force refresh all ICS calendar caches."""
    global _parsed_events_cache, _refresh_version
    try:
        # Re-download all calendars
        for i, url in enumerate(ALL_ICS_URLS):
            _download_calendar(i, url)
        
        # Clear parsed caches
        _parsed_events_cache = {'date': None, 'events': None, 'parsed_data': None, 'timestamp': 0, 'cal_mtime': 0}
        if os.path.exists(PARSED_CACHE_FILE):
            os.remove(PARSED_CACHE_FILE)
        
        # Bump version so displays reload
        _refresh_version += 1
        location = get_location()
        
        print(f"🔄 Force refresh: caches cleared, version={_refresh_version}")
        return jsonify({'ok': True, 'location': location, 'version': _refresh_version})
    except Exception as e:
        print(f"⚠️ Force refresh failed: {e}")
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/version')
def api_version():
    """Lightweight endpoint for displays to poll for refresh signals."""
    dev_active = time.time() < _dev_mode_until
    return jsonify({'v': _refresh_version, 'dev_mode': dev_active})


@app.route('/api/dev-mode', methods=['POST'])
def api_dev_mode():
    """Enable dev mode — disables sleep screen for 4 hours."""
    global _dev_mode_until, _refresh_version
    _dev_mode_until = time.time() + (4 * 3600)
    _refresh_version += 1
    print(f"🛠️ Dev mode ON for 4 hours (version={_refresh_version})")
    resp = jsonify({'ok': True, 'dev_mode': True, 'expires_in': 4 * 3600, 'version': _refresh_version})
    return resp


@app.route('/api/dev-mode/off', methods=['POST'])
def api_dev_mode_off():
    """Disable dev mode."""
    global _dev_mode_until, _refresh_version
    _dev_mode_until = 0
    _refresh_version += 1
    print(f"🛠️ Dev mode OFF (version={_refresh_version})")
    resp = jsonify({'ok': True, 'dev_mode': False, 'version': _refresh_version})
    return resp


@app.route('/api/location', methods=['GET'])
def api_location_get():
    """Get current location and schedule info."""
    now = datetime.now(LOCAL_TZ)
    today_key = now.date().isoformat()
    overrides = _load_location_overrides()
    has_override = today_key in overrides
    
    return jsonify({
        'location': get_location(),
        'source': 'override' if has_override else 'schedule',
        'override_value': overrides.get(today_key),
        'schedule_default': 'office' if now.weekday() in OFFICE_DAYS else 'remote',
        'office_days': [WEEKDAY_NAMES[d] for d in sorted(OFFICE_DAYS)],
        'office_hours': f'{OFFICE_LOCATION_START}:00-{OFFICE_LOCATION_END}:00',
        'date': today_key,
    })


@app.route('/api/location/set', methods=['POST'])
def api_location_set():
    """Set location override for a date. 
    
    Body JSON: {"location": "office"|"remote", "date": "2026-04-03"} 
    If date is omitted, defaults to today.
    To clear an override, send {"location": "clear", "date": "..."}.
    """
    global _refresh_version
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    
    location = data.get('location', request.args.get('location', ''))
    date_str = data.get('date', request.args.get('date', ''))
    
    if not location:
        return jsonify({'error': 'location required (office, remote, or clear)'}), 400
    
    target_date = date_str if date_str else datetime.now(LOCAL_TZ).date().isoformat()
    
    overrides = _load_location_overrides()
    
    if location == 'clear':
        overrides.pop(target_date, None)
        action = 'cleared'
    elif location in ('office', 'remote'):
        overrides[target_date] = location
        action = 'set'
    else:
        return jsonify({'error': 'location must be office, remote, or clear'}), 400
    
    _save_location_overrides(overrides)
    _refresh_version += 1
    
    print(f"📍 Location {action}: {target_date} = {location} (version={_refresh_version})")
    resp = jsonify({
        'ok': True,
        'action': action,
        'date': target_date,
        'location': location if location != 'clear' else get_location(),
        'version': _refresh_version,
    })
    return resp


@app.route('/api/ooo', methods=['GET'])
def api_ooo_get():
    """Get current OOO override status."""
    override = _get_ooo_override()
    if override:
        return jsonify({
            'active': True,
            'return_date': override.isoformat(),
            'return_date_human': override.strftime('%A, %B %d %Y'),
        })
    return jsonify({'active': False})


@app.route('/api/ooo/set', methods=['POST'])
def api_ooo_set():
    """Set or clear OOO override. POST with return_date (ISO format) or action=clear."""
    global _refresh_version
    data = {}
    if request.form.get('return_date') or request.form.get('action'):
        data = request.form.to_dict()
    else:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            data = {}

    action = data.get('action', '')
    if action == 'clear':
        _set_ooo_override(None)
        _refresh_version += 1
        resp = jsonify({'ok': True, 'action': 'cleared', 'version': _refresh_version})
        return resp

    return_date = data.get('return_date', '')
    if not return_date:
        return jsonify({'ok': False, 'error': 'return_date is required (ISO format, e.g. 2026-04-07)'}), 400

    if _set_ooo_override(return_date):
        _refresh_version += 1
        override = _get_ooo_override()
        print(f"✈️ OOO set until {return_date} (version={_refresh_version})")
        resp = jsonify({
            'ok': True,
            'return_date': override.isoformat() if override else return_date,
            'version': _refresh_version,
        })
        return resp
    return jsonify({'ok': False, 'error': 'Invalid return_date format'}), 400


def _sanitize_message(text):
    """Sanitize message text: strip, length limit, HTML-escape, pattern check."""
    if not text or not isinstance(text, str):
        return None, 'Message text is required'
    text = text.strip()
    if len(text) == 0:
        return None, 'Message cannot be empty'
    if len(text) > MESSAGE_MAX_LEN:
        return None, f'Message too long ({len(text)} chars, max {MESSAGE_MAX_LEN})'
    if not MESSAGE_PATTERN.match(text):
        return None, 'Message contains disallowed characters'
    # HTML-escape to prevent XSS (the display uses innerHTML for scrolling)
    text = (text.replace('&', '&amp;').replace('<', '&lt;')
                .replace('>', '&gt;').replace('"', '&quot;').replace("'", '&#x27;'))
    return text, None


def _get_sender():
    """Sender identity from the verified Cloudflare Access JWT (never from spoofable headers)."""
    ident = _identity()
    if ident['email']:
        return ident['email']
    if ident['local']:
        return 'local'
    return 'anonymous'


def _sender_display_name(sender):
    """Public, display-safe name for a sender: 'jane.doe@example.com' -> 'jane doe'."""
    if not sender or '@' not in sender:
        return None
    name = re.sub(r'[._+\-]+', ' ', sender.split('@', 1)[0])
    name = re.sub(r'[^\w ]', '', name).strip()
    return name[:40] or None


def _log_message(text, sender):
    """Append message to persistent history log."""
    try:
        history = []
        if os.path.exists(MESSAGE_LOG_FILE):
            try:
                with open(MESSAGE_LOG_FILE, 'r') as f:
                    history = json.load(f)
            except (json.JSONDecodeError, TypeError):
                history = []
        history.append({
            'text': text,
            'sender': sender,
            'timestamp': datetime.now(LOCAL_TZ).isoformat(),
        })
        # Keep last 200 messages
        history = history[-200:]
        with open(MESSAGE_LOG_FILE, 'w') as f:
            json.dump(history, f)
    except Exception as e:
        print(f"⚠️ Error logging message: {e}")


@app.route('/api/message', methods=['GET'])
def api_message_get():
    """Get active message (if any). Polled by the display; exposes a display name, not the email."""
    if _active_message['text'] and time.time() < _active_message['expires']:
        return jsonify({
            'active': True,
            'text': _active_message['text'],
            'sender_name': _sender_display_name(_active_message['sender']),
            'expires_in': int(_active_message['expires'] - time.time()),
        })
    return jsonify({'active': False})


@app.route('/api/message', methods=['POST'])
def api_message_post():
    """Submit a message to display. Requires a Cloudflare Access identity (or a local request)."""
    global _active_message

    # Accept both JSON and form-encoded data
    raw_text = ''
    if request.form.get('text'):
        raw_text = request.form.get('text')
    else:
        data = request.get_json(silent=True) or {}
        raw_text = data.get('text', '') if isinstance(data, dict) else ''

    text, error = _sanitize_message(raw_text)
    if error:
        return jsonify({'ok': False, 'error': error}), 400

    sender = _get_sender()

    _active_message = {
        'text': text,
        'sender': sender,
        'expires': time.time() + MESSAGE_TTL,
    }
    _log_message(text, sender)

    print(f"💬 Message from {sender}: {text[:50]}...")
    resp = jsonify({'ok': True, 'text': text, 'sender': sender, 'expires_in': MESSAGE_TTL})
    return resp


@app.route('/msg', methods=['GET', 'POST'])
def message_ui():
    """Web UI for sending messages to the desk display. POST also accepted here."""
    if request.method == 'POST':
        return api_message_post()
    sender = _get_sender()
    return render_template('message.html', sender=sender, max_len=MESSAGE_MAX_LEN,
                           first_name=_display_context()['first_name'])


@app.route('/api/message/history', methods=['GET', 'DELETE'])
def api_message_history():
    """Get or clear message history."""
    if request.method == 'DELETE':
        try:
            with open(MESSAGE_LOG_FILE, 'w') as f:
                json.dump([], f)
        except Exception:
            pass
        return jsonify({'ok': True})

    if os.path.exists(MESSAGE_LOG_FILE):
        try:
            with open(MESSAGE_LOG_FILE, 'r') as f:
                history = json.load(f)
            return jsonify({'messages': list(reversed(history)), 'total': len(history)})
        except (json.JSONDecodeError, TypeError):
            pass
    return jsonify({'messages': [], 'total': 0})


@app.route('/admin/messages')
def test_messages():
    """Message history viewer."""
    return render_template('test_messages.html')


@app.route('/api/easter-eggs')
def api_easter_eggs():
    """List available easter egg background images."""
    egg_dir = os.path.join(app.static_folder, 'easter-eggs')
    images = []
    if os.path.isdir(egg_dir):
        images = sorted(f for f in os.listdir(egg_dir)
                       if f.lower().endswith(('.png', '.jpg', '.jpeg', '.webp')))
    return jsonify({'images': images})


@app.route('/api/health')
def api_health():
    """Calendar and system health status for admin dashboard."""
    health = _get_calendar_health()
    health['threshold_hours'] = CALENDAR_STALE_THRESHOLD // 3600
    health['num_calendars'] = len(ALL_ICS_URLS)
    health['dev_mode'] = time.time() < _dev_mode_until
    health['last_fetch_error'] = _last_fetch_error
    return jsonify(health)


@app.route('/api/debug')
def api_debug():
    """Debug endpoint: shows full status decision logic with all events and filtering."""
    now = datetime.now().astimezone()
    local_tz = LOCAL_TZ
    
    try:
        cal_text = get_cached_calendar()
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    
    all_events_parsed, parsed_data = parse_all_events(cal_text, now.date(), local_tz)
    
    # Build full event list with filtering info
    debug_events = []
    for event in all_events_parsed:
        start = event['start']
        end = event['end']
        is_transparent = event.get('transparent', False)
        is_tentative = event.get('busystatus') == 'TENTATIVE'
        is_all_day = event.get('all_day', False)
        is_current = not is_all_day and start <= now < end
        is_future = not is_all_day and start > now
        skipped = is_transparent or is_tentative
        
        debug_events.append({
            'name': event['name'],
            'start': start.isoformat(),
            'end': end.isoformat(),
            'busystatus': event.get('busystatus', 'BUSY'),
            'transparent': is_transparent,
            'all_day': is_all_day,
            'uid': event.get('uid', '')[:40],
            'is_current': is_current,
            'is_future': is_future,
            'skipped_reason': 'transparent' if is_transparent else ('tentative' if is_tentative else None),
        })
    
    debug_events.sort(key=lambda x: x['start'])
    
    # Get the actual status result
    status, meeting, return_time, location = get_status()
    
    return jsonify({
        'now': now.isoformat(),
        'status_result': {
            'status': status,
            'meeting': str(meeting),
            'return_time': return_time.isoformat() if return_time else None,
            'location': location,
        },
        'total_events': len(debug_events),
        'active_events': [e for e in debug_events if e['is_current'] and not e['skipped_reason']],
        'all_events': debug_events,
    })

@app.route('/')
def index():
    """Main display page."""
    return render_template('display.html', **_display_context())


_qr_svg_cache = None


@app.route('/qr.svg')
def api_qr():
    """QR code pointing at PUBLIC_URL/msg, shown on the display while a message is active."""
    global _qr_svg_cache
    if not PUBLIC_URL:
        abort(404)
    if _qr_svg_cache is None:
        import io
        import segno
        buf = io.BytesIO()
        segno.make(f"{PUBLIC_URL}/msg", error='m').save(buf, kind='svg', scale=10, border=2)
        _qr_svg_cache = buf.getvalue()
    return Response(_qr_svg_cache, mimetype='image/svg+xml')


@app.route('/api/status')
def api_status():
    """API endpoint that returns current status as JSON."""
    # Check business hours — but still show OOO if active
    if not is_business_hours():
        # Check if OOO override or calendar OOO is active
        ooo_override = _get_ooo_override()
        if ooo_override:
            return jsonify({
                'business_hours': False,
                'status': 'ooo',
                'location': get_location(),
                'return_time': ooo_override.isoformat(),
                'timestamp': datetime.now().isoformat()
            })
        # Quick calendar OOO check
        try:
            cal_text = get_cached_calendar()
            now = datetime.now().astimezone()
            all_events_parsed, _ = parse_all_events(cal_text, now.date(), LOCAL_TZ)
            for event in all_events_parsed:
                bs = event.get('busystatus', '').upper()
                is_oof = bs == 'OOF' or _is_ooo_title(event.get('name'))
                if not is_oof:
                    continue
                start = event['start']
                end = event['end']
                if event.get('all_day'):
                    if start.date() <= now.date() < (end + timedelta(microseconds=1)).date():
                        return jsonify({
                            'business_hours': False,
                            'status': 'ooo',
                            'location': get_location(),
                            'return_time': end.isoformat(),
                            'timestamp': datetime.now().isoformat()
                        })
                elif start <= now < end:
                    eod = now.replace(hour=BUSINESS_END_HOUR, minute=0, second=0, microsecond=0)
                    return_time = end if end <= eod else _find_first_non_ooo_time(cal_text, now, LOCAL_TZ)
                    return jsonify({
                        'business_hours': False,
                        'status': 'away',
                        'location': get_location(),
                        'return_time': return_time.isoformat(),
                        'timestamp': datetime.now().isoformat()
                    })
        except Exception:
            pass
        return jsonify({
            'business_hours': False,
            'status': 'offline'
        })
    
    try:
        status, meeting, return_time, location = get_status()
        
        response = {
            'business_hours': True,
            'status': status,
            'location': location,
            'timestamp': datetime.now().isoformat()
        }
        
        # Next meeting start time only — titles are never exposed (privacy)
        if isinstance(meeting, tuple) and len(meeting) == 2 and meeting[1]:
            response['meeting'] = {'start': meeting[1].isoformat()}
        
        # Add return/free time
        if return_time:
            response['return_time'] = return_time.isoformat()
        
        # Add calendar health
        health = _get_calendar_health()
        response['calendar_stale'] = health['stale']
        
        return jsonify(response)
    
    except Exception as e:
        print(f"⚠️ Error in /api/status: {type(e).__name__}: {e}")
        return jsonify({
            'business_hours': True,
            'status': 'error',
        }), 500


# --- Test Endpoints ---

@app.route('/admin')
def test_menu():
    """Admin hub page with links to all debug tools and mock statuses."""
    return render_template('test_menu.html')


@app.route('/test')
@app.route('/test/<path:subpath>')
def test_redirect(subpath=''):
    """Redirect old /test URLs to /admin."""
    from flask import redirect
    return redirect(f'/admin/{subpath}' if subpath else '/admin', code=301)


@app.route('/admin/events')
def test_events():
    """Show all calendar events for a specific date (defaults to today)."""
    # Get date parameter or use today
    date_str = request.args.get('date')
    if date_str:
        try:
            target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            return "Invalid date format. Use YYYY-MM-DD. Example: ?date=2025-10-30", 400
    else:
        target_date = datetime.now().date()
    
    # Get calendar data
    cal_text = get_cached_calendar()
    if not cal_text:
        return "Failed to fetch calendar", 500
    
    # Parse calendar (single-pass builds tz_cache, busystatus_map, recurring_uids internally)
    local_tz = LOCAL_TZ
    
    # Parse ALL events (both recurring and non-recurring)
    parsed_events, parsed_data = parse_all_events(cal_text, target_date, local_tz)
    recurring_uids = parsed_data['recurring_uids']
    
    all_events = []
    for event in parsed_events:
        is_ooo = _is_ooo_title(event['name'])
        is_recurring = event.get('uid', '') in recurring_uids
        
        all_events.append({
            'name': event['name'],
            'start': event['start'],
            'end': event['end'],
            'transparent': event.get('transparent', False),
            'is_ooo': is_ooo,
            'is_recurring': is_recurring,
            'all_day': event.get('all_day', False),
            'busystatus': event.get('busystatus', 'BUSY')
        })
    
    all_events.sort(key=lambda x: x['start'])
    
    return render_template('test_events.html', target_date=target_date, events=all_events)


@app.route('/admin/debug')
def test_debug():
    """Debug page: formatted view of status logic and all events."""
    return render_template('test_debug.html')


@app.route('/admin/backgrounds')
def test_backgrounds():
    """Gallery page showing all seasonal and holiday backgrounds."""
    backgrounds = [
        ('christmas.png', 'Christmas', 'Dec 11 - Jan 1'),
        ('newyears.png', "New Year's", 'Dec 30 - Jan 3'),
        ('halloween.png', 'Halloween', 'Oct 29 - Oct 31'),
        ('thanksgiving.png', 'Thanksgiving', 'Thanksgiving Week'),
        ('fourthofjuly.png', 'Fourth of July', 'June 30 - July 7'),
        ('memorialday.png', 'Memorial Day', 'Memorial Day Weekend'),
        ('laborday.png', 'Labor Day', 'Labor Day Weekend'),
        ('valentines.png', "Valentine's Day", 'Feb 13-14'),
        ('stpatricks.png', "St. Patrick's Day", 'Mar 16-17'),
        ('easter.png', 'Easter', 'Thu before - Mon after Easter Sunday'),
        ('winter.png', 'Winter', 'Dec 1 - Feb 28'),
        ('spring.png', 'Spring', 'Mar 1 - May 31'),
        ('summer.png', 'Summer', 'Jun 1 - Aug 31'),
        ('fall.png', 'Fall', 'Sep 1 - Nov 30'),
    ]
    return render_template('test_backgrounds.html', backgrounds=backgrounds)


@app.route('/api/update-log')
def api_update_log():
    """Get auto-update history log."""
    log_file = os.path.join(os.path.dirname(__file__), 'update_log.json')
    if os.path.exists(log_file):
        try:
            with open(log_file, 'r') as f:
                return jsonify(json.load(f))
        except (json.JSONDecodeError, TypeError):
            pass
    return jsonify([])


@app.route('/admin/<status>')
def test_mode(status='busy'):
    """Test page with different statuses. Usage: /admin/busy, /admin/free, /admin/ooo"""
    return render_template('display.html', test_mode=True, **_display_context())


@app.route('/api/test/busy')
def api_test_busy():
    """Mock: BUSY in office, next free in 1 hour."""
    now = datetime.now()
    return jsonify({
        'business_hours': True,
        'status': 'busy',
        'location': 'office',
        'return_time': (now + timedelta(hours=1)).isoformat(),
        'timestamp': now.isoformat()
    })


@app.route('/api/test/busy-remote')
def api_test_busy_remote():
    """Mock: BUSY working remote, next free in 2 hours."""
    now = datetime.now()
    return jsonify({
        'business_hours': True,
        'status': 'busy',
        'location': 'remote',
        'return_time': (now + timedelta(hours=2)).isoformat(),
        'timestamp': now.isoformat()
    })


@app.route('/api/test/free')
def api_test_free():
    """Mock: FREE with next meeting coming up."""
    now = datetime.now()
    next_meeting = now + timedelta(hours=1, minutes=30)
    return jsonify({
        'business_hours': True,
        'status': 'free',
        'location': 'remote',
        'meeting': {
            'name': 'Team Standup',
            'start': next_meeting.isoformat()
        },
        'timestamp': now.isoformat()
    })


@app.route('/api/test/free-office')
def api_test_free_office():
    """Mock: FREE in office, no subtext."""
    now = datetime.now()
    next_meeting = now + timedelta(hours=2)
    return jsonify({
        'business_hours': True,
        'status': 'free',
        'location': 'office',
        'meeting': {
            'name': 'All Hands Meeting',
            'start': next_meeting.isoformat()
        },
        'timestamp': now.isoformat()
    })


@app.route('/api/test/free-no-meetings')
def api_test_free_no_meetings():
    """Mock: FREE with no upcoming meetings."""
    now = datetime.now()
    return jsonify({
        'business_hours': True,
        'status': 'free',
        'location': 'remote',
        'timestamp': now.isoformat()
    })


@app.route('/api/test/ooo')
def api_test_ooo():
    """Mock: OOO returning tomorrow (all-day vacation)."""
    now = datetime.now()
    return_date = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0)
    return jsonify({
        'business_hours': True,
        'status': 'ooo',
        'location': 'remote',
        'return_time': return_date.isoformat(),
        'timestamp': now.isoformat()
    })


@app.route('/api/test/ooo-week')
def api_test_ooo_week():
    """Mock: OOO returning next week (multi-day vacation)."""
    now = datetime.now()
    return_date = (now + timedelta(days=5)).replace(hour=0, minute=0, second=0)
    return jsonify({
        'business_hours': True,
        'status': 'ooo',
        'location': 'remote',
        'return_time': return_date.isoformat(),
        'timestamp': now.isoformat()
    })


@app.route('/api/test/away')
def api_test_away():
    """Mock: AWAY, back in 2 hours (timed OOO block)."""
    now = datetime.now()
    return jsonify({
        'business_hours': True,
        'status': 'away',
        'location': 'office',
        'return_time': (now + timedelta(hours=2)).isoformat(),
        'timestamp': now.isoformat()
    })


@app.route('/api/test/away-tomorrow')
def api_test_away_tomorrow():
    """Mock: AWAY, back tomorrow morning (evening OOO block)."""
    now = datetime.now()
    return_time = (now + timedelta(days=1)).replace(hour=8, minute=0, second=0)
    return jsonify({
        'business_hours': True,
        'status': 'away',
        'location': 'remote',
        'return_time': return_time.isoformat(),
        'timestamp': now.isoformat()
    })


@app.route('/api/test/stale')
def api_test_stale():
    """Mock: FREE status with stale calendar warning."""
    now = datetime.now()
    next_meeting = now + timedelta(hours=1, minutes=30)
    return jsonify({
        'business_hours': True,
        'status': 'free',
        'location': 'office',
        'meeting': {'name': 'Team Standup', 'start': next_meeting.isoformat()},
        'calendar_stale': True,
        'timestamp': now.isoformat()
    })


if __name__ == '__main__':
    # Start background calendar refresh thread
    refresh_thread = threading.Thread(target=_background_calendar_refresh, daemon=True)
    refresh_thread.start()

    if HOST not in ('127.0.0.1', 'localhost', '::1') and not (CF_ACCESS_TEAM_DOMAIN and CF_ACCESS_AUD) and not TRUSTED_NETWORKS:
        print(f"⚠️ Listening on {HOST} without CF_ACCESS_* or TRUSTED_NETWORKS — only localhost can use the app")

    if os.getenv('FLASK_ENV', 'production') == 'development':
        app.run(host=HOST, port=PORT, debug=True, threaded=True)
    else:
        from waitress import serve
        print(f"🚀 Serving on http://{HOST}:{PORT}")
        # Keep proxy headers visible to the app so proxied requests are never mistaken for local ones
        serve(app, host=HOST, port=PORT, threads=8, clear_untrusted_proxy_headers=False)
