"""Shared fakes for the scheduler extension tests; this module holds no tests.

A clocked variant of the in-memory hotel (test_connector_support.Hotel): its own service clock, which the
scheduler moves through /__oracle/clock like the connector fixture's, quotes that expire, bookings that
confirm later, a status read with an ETag analog (if_none_match / not_modified) and status reads that are
rate-limited with a Retry-After delay. Everything else is the lane's own (permissions, runner, answer).
"""
import copy
import json
from datetime import UTC, datetime, timedelta

import httpx
from test_connector_support import ORIGIN, Hotel, scenario, services

START = "2026-11-30T09:00:00-08:00"  # The fixture clock's start in every scheduler scenario here.
STAY = {"check_in": "2026-12-04", "check_out": "2026-12-06", "adults": 2}
QUOTE_EXPIRED = {"pointer": "/content/0/text/error", "value": "quote_expired"}


def stamp(moment):
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


class ClockedHotel(Hotel):
    """The fake hotel on a service clock: quotes expire after `quote_seconds`; with `confirm_after` a booking
    stays pending that long; `etag` adds if_none_match to get_booking; after `limit_after` get_booking calls,
    the next `rate_limited` answer a rate_limited error with retry_after_seconds."""

    def __init__(self, *, now="2026-11-20T17:00:00+00:00", quote_seconds=900, confirm_after=None, etag=False,
                 rate_limited=0, limit_after=0, retry_after=120, **options):
        self.now, self.quote_seconds, self.confirm_after = datetime.fromisoformat(now), quote_seconds, confirm_after
        self.etag, self.rate_limited, self.retry_after, self.clock_posts = etag, rate_limited, retry_after, []
        self.limit_after = limit_after
        super().__init__(**options)
        if etag:
            booking = next(tool for tool in self.catalog if tool["name"] == "get_booking")
            booking["inputSchema"]["properties"]["if_none_match"] = {
                "type": "string", "description": "The etag of an earlier get_booking; an unchanged booking "
                                                 "answers not_modified."}

    def reset(self):
        super().reset()
        self.issued, self.limited_left, self.reads, self.confirm_at = {}, getattr(self, "rate_limited", 0), 0, {}

    @staticmethod
    def reply(data, error=False):
        return {"content": [{"type": "text", "text": json.dumps(data)}], "isError": error,
                **({} if error else {"structuredContent": data})}

    def settle(self):
        for booking in self.bookings:
            if booking["status"] == "pending" and self.now >= self.confirm_at[booking["booking_id"]]:
                booking["status"] = "confirmed"

    def state(self):
        self.settle()
        return {**super().state(), "clock": stamp(self.now)}

    def call(self, name, arguments):
        self.settle()
        if name == "get_quote":
            data = super().call(name, arguments)["structuredContent"]
            data["expires_at"] = stamp(self.now + timedelta(seconds=self.quote_seconds))
            self.issued[data["quote_id"]] = self.now
            return self.reply(data)
        if name == "create_booking":
            issued = self.issued.get(arguments["quote_id"])
            if issued is not None and self.now > issued + timedelta(seconds=self.quote_seconds):
                self.calls.append((name, copy.deepcopy(arguments)))
                return self.reply({"error": "quote_expired", "message": f"Quote {arguments['quote_id']} expired; "
                                                                         "request a new quote."}, error=True)
            result = super().call(name, arguments)
            if self.confirm_after is None or result is None or result.get("isError"):
                return result
            booking = self.bookings[-1]
            booking["status"] = "pending"
            self.confirm_at[booking["booking_id"]] = self.now + timedelta(seconds=self.confirm_after)
            return self.reply({**result["structuredContent"], "status": "pending"})
        if name == "get_booking":
            self.calls.append((name, copy.deepcopy(arguments)))
            self.reads += 1
            if self.limited_left > 0 and self.reads > self.limit_after:
                self.limited_left -= 1
                return self.reply({"error": "rate_limited", "message": "Too many requests; retry later.",
                                   "retry_after_seconds": self.retry_after}, error=True)
            found = next((row for row in self.bookings if row["booking_id"] == arguments["booking_id"]), None)
            if found is None:
                return self.reply({"error": "not_found", "message": "No such booking"}, error=True)
            data = {"booking_id": found["booking_id"], "status": found["status"], "checked_at": stamp(self.now)}
            if self.etag:
                data["etag"] = f"W/{found['booking_id']}-{found['status']}"
                if arguments.get("if_none_match") == data["etag"]:
                    return self.reply({"not_modified": True, "etag": data["etag"]})
            return self.reply(data)
        return super().call(name, arguments)


def clocked_oracle(hotel):
    """The private oracle of test_connector_support plus the connector fixture's clock control."""
    def handler(request):
        path, method = request.url.path, request.method
        if path in ("/__oracle/clock", "/__oracle/advance") and method == "POST":
            body = json.loads(request.content)
            hotel.clock_posts.append(body)
            if "set" in body:
                hotel.now = datetime.fromisoformat(body["set"])
            else:
                hotel.now += timedelta(seconds=body.get("advance_seconds", body.get("seconds", 0)))
            return httpx.Response(200, json={"clock": stamp(hotel.now)})
        if path == "/__oracle/state" and method == "GET":
            return httpx.Response(200, json=hotel.state())
        if path == "/__oracle/reset" and method == "POST":
            hotel.reset()
            return httpx.Response(204)
        return httpx.Response(404)
    return lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)


def clocked_services(hotel):
    made = services(hotel)
    made.http = clocked_oracle(hotel)
    return made


KEEP = object()


def scheduled(jobs=None, *, clock=None, checks=KEEP, answer=KEEP, **config):
    """The booking scenario with a scheduler on the fixture clock (its service clock at /__oracle/clock);
    `checks` and `answer` replace the booking scenario's own (checks=[] for runs that book nothing)."""
    settings = {"jobs": jobs or [{"id": "price-watch", "every": "1h", "max_runs": 2,
                                  "message": "Check the Garden double's price for December 4 to 6 and book it if "
                                             "the total is at most $500."}],
                "advance": [{"url": f"{ORIGIN}/__oracle/clock"}], **config}
    document = scenario(goal="Keep an eye on the Garden double for December 4 to 6 for two adults and book it "
                             "when the total is at most $500.",
                        extensions={"clock": clock or {"now": START}, "scheduler": settings})
    if checks is not KEEP:
        document["checks"] = checks
    if answer is not KEEP:
        document["answer"] = answer
    return document


def user_messages(api, request=-1):
    return [message["content"] for message in api.inputs[request]["messages"] if message["role"] == "user"]
