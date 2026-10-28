#!/usr/bin/env python3
"""
Performance testing script for calendar parsing
"""

import time
import sys
from datetime import datetime
import pytz

# Import current parsing functions
from app import (
    get_cached_calendar,
    parse_all_events,
    get_status
)

def test_current_performance():
    """Test current implementation performance."""
    print("=" * 60)
    print("Testing CURRENT implementation")
    print("=" * 60)
    
    # Test 1: Parse calendar for today
    start = time.time()
    cal_text = get_cached_calendar()
    cache_time = time.time() - start
    print(f"📥 Get calendar: {cache_time:.3f}s")
    
    # Test 2: Parse all events (includes tz_cache, recurrence map, etc.)
    start = time.time()
    local_tz = pytz.timezone('America/Los_Angeles')
    target_date = datetime.now().date()
    all_events, parsed_data = parse_all_events(cal_text, target_date, local_tz)
    all_time = time.time() - start
    print(f"📋 Parse all events: {all_time:.3f}s ({len(all_events)} events)")
    
    # Test 3: Get status (full pipeline)
    start = time.time()
    status = get_status()
    status_time = time.time() - start
    print(f"✅ Get status (full pipeline): {status_time:.3f}s")
    
    print(f"\n⏱️  Total parsing time: {all_time:.3f}s")
    print(f"⏱️  Full request time (get_status): {status_time:.3f}s")
    
    return {
        'cache': cache_time,
        'all': all_time,
        'status': status_time,
        'total': all_time
    }

if __name__ == "__main__":
    # Run test 3 times and average
    print("\nRunning 3 test iterations...\n")
    
    results = []
    for i in range(3):
        print(f"\n--- Iteration {i+1} ---")
        result = test_current_performance()
        results.append(result)
        if i < 2:
            time.sleep(1)
    
    # Calculate averages
    print("\n" + "=" * 60)
    print("AVERAGE RESULTS (3 iterations)")
    print("=" * 60)
    avg_total = sum(r['total'] for r in results) / len(results)
    avg_status = sum(r['status'] for r in results) / len(results)
    
    print(f"⏱️  Average parsing time: {avg_total:.3f}s")
    print(f"⏱️  Average full request time: {avg_status:.3f}s")
