"""Independent HTTP probe for atomic closure repair and recurring amendments."""
import json
import os
import urllib.error
import urllib.request
from datetime import date, timedelta

BASE = os.environ.get("TABLEKEEPER_URL", "http://127.0.0.1:8080").rstrip("/")


def call(method, path, body=None, token=None, key=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if key:
        headers["Idempotency-Key"] = key
    request = urllib.request.Request(
        BASE + path, data=None if body is None else json.dumps(body).encode(),
        headers=headers, method=method)
    try:
        response = urllib.request.urlopen(request, timeout=10)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        raw = response.read()
        return response.status, json.loads(raw) if raw else None


def expect(actual, wanted, label):
    if actual != wanted:
        raise AssertionError(f"{label}: wanted {wanted!r}, got {actual!r}")


def main():
    day = date.today() + timedelta(days=21)
    hours = [{"weekday": weekday, "opens": "18:00", "closes": "23:00"}
             for weekday in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")]
    fixture = {"users": [
        {"id": name, "email": name + "@example.test", "password": "secret123",
         "display_name": name} for name in ("manager", "diner")],
        "restaurants": [{"id": "probe", "name": "Probe", "timezone": "UTC",
                         "slot_minutes": 30, "reservation_duration_minutes": 90,
                         "cancellation_cutoff_minutes": 120, "opening_hours": hours,
                         "tables": [{"id": "a", "label": "A", "capacity": 2},
                                    {"id": "b", "label": "B", "capacity": 2},
                                    {"id": "c", "label": "C", "capacity": 4}],
                         "combinable": [["a", "b"]], "manager_user_ids": ["manager"]}],
        "reservations": []}
    expect(call("POST", "/_test/reset", fixture)[0], 204, "reset")
    tokens = {}
    for name in ("manager", "diner"):
        status, result = call("POST", "/auth/login",
                              {"email": name + "@example.test", "password": "secret123"})
        expect(status, 200, "login")
        tokens[name] = result["token"]
    manager, diner = tokens["manager"], tokens["diner"]
    local = day.isoformat() + "T19:00"
    first = call("POST", "/reservations",
                 {"restaurant_id": "probe", "table_id": "a", "starts_at_local": local,
                  "party_size": 2}, diner, "booking-a")
    second = call("POST", "/reservations",
                  {"restaurant_id": "probe", "table_id": "b", "starts_at_local": local,
                   "party_size": 2}, diner, "booking-b")
    expect((first[0], second[0]), (201, 201), "two bookings")
    by_table = {"a": first[1], "b": second[1]}
    closure = {"table_id": "a", "from": day.isoformat() + "T18:30:00+00:00",
               "to": day.isoformat() + "T21:00:00+00:00"}
    before = call("GET", "/_test/export")[1]
    preview = call("POST", "/restaurants/probe/replans", closure, manager, "preview-1")
    expect(preview[0], 201, "preview status")
    plan = preview[1]
    expect((plan["restaurant_revision"], plan["moved_count"], plan["unused_seats"]),
           (2, 1, 2), "lexicographic plan objective")
    expect([(item["reference"], item["changed"]) for item in plan["assignments"]],
           [(ref, ref == by_table["a"]["reference"])
            for ref in sorted((by_table["a"]["reference"], by_table["b"]["reference"]))],
           "reference order and changed flags")
    after = call("GET", "/_test/export")[1]
    expect(after["state"]["reservations"], before["state"]["reservations"],
           "preview leaves bookings unchanged")
    expect(after["state"]["restaurant_revisions"], before["state"]["restaurant_revisions"],
           "preview leaves restaurant revision unchanged")
    expect(call("POST", "/restaurants/probe/replans", closure, manager, "preview-1"),
           (200, plan), "preview replay")
    apply_path = "/restaurants/probe/replans/" + plan["plan_id"] + "/apply"
    applied = call("POST", apply_path, {}, manager, "apply-1")
    expect((applied[0], applied[1]["restaurant_revision"]), (201, 3),
           "atomic apply revision")
    expect(call("POST", apply_path, {}, manager, "apply-1"), (200, applied[1]),
           "apply replay")
    expect(call("POST", apply_path, {}, manager, "apply-2")[1]["error"]["code"],
           "plan_already_applied", "distinct key rejected")
    moved = call("GET", "/reservations/" + by_table["a"]["reference"], token=diner)[1]
    unmoved = call("GET", "/reservations/" + by_table["b"]["reference"], token=diner)[1]
    expect((moved["table_ids"], moved["revision"], unmoved["revision"]),
           (["c"], 2, 1), "moved and unmoved revisions")
    history = call("GET", "/reservations/" + by_table["a"]["reference"] + "/history",
                   token=diner)[1]["entries"]
    expect((history[-1]["event"], history[-1]["plan_id"]), ("reassigned", plan["plan_id"]),
           "truthful reassignment history")
    rejected = call("POST", "/reservations",
                    {"restaurant_id": "probe", "table_id": "a",
                     "starts_at_local": local, "party_size": 2}, diner, "closed-create")
    expect((rejected[0], rejected[1]["error"]["code"]),
           (409, "table_unavailable"), "closure blocks creation")

    expect(call("POST", "/_test/reset", fixture)[0], 204, "series reset")
    status, login = call("POST", "/auth/login",
                         {"email": "diner@example.test", "password": "secret123"})
    expect(status, 200, "series login")
    diner = login["token"]
    status, anchor = call("POST", "/reservations",
                          {"restaurant_id": "probe", "table_id": "a",
                           "starts_at_local": local, "party_size": 2}, diner, "anchor")
    expect(status, 201, "series anchor")
    status, series = call("POST", "/series",
                          {"anchor_reference": anchor["reference"], "count": 3,
                           "interval_weeks": 1}, diner, "series")
    expect(status, 201, "series create")
    sid = series["series_id"]
    amend_path = "/series/" + sid + "/amend"
    body = {"expected_revision": 1, "from_index": 1, "local_time": "20:00"}
    status, amended = call("POST", amend_path, body, diner, "amend-1")
    expect((status, amended["revision"]), (201, 2), "series amendment")
    expect([entry["reservation"]["starts_at_local"][-5:]
            for entry in amended["occurrences"]], ["19:00", "20:00", "20:00"],
           "original dates and scoped time change")
    expect(call("POST", amend_path, body, diner, "amend-1"), (200, amended),
           "series original receipt")
    stale = call("POST", amend_path, body, diner, "amend-stale")
    expect((stale[0], stale[1]["error"]["code"]), (409, "stale_revision"),
           "same expected revision cannot change again")
    print("stage4_contract: PASS deterministic repair, preview/apply atomicity, closure, history, series amendment, replay")


if __name__ == "__main__":
    main()
