"""Independent HTTP checks derived from cumulative Stage 3 policy and series rules."""

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta


BASE = os.environ.get("TABLEKEEPER_URL", "http://127.0.0.1:8080").rstrip("/")


def call(method, path, body=None, token=None, key=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if key:
        headers["Idempotency-Key"] = key
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        response = urllib.request.urlopen(request, timeout=10)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        raw = response.read()
        return response.status, json.loads(raw) if raw else None


def check(actual, expected, label):
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")


def main():
    day = next(date.today() + timedelta(days=n) for n in range(14, 21)
               if (date.today() + timedelta(days=n)).weekday() == 0)
    later = day + timedelta(days=7)
    hours = [{"weekday": name, "opens": "18:00", "closes": "23:00"}
             for name in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")]
    fixture = {
        "users": [
            {"id": "manager", "email": "manager@example.test", "password": "secret123", "display_name": "Manager"},
            {"id": "diner", "email": "diner@example.test", "password": "secret123", "display_name": "Diner"},
        ],
        "restaurants": [{
            "id": "probe", "name": "Probe", "timezone": "UTC", "slot_minutes": 30,
            "reservation_duration_minutes": 90, "cancellation_cutoff_minutes": 120,
            "opening_hours": hours,
            "tables": [{"id": "a", "label": "A", "capacity": 2},
                       {"id": "b", "label": "B", "capacity": 4}],
            "combinable": [["a", "b"]], "manager_user_ids": ["manager"],
        }],
        "reservations": [],
    }
    check(call("POST", "/_test/reset", fixture)[0], 204, "reset")
    tokens = {}
    for name in ("manager", "diner"):
        status, result = call("POST", "/auth/login", {"email": name + "@example.test", "password": "secret123"})
        check(status, 200, name + " login")
        tokens[name] = result["token"]
    manager, diner = tokens["manager"], tokens["diner"]
    policy = {"effective_from": later.isoformat(), "slot_minutes": 30,
              "reservation_duration_minutes": 120, "cancellation_cutoff_minutes": 60,
              "opening_hours": hours, "capacities": {"a": 1, "b": 4}}
    status, result = call("POST", "/restaurants/probe/policies", policy, diner, "forbidden-policy")
    check((status, result["error"]["code"]), (403, "forbidden"), "manager authority")
    status, first_policy = call("POST", "/restaurants/probe/policies", policy, manager, "policy-one")
    check((status, first_policy["policy_version"]), (201, 1), "policy version one")
    check(call("POST", "/restaurants/probe/policies", policy, manager, "policy-one"),
          (200, first_policy), "policy replay")
    base = {"restaurant_id": "probe", "table_id": "a", "starts_at_local": day.isoformat() + "T19:00", "party_size": 2}
    status, anchor = call("POST", "/reservations", base, diner, "anchor")
    check((status, anchor["revision"], anchor["accepted_terms"]["policy_version"]),
          (201, 1, 0), "anchor policy snapshot")
    reference = anchor["reference"]
    status, history = call("GET", "/reservations/" + reference + "/history", token=diner)
    check((status, [e["event"] for e in history["entries"]], history["entries"][0]["revision"]),
          (200, ["created"], 1), "initial truthful history")
    check(call("GET", "/reservations/" + reference + "/history", token=manager)[0],
          404, "manager cannot read diner history")
    query = urllib.parse.urlencode({"restaurant_id": "probe", "date": later.isoformat(),
                                    "party_size": 2, "explain": "true"})
    status, availability = call("GET", "/availability?" + query)
    check(status, 200, "explained availability")
    slot = next(s for s in availability["slots"] if s["starts_at_local"].endswith("T19:00"))
    explanation = slot["explain"][0]
    check((explanation["policy_version"], explanation["available"],
           [(r["rule"], r["holds"]) for r in explanation["rules"]]),
          (1, False, [("capacity", False), ("no_overlap", True)]), "independent availability rules")
    bad_query = query.replace("explain=true", "explain=false")
    check(call("GET", "/availability?" + bad_query)[0], 422, "strict explain value")
    series_body = {"anchor_reference": reference, "count": 2, "interval_weeks": 1}
    status, failure = call("POST", "/series", series_body, diner, "series-attempt")
    check((status, failure["error"]["code"]), (422, "party_exceeds_capacity"), "atomic series failure")
    check(call("GET", "/reservations", token=diner)[1]["reservations"], [anchor], "no partial occurrence")
    replacement = dict(policy, capacities={"a": 3, "b": 4})
    status, second_policy = call("POST", "/restaurants/probe/policies", replacement, manager, "policy-two")
    check((status, second_policy["policy_version"]), (201, 2), "same-date supersession")
    status, series = call("POST", "/series", series_body, diner, "series-success")
    check((status, series["revision"], len(series["occurrences"])), (201, 1, 2), "series adoption")
    check(series["occurrences"][0]["reservation"], anchor, "anchor identity and terms unchanged")
    child = series["occurrences"][1]["reservation"]
    check((child["accepted_terms"]["policy_version"], child["revision"]), (2, 1), "child date policy")
    check(call("POST", "/series", series_body, diner, "series-success"), (200, series), "series original receipt")
    status, unchanged = call("PATCH", "/reservations/" + child["reference"],
                             {"party_size": 2, "expected_revision": 1}, diner)
    check((status, unchanged["revision"]), (200, 1), "no-op retains revision")
    status, changed = call("PATCH", "/reservations/" + child["reference"],
                           {"party_size": 3, "expected_revision": 1}, diner)
    check((status, changed["revision"]), (200, 2), "real amendment revision")
    status, stale = call("PATCH", "/reservations/" + child["reference"],
                         {"party_size": 2, "expected_revision": 1}, diner)
    check((status, stale["error"]["code"]), (409, "stale_revision"), "compare and swap")
    status, current_series = call("GET", "/series/" + series["series_id"], token=diner)
    check((status, current_series["revision"], current_series["occurrences"][1]["exception"]),
          (200, 2, True), "permanent exception")
    status, child_history = call("GET", "/reservations/" + child["reference"] + "/history", token=diner)
    check((status, [(e["seq"], e["event"], e["revision"]) for e in child_history["entries"]]),
          (200, [(1, "created", 1), (2, "changed", 2)]), "history after no-op and change")
    status, snapshot = call("GET", "/_test/export")
    check(status, 200, "export")
    check(call("POST", "/_test/import", snapshot)[0], 204, "same-stage import")
    check(call("GET", "/series/" + series["series_id"], token=diner)[1],
          current_series, "series survives import")
    print("stage3_contract: PASS policy, history, recurrence, replay, exception, import")


if __name__ == "__main__":
    main()
