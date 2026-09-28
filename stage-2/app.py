"""Self-contained Tablekeeper stage-1 HTTP service."""

import copy
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
from datetime import date, datetime, time, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


LOCAL = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")
DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CLOCK = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
EMAIL = re.compile(r"^[^\s@]+@[^\s@]+$")
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
LOCK = threading.RLock()


def blank_state():
    return {"users": {}, "restaurants": {}, "reservations": {}, "tokens": {}, "receipts": {}}


STATE = blank_state()


class Problem(Exception):
    def __init__(self, status, code):
        self.status = status
        self.code = code


def fail(status, code):
    raise Problem(status, code)


def require_type(value, expected, *, required=True):
    if value is None and not required:
        return
    if value is None:
        fail(422, "validation_failed")
    if expected is int:
        if type(value) is not int:
            fail(400, "malformed_request")
    elif not isinstance(value, expected):
        fail(400, "malformed_request")


def text_field(body, key, *, required=True):
    value = body.get(key)
    require_type(value, str, required=required)
    return value


def opaque(value):
    if not value or len(value) > 64:
        fail(422, "validation_failed")
    return value


def integer(value, *, endpoint_party=False):
    if type(value) is not int:
        fail(422 if endpoint_party else 400, "validation_failed" if endpoint_party else "malformed_request")
    if value < 1:
        fail(422, "validation_failed")
    return value


def parse_date(value):
    if not isinstance(value, str):
        fail(400, "malformed_request")
    if not DAY.fullmatch(value):
        fail(422, "validation_failed")
    try:
        return date.fromisoformat(value)
    except ValueError:
        fail(422, "validation_failed")


def parse_local(value):
    if value is None:
        fail(422, "validation_failed")
    if not isinstance(value, str):
        fail(400, "malformed_request")
    if not LOCAL.fullmatch(value):
        fail(422, "validation_failed")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M")
    except ValueError:
        fail(422, "validation_failed")


def resolve_local(local, zone):
    tz = ZoneInfo(zone)
    aware = local.replace(tzinfo=tz, fold=0)
    if aware.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None) != local:
        fail(422, "invalid_local_time")
    return aware


def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 150_000)
    return {"salt": salt, "hash": digest.hex()}


def verify_password(password, user):
    candidate = hash_password(password, user["salt"])["hash"]
    return hmac.compare_digest(candidate, user["hash"])


def issue_token(user_id):
    token = secrets.token_urlsafe(32)
    STATE["tokens"][token] = user_id
    return token


def min_of_clock(clock):
    m = CLOCK.fullmatch(clock)
    if not m:
        fail(422, "validation_failed")
    return int(m.group(1)) * 60 + int(m.group(2))


def restaurant_for(restaurant_id):
    restaurant = STATE["restaurants"].get(restaurant_id)
    if restaurant is None:
        fail(404, "not_found")
    return restaurant


def table_for(restaurant, table_id):
    for table in restaurant["tables"]:
        if table["id"] == table_id:
            return table
    fail(404, "not_found")


def interval(reservation):
    start = datetime.fromisoformat(reservation["starts_at"]).astimezone(timezone.utc)
    end = datetime.fromisoformat(reservation["ends_at"]).astimezone(timezone.utc)
    return start, end


def tables_of(reservation):
    return reservation.get("table_ids", [reservation["table_id"]] if "table_id" in reservation else [])


def occupied(restaurant_id, table_id, start, end, *, exclude=()):
    for reservation in STATE["reservations"].values():
        if reservation["reference"] in exclude or reservation["status"] != "confirmed" or reservation["restaurant_id"] != restaurant_id or table_id not in tables_of(reservation):
            continue
        old_start, old_end = interval(reservation)
        if start < old_end and old_start < end:
            return True
    return False


def select_tables(restaurant, body, current=None):
    if "table_id" in body and "table_ids" in body:
        fail(422, "validation_failed")
    if "table_ids" in body:
        ids = body["table_ids"]
        if not isinstance(ids, list):
            fail(400, "malformed_request")
    elif "table_id" in body:
        if not isinstance(body["table_id"], str):
            fail(400, "malformed_request")
        ids = [body["table_id"]]
    elif current is not None:
        ids = tables_of(current)
    else:
        fail(422, "validation_failed")
    if not ids:
        fail(422, "validation_failed")
    if len(ids) > 2:
        fail(422, "combination_not_allowed")
    if any(not isinstance(item, str) for item in ids):
        fail(400, "malformed_request")
    if len(set(ids)) != len(ids):
        fail(422, "validation_failed")
    tables = [table_for(restaurant, opaque(item)) for item in ids]
    if len(ids) == 2 and not any(set(pair) == set(ids) for pair in restaurant.get("combinable", [])):
        fail(422, "combination_not_allowed")
    return ids, tables


