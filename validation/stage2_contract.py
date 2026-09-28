"""Independent HTTP checks for cumulative stage-2 combined-table behavior."""

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
    day = next(date.today() + timedelta(days=n) for n in range(8, 20)
               if (date.today() + timedelta(days=n)).weekday() == 0)
    hours = [{"weekday": name, "opens": "18:00", "closes": "23:00"}
             for name in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")]
    fixture = {
        "users": [{"id": "probe_user", "email": "probe@example.test",
                   "password": "probe-pass-123", "display_name": "Probe"}],
        "restaurants": [{
            "id": "probe_restaurant", "name": "Probe", "timezone": "UTC",
            "slot_minutes": 30, "reservation_duration_minutes": 90,
            "cancellation_cutoff_minutes": 120, "opening_hours": hours,
            "tables": [{"id": "a", "label": "Window", "capacity": 2},
                       {"id": "b", "label": "Garden", "capacity": 4},
                       {"id": "c", "label": "Nook", "capacity": 2}],
            "combinable": [["a", "b"], ["b", "c"]],
        }],
        "reservations": [],
    }
    check(call("POST", "/_test/reset", fixture)[0], 204, "reset")
    status, login = call("POST", "/auth/login", {"email": "probe@example.test",
                                                 "password": "probe-pass-123"})
    check(status, 200, "login")
    token = login["token"]
    local = day.isoformat() + "T19:00"
    query = urllib.parse.urlencode({"restaurant_id": "probe_restaurant",
                                    "date": day.isoformat(), "party_size": 5})
    status, availability = call("GET", "/availability?" + query)
    check(status, 200, "availability")
    slot = next(s for s in availability["slots"] if s["starts_at_local"] == local)
    check(slot["available_table_ids"], [], "single-table list")
    check(slot["available_options"], [{"table_ids": ["a", "b"], "capacity": 6},
                                      {"table_ids": ["b", "c"], "capacity": 6}],
          "ordered eligible combinations")
    base = {"restaurant_id": "probe_restaurant", "starts_at_local": local,
            "party_size": 5}
    status, invalid = call("POST", "/reservations", dict(base, table_ids=["a", "c"]),
                           token, "invalid-pair")
    check((status, invalid["error"]["code"]), (422, "combination_not_allowed"),
          "unlisted nontransitive pair")
    status, invalid = call("POST", "/reservations", dict(base, table_ids=["a", "a"]),
                           token, "duplicate-pair")
    check((status, invalid["error"]["code"]), (422, "validation_failed"),
          "duplicate member")
    body = dict(base, table_ids=["a", "b"])
    status, booked = call("POST", "/reservations", body, token, "combined")
    check(status, 201, "combined booking")
    check(booked["table_ids"], ["a", "b"], "response members")
    check("table_id" in booked, False, "pair omits singular table_id")
    check(call("POST", "/reservations", body, token, "combined"), (200, booked),
          "lost-response retry")
    status, conflict = call("POST", "/reservations", dict(base, table_id="b", party_size=4),
                            token, "conflict")
    check((status, conflict["error"]["code"]), (409, "table_unavailable"),
          "either member blocks overlap")
    status, snapshot = call("GET", "/_test/export")
    check(status, 200, "export")
    check(call("POST", "/_test/reset", fixture)[0], 204, "reset before import")
    check(call("POST", "/_test/import", snapshot)[0], 204, "import")
    check(call("POST", "/reservations", body, token, "combined"), (200, booked),
          "imported combined receipt")
    status, found = call("GET", "/reservations/" + booked["reference"], token=token)
    check((status, found["table_ids"]), (200, ["a", "b"]), "imported lookup")
    check(call("POST", "/reservations/" + booked["reference"] + "/cancel", {}, token)[0],
          200, "cancel frees pair")
    status, after = call("GET", "/availability?" + query)
    check(status, 200, "availability after cancel")
    restored = next(s for s in after["slots"] if s["starts_at_local"] == local)
    check(restored["available_options"], slot["available_options"],
          "both members free after cancel")
    print("PASS: pair ordering, nontransitivity, validation, overlap, retry, import, cancel")


if __name__ == "__main__":
    main()
