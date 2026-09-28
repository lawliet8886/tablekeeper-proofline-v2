"""Independent stage-1 HTTP contract probe; run with python -B against a live service."""

import json
import os
import sys
import urllib.error
import urllib.request


BASE = os.environ.get("TABLEKEEPER_URL", "http://127.0.0.1:8080").rstrip("/")


def call(method, path, body=None, token=None, key=None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if key:
        headers["Idempotency-Key"] = key
    request = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        response = urllib.request.urlopen(request, timeout=5)
    except urllib.error.HTTPError as error:
        response = error
    raw = response.read()
    return response.status, json.loads(raw) if raw else None


def expect(actual, desired, context):
    if actual != desired:
        raise AssertionError(f"{context}: expected {desired!r}, got {actual!r}")


def main():
    opening = [{"weekday": day, "opens": "10:00", "closes": "22:00"}
               for day in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")]
    fixture = {
        "users": [{"id": "probe_user", "email": "probe@example.test",
                   "password": "probe-pass-123", "display_name": "Probe"}],
        "restaurants": [{"id": "probe_restaurant", "name": "Probe", "timezone": "UTC",
                         "slot_minutes": 30, "reservation_duration_minutes": 90,
                         "cancellation_cutoff_minutes": 120, "opening_hours": opening,
                         "tables": [{"id": "probe_a", "label": "A", "capacity": 2},
                                    {"id": "probe_b", "label": "B", "capacity": 2}]}],
        "reservations": [],
    }
    expect(call("POST", "/_test/reset", fixture)[0], 204, "reset")
    status, login = call("POST", "/auth/login", {"email": "probe@example.test",
                                                   "password": "probe-pass-123"})
    expect(status, 200, "seeded login")
    token = login["token"]
    first = {"restaurant_id": "probe_restaurant", "table_id": "probe_a",
             "starts_at_local": "2030-01-01T18:00", "party_size": 2}
    second = dict(first, table_id="probe_b")
    status, a = call("POST", "/reservations", first, token, "probe-key-a")
    expect(status, 201, "first booking")
    status, b = call("POST", "/reservations", second, token, "probe-key-b")
    expect(status, 201, "second booking")
    status, replay = call("POST", "/reservations", first, token, "probe-key-a")
    expect((status, replay), (200, a), "idempotent booking replay")
    moves = {"moves": [{"reference": a["reference"], "table_id": "probe_b"},
                        {"reference": b["reference"], "table_id": "probe_a"}]}
    status, moved = call("POST", "/reservation-moves", moves, token, "probe-key-a")
    expect(status, 201, "same key on different path and atomic swap")
    expect([r["table_id"] for r in moved["reservations"]],
           ["probe_b", "probe_a"], "swap order")
    status, snapshot = call("GET", "/_test/export")
    expect(status, 200, "export")
    expect(snapshot["track"], "tablekeeper", "export track")
    expect(call("POST", "/_test/reset", fixture)[0], 204, "second reset")
    expect(call("POST", "/_test/import", snapshot)[0], 204, "import")
    status, replay = call("POST", "/reservation-moves", moves, token, "probe-key-a")
    expect((status, replay), (200, moved), "imported move receipt")
    status, old = call("POST", "/reservations", first, token, "probe-key-a")
    expect((status, old), (200, a), "imported booking receipt")
    status, invalid = call("POST", "/reservations", dict(first, party_size=True),
                           token, "probe-invalid")
    expect((status, invalid["error"]["code"]),
           (422, "validation_failed"), "boolean party size")
    print("PASS: swap, cross-path idempotency, export/import receipts, malformed party size")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"FAIL: {error}", file=sys.stderr)
        raise