def validate_booking(restaurant, table_ids, local_text, party_size):
    tables = [table_for(restaurant, opaque(item)) for item in table_ids]
    party_size = integer(party_size, endpoint_party=True)
    local = parse_local(local_text)
    aware = resolve_local(local, restaurant["timezone"])
    if party_size > sum(table["capacity"] for table in tables):
        fail(422, "party_exceeds_capacity")
    hours = next((h for h in restaurant["opening_hours"] if h["weekday"] == WEEKDAYS[local.weekday()]), None)
    minute = local.hour * 60 + local.minute
    if hours is None or minute < min_of_clock(hours["opens"]) or minute + restaurant["reservation_duration_minutes"] > min_of_clock(hours["closes"]):
        fail(422, "outside_opening_hours")
    if (minute - min_of_clock(hours["opens"])) % restaurant["slot_minutes"]:
        fail(422, "not_on_slot_grid")
    end = (aware.astimezone(timezone.utc) + timedelta(minutes=restaurant["reservation_duration_minutes"])).astimezone(ZoneInfo(restaurant["timezone"]))
    fields = {"table_ids": list(table_ids), "party_size": party_size, "starts_at_local": local_text, "starts_at": aware.isoformat(timespec="seconds"), "ends_at": end.isoformat(timespec="seconds")}
    if len(table_ids) == 1:
        fields["table_id"] = table_ids[0]
    return fields


def public_reservation(reservation):
    result = {key: reservation[key] for key in ("reservation_id", "reference", "restaurant_id", "party_size", "status", "starts_at_local", "starts_at", "ends_at", "created_at")}
    result["table_ids"] = tables_of(reservation)
    if len(result["table_ids"]) == 1:
        result["table_id"] = result["table_ids"][0]
    return result


def owned(reference, user_id):
    reservation = STATE["reservations"].get(reference)
    if reservation is None or reservation["user_id"] != user_id:
        fail(404, "not_found")
    return reservation


def cutoff(reservation):
    restaurant = restaurant_for(reservation["restaurant_id"])
    start = datetime.fromisoformat(reservation["starts_at"]).astimezone(timezone.utc)
    if datetime.now(timezone.utc) >= start - timedelta(minutes=restaurant["cancellation_cutoff_minutes"]):
        fail(409, "cutoff_passed")


def changes(reservation, body):
    restaurant = restaurant_for(reservation["restaurant_id"])
    table_ids, _ = select_tables(restaurant, body, reservation)
    local = body.get("starts_at_local", reservation["starts_at_local"])
    party = body.get("party_size", reservation["party_size"])
    return validate_booking(restaurant, table_ids, local, party)


def amended(reservation, fields):
    result = {**reservation, **fields}
    if len(fields["table_ids"]) == 2:
        result.pop("table_id", None)
    return result


def canonical(body):
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def receipt_key(user_id, path, key):
    return json.dumps([user_id, path, key], separators=(",", ":"))


def replay(user_id, path, key, body):
    receipt = STATE["receipts"].get(receipt_key(user_id, path, key))
    if receipt is None:
        return None
    if receipt["body"] != canonical(body):
        fail(409, "idempotency_key_reuse")
    return copy.deepcopy(receipt["response"])


def record(user_id, path, key, body, response):
    STATE["receipts"][receipt_key(user_id, path, key)] = {"body": canonical(body), "response": copy.deepcopy(response)}


