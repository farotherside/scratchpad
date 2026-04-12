#!/usr/bin/env python3
"""
Debug script — shows raw TheTVDB API responses for a test show.
Usage: python debug_thetvdb.py <apikey_file> [show name]
"""
import sys
import json
import requests

THETVDB_BASE = "https://api4.thetvdb.com/v4"
TIMEOUT = 15

def main():
    if len(sys.argv) < 2:
        print("Usage: python debug_thetvdb.py <apikey_file> [show name]")
        sys.exit(1)

    api_key = open(sys.argv[1]).read().strip()
    show_name = sys.argv[2] if len(sys.argv) > 2 else "Breaking Bad"

    # 1. Authenticate
    print(f"=== AUTH ===")
    resp = requests.post(f"{THETVDB_BASE}/login", json={"apikey": api_key}, timeout=TIMEOUT)
    print(f"Status: {resp.status_code}")
    auth_data = resp.json()
    print(json.dumps(auth_data, indent=2)[:500])
    token = auth_data.get("data", {}).get("token")
    if not token:
        print("ERROR: No token returned")
        sys.exit(1)
    print(f"Token: {token[:30]}...")

    headers = {"Authorization": f"Bearer {token}"}

    # 2. Search
    print(f"\n=== SEARCH: {show_name!r} ===")
    resp = requests.get(f"{THETVDB_BASE}/search", params={"query": show_name, "type": "series"}, headers=headers, timeout=TIMEOUT)
    print(f"Status: {resp.status_code}")
    search_data = resp.json()
    results = search_data.get("data", [])
    print(f"Results count: {len(results)}")
    if results:
        print("First result fields:", list(results[0].keys()))
        print(json.dumps(results[0], indent=2)[:800])

    if not results:
        print("No results, exiting")
        sys.exit(1)

    # 3. Extract series ID
    first = results[0]
    series_id = first.get("tvdb_id") or first.get("id") or first.get("objectID")
    print(f"\nSeries ID candidates: tvdb_id={first.get('tvdb_id')!r}, id={first.get('id')!r}, objectID={first.get('objectID')!r}")
    print(f"Using series_id: {series_id!r}")

    if not series_id:
        print("ERROR: Could not determine series ID")
        sys.exit(1)

    # 4. Fetch episodes - try different endpoint variations
    print(f"\n=== EPISODES (series/{series_id}/episodes/official) ===")
    resp = requests.get(
        f"{THETVDB_BASE}/series/{series_id}/episodes/official",
        params={"page": 0},
        headers=headers,
        timeout=TIMEOUT,
    )
    print(f"Status: {resp.status_code}")
    ep_data = resp.json()
    print("Top-level keys:", list(ep_data.keys()))
    data_val = ep_data.get("data")
    if isinstance(data_val, dict):
        print("data keys:", list(data_val.keys()))
        eps = data_val.get("episodes", [])
    elif isinstance(data_val, list):
        print("data is a list, len:", len(data_val))
        eps = data_val
    else:
        print("data value:", repr(data_val)[:200])
        eps = []
    print(f"Episodes found: {len(eps)}")
    if eps:
        print("First episode (all fields):", json.dumps(eps[0], indent=2))
        if len(eps) > 1:
            print("Second episode (all fields):", json.dumps(eps[1], indent=2))

    # 5. Also try the plain episodes endpoint
    print(f"\n=== EPISODES (series/{series_id}/episodes/default) ===")
    resp2 = requests.get(
        f"{THETVDB_BASE}/series/{series_id}/episodes/default",
        params={"page": 0},
        headers=headers,
        timeout=TIMEOUT,
    )
    print(f"Status: {resp2.status_code}")
    ep_data2 = resp2.json()
    print("Top-level keys:", list(ep_data2.keys()))
    data_val2 = ep_data2.get("data")
    if isinstance(data_val2, dict):
        print("data keys:", list(data_val2.keys()))
        eps2 = data_val2.get("episodes", [])
    elif isinstance(data_val2, list):
        print("data is a list, len:", len(data_val2))
        eps2 = data_val2
    else:
        print("data value:", repr(data_val2)[:200])
        eps2 = []
    print(f"Episodes found: {len(eps2)}")
    if eps2:
        print("First episode (all fields):", json.dumps(eps2[0], indent=2))

if __name__ == "__main__":
    main()
