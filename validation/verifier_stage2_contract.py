"""Independent contract probes for the exact stage-2 service under test."""

import concurrent.futures
import json
import os
import urllib.error
import urllib.request


BASE = os.environ.get("TABLEKEEPER_URL", "http://127.0.0.1:8080")


def call(method, path, body=None, token=None, key=None):
    headers = {}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    if key:
        headers["Idempotency-Key"] = key
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    try:
        response = urllib.request.urlopen(req, timeout=10)
    except urllib.error.HTTPError as exc:
        response = exc
    raw = response.read()
    return response.status, json.loads(raw) if raw else None


fixture = {
    "users": [{"id": "u1", "email": "verify@example.test", "password": "secret123", "display_name": "Verifier"}],
    "restaurants": [{
        "id": "r1", "name": "Proof Table", "timezone": "Europe/Berlin",
        "slot_minutes": 30, "reservation_duration_minutes": 90,
        "cancellation_cutoff_minutes": 0,
        "opening_hours": [{"weekday": day, "opens": "18:00", "closes": "23:00"} for day in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")],
        "tables": [{"id": "a", "label": "Window", "capacity": 2}, {"id": "b", "label": "Garden", "capacity": 2}, {"id": "c", "label": "Corner", "capacity": 4}],
        "combinable": [["a", "b"], ["b", "c"]],
    }],
    "reservations": [],
}

assert call("POST", "/_test/reset", fixture)[0] == 204
status, login = call("POST", "/auth/login", {"email": "verify@example.test", "password": "secret123"})
assert status == 200, (status, login)
token = login["token"]
slot = "2030-01-01T19:00"
status, availability = call("GET", "/availability?restaurant_id=r1&date=2030-01-01&party_size=4")
assert status == 200
at = next(x for x in availability["slots"] if x["starts_at_local"] == slot)
assert at["available_table_ids"] == ["c"]
assert at["available_options"] == [{"table_ids": ["c"], "capacity": 4}, {"table_ids": ["a", "b"], "capacity": 4}, {"table_ids": ["b", "c"], "capacity": 6}]

body = {"restaurant_id": "r1", "table_ids": ["a", "b"], "party_size": 4, "starts_at_local": slot}
status, booked = call("POST", "/reservations", body, token, "combo-1")
assert status == 201 and booked["table_ids"] == ["a", "b"] and "table_id" not in booked, (status, booked)
status, replay = call("POST", "/reservations", body, token, "combo-1")
assert status == 200 and replay == booked

for member in ("a", "b"):
    status, result = call("POST", "/reservations", {"restaurant_id": "r1", "table_id": member, "party_size": 1, "starts_at_local": slot}, token, "overlap-" + member)
    assert (status, result["error"]["code"]) == (409, "table_unavailable")

for ids, code in ((["a", "c"], "combination_not_allowed"), (["a", "b", "c"], "combination_not_allowed"), (["a", "a"], "validation_failed")):
    status, result = call("POST", "/reservations", {**body, "table_ids": ids}, token, "invalid-" + "".join(ids))
    assert (status, result["error"]["code"]) == (422, code), (ids, status, result)

status, exported = call("GET", "/_test/export")
assert status == 200
status, cancelled = call("POST", "/reservations/" + booked["reference"] + "/cancel", token=token)
assert status == 200 and cancelled["status"] == "cancelled"
status, available_after = call("GET", "/availability?restaurant_id=r1&date=2030-01-01&party_size=4")
assert ["a", "b"] in [x["table_ids"] for x in next(x for x in available_after["slots"] if x["starts_at_local"] == slot)["available_options"]]
assert call("POST", "/_test/import", exported)[0] == 204
assert call("POST", "/reservations", body, token, "combo-1") == (200, booked)

assert call("POST", "/_test/reset", fixture)[0] == 204
token = call("POST", "/auth/login", {"email": "verify@example.test", "password": "secret123"})[1]["token"]
def race(index):
    return call("POST", "/reservations", body, token, "race-" + str(index))
with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
    outcomes = list(pool.map(race, range(20)))
assert sum(status == 201 for status, _ in outcomes) == 1, outcomes
assert all(status in (201, 409) for status, _ in outcomes), outcomes
assert all(result.get("error", {}).get("code") == "table_unavailable" for status, result in outcomes if status == 409), outcomes
print("PASS: options/order, pair validation, occupancy, cancellation, import receipt, 20-client conflict race")
assert call("POST", "/_test/reset", fixture)[0] == 204
