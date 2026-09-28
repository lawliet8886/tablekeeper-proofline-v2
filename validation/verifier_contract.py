"""Independent HTTP probes derived from the stage-1 contract."""
import copy
import json
import os
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

BASE = os.environ.get("TABLEKEEPER_URL", "http://127.0.0.1:18082")


def call(method, path, body=None, token=None, key=None):
    data = None if body is None else json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if key:
        headers["Idempotency-Key"] = key
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        res = urllib.request.urlopen(req, timeout=10)
    except urllib.error.HTTPError as exc:
        res = exc
    raw = res.read()
    return res.status, json.loads(raw) if raw else None


def expected(actual, value, label):
    assert actual == value, f"{label}: expected {value!r}; got {actual!r}"


def fixture(zone, day):
    return {
        "users": [{"id": "u", "email": "u@example.test", "password": "password123", "display_name": "U"}],
        "restaurants": [{"id": "r", "name": "R", "timezone": zone, "slot_minutes": 30,
                         "reservation_duration_minutes": 90, "cancellation_cutoff_minutes": 0,
                         "opening_hours": [{"weekday": day, "opens": "01:00", "closes": "05:00"}],
                         "tables": [{"id": "t", "label": "T", "capacity": 2}]}],
        "reservations": [],
    }


def main():
    expected(call("GET", "/health"), (200, {"status": "ok"}), "health")
    expected(call("POST", "/_test/reset", fixture("Europe/Berlin", "sun"))[0], 204, "reset")
    status, login = call("POST", "/auth/login", {"email": "u@example.test", "password": "password123"})
    expected(status, 200, "login")
    token = login["token"]
    status, spring = call("GET", "/availability?restaurant_id=r&date=2026-03-29&party_size=1")
    expected(status, 200, "spring availability")
    assert all(not s["starts_at_local"].startswith("2026-03-29T02:") for s in spring["slots"])
    body = {"restaurant_id": "r", "table_id": "t", "starts_at_local": "2026-03-29T02:00", "party_size": 1}
    status, err = call("POST", "/reservations", body, token, "spring")
    expected((status, err["error"]["code"]), (422, "invalid_local_time"), "skipped hour")

    status, fall = call("GET", "/availability?restaurant_id=r&date=2026-10-25&party_size=1")
    expected(status, 200, "fall availability")
    first = [s for s in fall["slots"] if s["starts_at_local"] == "2026-10-25T02:30"]
    expected(len(first), 1, "repeated slot once")
    assert first[0]["starts_at"].endswith("+02:00"), first
    body["starts_at_local"] = "2026-10-25T02:30"
    status, booked = call("POST", "/reservations", body, token, "fall")
    expected(status, 201, "fall booking")
    expected(booked["ends_at"], "2026-10-25T03:00:00+01:00", "absolute 90 minutes")

    status, snapshot = call("GET", "/_test/export")
    expected(status, 200, "export")
    invalid = copy.deepcopy(snapshot)
    invalid["state"]["users"]["u"]["hash"] = "not-hex"
    status, err = call("POST", "/_test/import", invalid)
    expected((status, err["error"]["code"]), (422, "validation_failed"), "malformed import")
    expected(call("GET", "/_test/export")[1], snapshot, "failed import atomicity")

    expected(call("POST", "/_test/reset", fixture("UTC", "tue"))[0], 204, "concurrency reset")
    token = call("POST", "/auth/login", {"email": "u@example.test", "password": "password123"})[1]["token"]
    body["starts_at_local"] = "2030-01-01T02:00"
    def attempt(n):
        return call("POST", "/reservations", body, token, f"concurrent-{n}")
    with ThreadPoolExecutor(max_workers=25) as pool:
        outcomes = list(pool.map(attempt, range(25)))
    expected(sum(s == 201 for s, _ in outcomes), 1, "single winner")
    expected(sum(s == 409 and b["error"]["code"] == "table_unavailable" for s, b in outcomes), 24, "collision losers")
    status, snapshot = call("GET", "/_test/export")
    expected(status, 200, "concurrency export")
    broken = copy.deepcopy(snapshot)
    reference = next(iter(broken["state"]["reservations"]))
    broken["state"]["reservations"][reference]["table_id"] = "missing_table"
    status, error = call("POST", "/_test/import", broken)
    expected((status, error and error.get("error", {}).get("code")),
             (422, "validation_failed"), "dangling imported table")
    expected(call("GET", "/_test/export")[1], snapshot, "rejected import atomicity")
    print("PASS: health, DST gap/fold, import validation/atomicity, 25 concurrent contenders")


if __name__ == "__main__":
    main()
