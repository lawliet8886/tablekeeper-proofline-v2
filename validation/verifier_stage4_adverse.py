"""Adversarial HTTP check for imported pending-plan integrity."""
import copy
import json
import os
import urllib.error
import urllib.request
from datetime import date, timedelta

base = os.environ.get("TABLEKEEPER_URL", "http://127.0.0.1:19104")

def call(method, path, body=None, token=None, key=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if key:
        headers["Idempotency-Key"] = key
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        response = urllib.request.urlopen(request, timeout=10)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        raw = response.read()
        return response.status, json.loads(raw) if raw else None

day = date.today() + timedelta(days=30)
weekday = day.strftime("%a").lower()
fixture = {"users": [{"id": "manager", "email": "manager@example.test", "password": "secret123", "display_name": "Manager"},
                     {"id": "diner", "email": "diner@example.test", "password": "secret123", "display_name": "Diner"}],
           "restaurants": [{"id": "r", "name": "R", "timezone": "UTC", "slot_minutes": 30,
                            "reservation_duration_minutes": 90, "cancellation_cutoff_minutes": 0,
                            "opening_hours": [{"weekday": weekday, "opens": "18:00", "closes": "23:00"}],
                            "tables": [{"id": "a", "label": "A", "capacity": 2}, {"id": "b", "label": "B", "capacity": 2}],
                            "manager_user_ids": ["manager"]}], "reservations": []}
assert call("POST", "/_test/reset", fixture)[0] == 204
tokens = {}
for name in ("manager", "diner"):
    status, result = call("POST", "/auth/login", {"email": name + "@example.test", "password": "secret123"})
    assert status == 200
    tokens[name] = result["token"]
booking_body = {"restaurant_id": "r", "table_id": "a", "starts_at_local": day.isoformat() + "T19:00", "party_size": 2}
status, booking = call("POST", "/reservations", booking_body, tokens["diner"], "book")
assert status == 201
closure = {"table_id": "a", "from": day.isoformat() + "T18:00:00+00:00", "to": day.isoformat() + "T21:00:00+00:00"}
status, plan = call("POST", "/restaurants/r/replans", closure, tokens["manager"], "preview")
assert status == 201 and plan["assignments"][0]["table_ids"] == ["b"]
export = call("GET", "/_test/export")[1]
tampered = copy.deepcopy(export)
tampered["state"]["plans"][plan["plan_id"]]["assignments"][0]["table_ids"] = ["a"]
status, result = call("POST", "/_test/import", tampered)
print(json.dumps({"case": "pending plan assigns booking to table closed by its own closure",
                  "import_status": status, "import_response": result,
                  "expected_status": 422, "reference": booking["reference"]}))
if status == 204:
    apply_status, applied = call("POST", "/restaurants/r/replans/" + plan["plan_id"] + "/apply",
                                 {}, tokens["manager"], "apply-corrupt")
    current = call("GET", "/reservations/" + booking["reference"], token=tokens["diner"])[1]
    print(json.dumps({"apply_status": apply_status,
                      "booking_table_ids": current["table_ids"],
                      "booking_status": current["status"],
                      "closure": closure}))
assert status == 422, "invalid imported state must be rejected"
assert call("GET", "/_test/export")[1] == export, "rejected import changed destination"
