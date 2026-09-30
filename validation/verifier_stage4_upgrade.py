"""Populated stage-3 to stage-4 migration with series, history and receipts."""
import json
import os
import urllib.error
import urllib.request
from datetime import date, timedelta

source = os.environ.get("TABLEKEEPER_SOURCE", "http://127.0.0.1:19103")
target = os.environ.get("TABLEKEEPER_TARGET", "http://127.0.0.1:19105")

def call(base, method, path, body=None, token=None, key=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if key:
        headers["Idempotency-Key"] = key
    request = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                     headers=headers, method=method)
    try:
        response = urllib.request.urlopen(request, timeout=10)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        raw = response.read()
        return response.status, json.loads(raw) if raw else None

day = date.today() + timedelta(days=35)
fixture = {"users": [{"id": name, "email": name + "@example.test", "password": "secret123",
                      "display_name": name} for name in ("manager", "diner")],
           "restaurants": [{"id": "r", "name": "R", "timezone": "UTC", "slot_minutes": 30,
                            "reservation_duration_minutes": 60, "cancellation_cutoff_minutes": 0,
                            "opening_hours": [{"weekday": wd, "opens": "18:00", "closes": "23:00"}
                                              for wd in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")],
                            "tables": [{"id": name, "label": name, "capacity": 2} for name in ("a", "b", "c")],
                            "manager_user_ids": ["manager"]}], "reservations": []}
assert call(source, "POST", "/_test/reset", fixture)[0] == 204
tokens = {}
for name in ("manager", "diner"):
    status, result = call(source, "POST", "/auth/login", {"email": name + "@example.test",
        "password": "secret123"})
    assert status == 200
    tokens[name] = result["token"]
diner, manager = tokens["diner"], tokens["manager"]
booking_body = {"restaurant_id": "r", "table_id": "a", "starts_at_local": day.isoformat() + "T19:00", "party_size": 2}
status, anchor = call(source, "POST", "/reservations", booking_body, diner, "anchor")
assert status == 201
status, series = call(source, "POST", "/series", {"anchor_reference": anchor["reference"],
    "count": 3, "interval_weeks": 1}, diner, "series")
assert status == 201
sid = series["series_id"]
refs = [item["reference"] for item in series["occurrences"]]
assert call(source, "POST", "/reservations/" + refs[1] + "/cancel", {}, diner)[0] == 200
assert call(source, "PATCH", "/reservations/" + refs[2], {"table_id": "b"}, diner)[0] == 200
old_history = call(source, "GET", "/reservations/" + refs[2] + "/history", token=diner)[1]
export = call(source, "GET", "/_test/export")[1]
assert call(target, "POST", "/_test/import", export)[0] == 204
assert call(target, "POST", "/reservations", booking_body, diner, "anchor") == (200, anchor)
status, imported_series = call(target, "GET", "/series/" + sid, token=diner)
assert status == 200
assert [(x["reservation"]["status"], x["exception"]) for x in imported_series["occurrences"]] == [
    ("confirmed", False), ("cancelled", False), ("confirmed", True)]
assert call(target, "GET", "/reservations/" + refs[2] + "/history", token=diner)[1] == old_history
close_day = (day + timedelta(days=14)).isoformat()
closure = {"table_id": "b", "from": close_day + "T18:00:00+00:00", "to": close_day + "T21:00:00+00:00"}
status, plan = call(target, "POST", "/restaurants/r/replans", closure, manager, "upgrade-preview")
assert status == 201 and plan["assignments"][0]["reference"] == refs[2]
status, applied = call(target, "POST", "/restaurants/r/replans/" + plan["plan_id"] + "/apply", {}, manager, "upgrade-apply")
assert status == 201 and applied["reservations"][0]["table_ids"] != ["b"]
updated = call(target, "GET", "/series/" + sid, token=diner)[1]
assert updated["revision"] == imported_series["revision"] + 1
assert updated["occurrences"][2]["exception"] is True
history = call(target, "GET", "/reservations/" + refs[2] + "/history", token=diner)[1]["entries"]
assert history[:-1] == old_history["entries"] and history[-1]["event"] == "reassigned"
status, amended = call(target, "POST", "/series/" + sid + "/amend", {"expected_revision": updated["revision"],
    "from_index": 0, "local_time": "20:00"}, diner, "upgrade-amend")
assert status == 201 and amended["occurrences"][0]["reservation"]["starts_at_local"].endswith("20:00")
assert amended["occurrences"][1]["reservation"]["status"] == "cancelled"
assert amended["occurrences"][2]["reservation"]["starts_at_local"].endswith("19:00")
print(json.dumps({"upgrade": "stage-3 populated export to stage-4", "tokens": "retained",
                  "booking_receipt": "retained", "history": "retained and extended",
                  "cancelled_and_exception_flags": "retained", "replan": "applied", "amend": "eligible-only"}))