def validate_fixture(fixture):
    if not isinstance(fixture, dict):
        fail(422, "validation_failed")
    for key in ("users", "restaurants", "reservations"):
        if not isinstance(fixture.get(key), list):
            fail(422, "validation_failed")
    result = blank_state()
    try:
        for raw in fixture["users"]:
            uid, email, password, name = (raw[k] for k in ("id", "email", "password", "display_name"))
            if not all(isinstance(x, str) for x in (uid, email, password, name)) or not opaque(uid) or not EMAIL.fullmatch(email) or len(password) < 8 or uid in result["users"]:
                fail(422, "validation_failed")
            result["users"][uid] = {"id": uid, "email": email, "display_name": name, **hash_password(password)}
        for raw in fixture["restaurants"]:
            rid = raw["id"]
            if not isinstance(rid, str) or not opaque(rid) or rid in result["restaurants"]:
                fail(422, "validation_failed")
            ZoneInfo(raw["timezone"])
            for key in ("slot_minutes", "reservation_duration_minutes"):
                integer(raw[key])
            if type(raw["cancellation_cutoff_minutes"]) is not int or raw["cancellation_cutoff_minutes"] < 0:
                fail(422, "validation_failed")
            if not isinstance(raw["opening_hours"], list) or not isinstance(raw["tables"], list):
                fail(422, "validation_failed")
            days = set()
            for hours in raw["opening_hours"]:
                if hours["weekday"] not in WEEKDAYS or hours["weekday"] in days or min_of_clock(hours["opens"]) >= min_of_clock(hours["closes"]):
                    fail(422, "validation_failed")
                days.add(hours["weekday"])
            tables = set()
            for table in raw["tables"]:
                if not isinstance(table["id"], str) or not opaque(table["id"]) or table["id"] in tables or not isinstance(table["label"], str):
                    fail(422, "validation_failed")
                integer(table["capacity"])
                tables.add(table["id"])
            pairs = raw.get("combinable", [])
            if not isinstance(pairs, list) or any(not isinstance(pair, list) or len(pair) != 2 or any(not isinstance(item, str) or item not in tables for item in pair) or pair[0] == pair[1] for pair in pairs):
                fail(422, "validation_failed")
            result["restaurants"][rid] = copy.deepcopy(raw)
        old = globals()["STATE"]
        globals()["STATE"] = result
        try:
            for raw in fixture["reservations"]:
                rid = raw["id"]
                ref = raw["reference"]
                uid = raw["user_id"]
                if not isinstance(rid, str) or not opaque(rid) or not isinstance(ref, str) or not (6 <= len(ref) <= 12 and re.fullmatch(r"[A-Z0-9]+", ref)) or ref in result["reservations"] or uid not in result["users"]:
                    fail(422, "validation_failed")
                restaurant = restaurant_for(raw["restaurant_id"])
                ids, _ = select_tables(restaurant, raw)
                fields = validate_booking(restaurant, ids, raw["starts_at_local"], raw["party_size"])
                start, end = datetime.fromisoformat(fields["starts_at"]).astimezone(timezone.utc), datetime.fromisoformat(fields["ends_at"]).astimezone(timezone.utc)
                status = raw.get("status", "confirmed")
                if status not in ("confirmed", "cancelled"):
                    fail(422, "validation_failed")
                if status == "confirmed" and any(occupied(raw["restaurant_id"], table_id, start, end) for table_id in ids):
                    fail(422, "validation_failed")
                result["reservations"][ref] = {"reservation_id": rid, "reference": ref, "restaurant_id": raw["restaurant_id"], "user_id": uid, "status": status, "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **fields}
        finally:
            globals()["STATE"] = old
    except (KeyError, TypeError, ValueError, ZoneInfoNotFoundError, Problem):
        fail(422, "validation_failed")
    return result


def validate_import(document):
    if not isinstance(document, dict) or document.get("track") != "tablekeeper" or type(document.get("format_version")) is not int or document["format_version"] != 1:
        fail(422, "validation_failed")
    state = document.get("state")
    if not isinstance(state, dict) or set(state) != set(blank_state()) or any(not isinstance(value, dict) for value in state.values()):
        fail(422, "validation_failed")
    try:
        emails = set()
        for uid, user in state["users"].items():
            if uid != user["id"] or not opaque(uid) or not EMAIL.fullmatch(user["email"]) or not isinstance(user["display_name"], str) or user["email"] in emails:
                fail(422, "validation_failed")
            emails.add(user["email"])
            if len(bytes.fromhex(user["salt"])) != 16 or len(bytes.fromhex(user["hash"])) != 32:
                fail(422, "validation_failed")
        for rid, rest in state["restaurants"].items():
            if rid != rest["id"] or not opaque(rid) or not isinstance(rest["name"], str):
                fail(422, "validation_failed")
            ZoneInfo(rest["timezone"])
            integer(rest["slot_minutes"])
            integer(rest["reservation_duration_minutes"])
            if type(rest["cancellation_cutoff_minutes"]) is not int or rest["cancellation_cutoff_minutes"] < 0:
                fail(422, "validation_failed")
            if not isinstance(rest["opening_hours"], list) or not isinstance(rest["tables"], list):
                fail(422, "validation_failed")
            days = set()
            for h in rest["opening_hours"]:
                if h["weekday"] not in WEEKDAYS or h["weekday"] in days or min_of_clock(h["opens"]) >= min_of_clock(h["closes"]):
                    fail(422, "validation_failed")
                days.add(h["weekday"])
            tables = set()
            for table in rest["tables"]:
                opaque(table["id"])
                if table["id"] in tables or not isinstance(table["label"], str):
                    fail(422, "validation_failed")
                integer(table["capacity"])
                tables.add(table["id"])
            pairs = rest.get("combinable", [])
            if not isinstance(pairs, list) or any(not isinstance(pair, list) or len(pair) != 2 or any(not isinstance(item, str) or item not in tables for item in pair) or pair[0] == pair[1] for pair in pairs):
                fail(422, "validation_failed")
        reservation_ids = set()
        confirmed = []
        for ref, res in state["reservations"].items():
            if ref != res["reference"] or not 6 <= len(ref) <= 12 or not re.fullmatch(r"[A-Z0-9]+", ref) or res["user_id"] not in state["users"] or res["restaurant_id"] not in state["restaurants"] or res["status"] not in ("confirmed", "cancelled"):
                fail(422, "validation_failed")
            opaque(res["reservation_id"])
            if res["reservation_id"] in reservation_ids:
                fail(422, "validation_failed")
            reservation_ids.add(res["reservation_id"])
            for key in ("starts_at", "ends_at", "created_at"):
                if datetime.fromisoformat(res[key]).tzinfo is None:
                    fail(422, "validation_failed")
            restaurant = state["restaurants"][res["restaurant_id"]]
            if "table_id" in res and "table_ids" in res and res["table_ids"] != [res["table_id"]]:
                fail(422, "validation_failed")
            ids, _ = select_tables(restaurant, {"table_ids": res["table_ids"]} if "table_ids" in res else {"table_id": res["table_id"]})
            fields = validate_booking(restaurant, ids, res["starts_at_local"], res["party_size"])
            if any(res.get(key) != value for key, value in fields.items() if key != "table_ids" or "table_ids" in res):
                fail(422, "validation_failed")
            res["table_ids"] = ids
            if res["status"] == "confirmed":
                confirmed.append(res)
        for index, reservation in enumerate(confirmed):
            start, end = interval(reservation)
            for other in confirmed[:index]:
                if reservation["restaurant_id"] == other["restaurant_id"] and set(tables_of(reservation)) & set(tables_of(other)):
                    old_start, old_end = interval(other)
                    if start < old_end and old_start < end:
                        fail(422, "validation_failed")
        for token, uid in state["tokens"].items():
            if not isinstance(token, str) or uid not in state["users"]:
                fail(422, "validation_failed")
        for receipt in state["receipts"].values():
            if not isinstance(receipt["body"], str) or not isinstance(receipt["response"], dict):
                fail(422, "validation_failed")
            json.loads(receipt["body"])
    except (KeyError, TypeError, ValueError, ZoneInfoNotFoundError, Problem):
        fail(422, "validation_failed")
    return copy.deepcopy(state)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format_string, *args):
        pass

    def respond(self, status, body=None):
        payload = b"" if body is None else json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > 10_000_000:
                fail(400, "malformed_request")
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                fail(400, "malformed_request")
            return body
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            fail(400, "malformed_request")

    def user(self):
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or not auth[7:] or " " in auth[7:]:
            fail(401, "unauthenticated")
        user_id = STATE["tokens"].get(auth[7:])
        if user_id is None:
            fail(401, "unauthenticated")
        return user_id

    def idempotency(self):
        key = self.headers.get("Idempotency-Key")
        if not key:
            fail(400, "missing_idempotency_key")
        if len(key) > 255:
            fail(422, "validation_failed")
        return key

    def handle_method(self):
        global STATE
        method = self.command
        url = urlsplit(self.path)
        path = unquote(url.path)
        if method == "GET" and path in ("/", "/signup", "/login", "/lookup", "/app.js", "/style.css"):
            name = "index.html" if path in ("/", "/signup", "/login", "/lookup") else path[1:]
            content_type = "text/html" if name.endswith(".html") else "text/javascript" if name.endswith(".js") else "text/css"
            return 200, (open(os.path.join(os.path.dirname(__file__), name), "rb").read(), content_type)
        with LOCK:
            if method == "GET" and path == "/health":
                return 200, {"status": "ok"}
            if method == "POST" and path == "/_test/reset":
                STATE = validate_fixture(self.body())
                return 204, None
            if method == "GET" and path == "/_test/export":
                return 200, {"track": "tablekeeper", "format_version": 1, "state": copy.deepcopy(STATE)}
            if method == "POST" and path == "/_test/import":
                STATE = validate_import(self.body())
                return 204, None
            if method == "POST" and path in ("/auth/signup", "/auth/login"):
                body = self.body()
                email = text_field(body, "email")
                password = text_field(body, "password")
                if path == "/auth/signup":
                    name = text_field(body, "display_name")
                    if not EMAIL.fullmatch(email) or len(password) < 8:
                        fail(422, "validation_failed")
                    if any(u["email"] == email for u in STATE["users"].values()):
                        fail(409, "email_taken")
                    uid = "u_" + secrets.token_hex(12)
                    STATE["users"][uid] = {"id": uid, "email": email, "display_name": name, **hash_password(password)}
                    return 201, {"user_id": uid, "display_name": name, "token": issue_token(uid)}
                user = next((u for u in STATE["users"].values() if u["email"] == email), None)
                if user is None or not verify_password(password, user):
                    fail(401, "unauthenticated")
                return 200, {"user_id": user["id"], "display_name": user["display_name"], "token": issue_token(user["id"])}
            if method == "GET" and path == "/restaurants":
                return 200, {"restaurants": [{key: r[key] for key in ("id", "name", "timezone")} for r in STATE["restaurants"].values()]}
            if method == "GET" and path.startswith("/restaurants/"):
                return 200, copy.deepcopy(restaurant_for(path[len("/restaurants/"):]))
            if method == "GET" and path == "/availability":
                params = parse_qs(url.query, keep_blank_values=True)
                rid, day_text, size_text = (params.get(k, [None])[0] for k in ("restaurant_id", "date", "party_size"))
                if rid is None or day_text is None or size_text is None:
                    fail(422, "validation_failed")
                restaurant = restaurant_for(opaque(rid))
                day = parse_date(day_text)
                if not re.fullmatch(r"[0-9]+", size_text):
                    fail(422, "validation_failed")
                party = integer(int(size_text), endpoint_party=True)
                hours = next((h for h in restaurant["opening_hours"] if h["weekday"] == WEEKDAYS[day.weekday()]), None)
                slots = []
                if hours:
                    opening, closing = min_of_clock(hours["opens"]), min_of_clock(hours["closes"])
                    for minute in range(opening, closing - restaurant["reservation_duration_minutes"] + 1, restaurant["slot_minutes"]):
                        local = datetime.combine(day, time(minute // 60, minute % 60))
                        try:
                            aware = resolve_local(local, restaurant["timezone"])
                        except Problem as error:
                            if error.code == "invalid_local_time":
                                continue
                            raise
                        end = aware.astimezone(timezone.utc) + timedelta(minutes=restaurant["reservation_duration_minutes"])
                        start = aware.astimezone(timezone.utc)
                        available = [table["id"] for table in restaurant["tables"] if table["capacity"] >= party and not occupied(rid, table["id"], start, end)]
                        options = [{"table_ids": [table["id"]], "capacity": table["capacity"]} for table in restaurant["tables"] if table["id"] in available]
                        by_id = {table["id"]: table for table in restaurant["tables"]}
                        for pair in restaurant.get("combinable", []):
                            if sum(by_id[item]["capacity"] for item in pair) >= party and all(not occupied(rid, item, start, end) for item in pair):
                                options.append({"table_ids": list(pair), "capacity": sum(by_id[item]["capacity"] for item in pair)})
                        slots.append({"starts_at_local": local.strftime("%Y-%m-%dT%H:%M"), "starts_at": aware.isoformat(timespec="seconds"), "available_table_ids": available, "available_options": options})
                return 200, {"restaurant_id": rid, "date": day_text, "timezone": restaurant["timezone"], "slots": slots}
            user_id = self.user()
            if method == "GET" and path == "/reservations":
                reservations = [public_reservation(r) for r in STATE["reservations"].values() if r["user_id"] == user_id]
                reservations.sort(key=lambda r: datetime.fromisoformat(r["starts_at"]).astimezone(timezone.utc), reverse=True)
                return 200, {"reservations": reservations}
            if method == "POST" and path in ("/reservations", "/reservation-moves"):
                body = self.body()
                key = self.idempotency()
                old = replay(user_id, path, key, body)
                if old is not None:
                    return 200, old
                if path == "/reservations":
                    rid = text_field(body, "restaurant_id")
                    local = body.get("starts_at_local")
                    party = body.get("party_size")
                    restaurant = restaurant_for(opaque(rid))
                    ids, _ = select_tables(restaurant, body)
                    fields = validate_booking(restaurant, ids, local, party)
                    start = datetime.fromisoformat(fields["starts_at"]).astimezone(timezone.utc)
                    end = datetime.fromisoformat(fields["ends_at"]).astimezone(timezone.utc)
                    if any(occupied(rid, item, start, end) for item in ids):
                        fail(409, "table_unavailable")
                    reference = secrets.token_hex(5).upper()
                    while reference in STATE["reservations"]:
                        reference = secrets.token_hex(5).upper()
                    reservation = {"reservation_id": "res_" + secrets.token_hex(12), "reference": reference, "restaurant_id": rid, "user_id": user_id, "status": "confirmed", "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **fields}
                    STATE["reservations"][reference] = reservation
                    result = public_reservation(reservation)
                    record(user_id, path, key, body, result)
                    return 201, result
                moves = body.get("moves")
                if not isinstance(moves, list) or not 1 <= len(moves) <= 8:
                    fail(422, "validation_failed")
                refs = []
                for item in moves:
                    if not isinstance(item, dict) or not isinstance(item.get("reference"), str) or not item["reference"] or len(item["reference"]) > 64 or item["reference"] in refs:
                        fail(422, "validation_failed")
                    refs.append(item["reference"])
                proposed = []
                restaurant_id = None
                for item in moves:
                    current = owned(item["reference"], user_id)
                    if restaurant_id is None:
                        restaurant_id = current["restaurant_id"]
                    elif restaurant_id != current["restaurant_id"]:
                        fail(422, "validation_failed")
                    if current["status"] == "cancelled":
                        fail(409, "reservation_cancelled")
                    cutoff(current)
                    fields = changes(current, item)
                    proposed.append(amended(current, fields))
                for candidate in proposed:
                    start, end = interval(candidate)
                    if any(occupied(candidate["restaurant_id"], item, start, end, exclude=refs) for item in tables_of(candidate)):
                        fail(409, "table_unavailable")
                for i, candidate in enumerate(proposed):
                    a, b = interval(candidate)
                    for other in proposed[:i]:
                        c, d = interval(other)
                        if set(tables_of(candidate)) & set(tables_of(other)) and a < d and c < b:
                            fail(409, "table_unavailable")
                for candidate in proposed:
                    STATE["reservations"][candidate["reference"]] = candidate
                result = {"reservations": [public_reservation(r) for r in proposed]}
                record(user_id, path, key, body, result)
                return 201, result
            match = re.fullmatch(r"/reservations/([^/]+)(/cancel)?", path)
            if match:
                reservation = owned(match.group(1), user_id)
                if method == "GET" and match.group(2) is None:
                    return 200, public_reservation(reservation)
                if method == "POST" and match.group(2) == "/cancel":
                    if reservation["status"] == "cancelled":
                        return 200, public_reservation(reservation)
                    cutoff(reservation)
                    reservation["status"] = "cancelled"
                    return 200, public_reservation(reservation)
                if method == "PATCH" and match.group(2) is None:
                    body = self.body()
                    if reservation["status"] == "cancelled":
                        fail(409, "reservation_cancelled")
                    cutoff(reservation)
                    fields = changes(reservation, body)
                    candidate = amended(reservation, fields)
                    start, end = interval(candidate)
                    if any(occupied(candidate["restaurant_id"], item, start, end, exclude=(reservation["reference"],)) for item in tables_of(candidate)):
                        fail(409, "table_unavailable")
                    STATE["reservations"][reservation["reference"]] = candidate
                    return 200, public_reservation(candidate)
            fail(404, "not_found")

    def dispatch(self):
        try:
            status, body = self.handle_method()
            if isinstance(body, tuple):
                payload, content_type = body
                self.send_response(status)
                self.send_header("Content-Type", content_type + "; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            else:
                self.respond(status, body)
        except Problem as error:
            self.respond(error.status, {"error": {"code": error.code, "message": error.code.replace("_", " ")}})
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            self.respond(400, {"error": {"code": "malformed_request", "message": "Invalid request"}})

    do_GET = dispatch
    do_POST = dispatch
    do_PATCH = dispatch


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8080"))), Handler).serve_forever()
