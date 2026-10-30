"""Explicit clocks, bounded observation windows and restartable schedule records."""
import asyncio
import json
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from typing import Protocol

from .errors import LabError


class Clock(Protocol):
    mode: str

    def utc(self) -> datetime: ...
    def monotonic(self) -> float: ...


class WallClock:
    mode = "wall"

    def utc(self):
        return datetime.now(UTC)

    def monotonic(self):
        return time.monotonic()


class VirtualClock:
    mode = "virtual"

    def __init__(self, start):
        if start.tzinfo is None:
            raise ValueError("Clock start requires a timezone")
        self.start, self.elapsed = start.astimezone(UTC), 0.0

    def utc(self):
        return self.start + timedelta(seconds=self.elapsed)

    def monotonic(self):
        return self.elapsed

    def advance(self, seconds):
        if seconds < 0:
            raise ValueError("Virtual time cannot go backward")
        self.elapsed += seconds


class AvailabilityWindow:
    def __init__(self, clock, *, duration, max_gap):
        if duration <= 0 or max_gap <= 0:
            raise ValueError("Observation windows must be bounded")
        self.clock, self.duration, self.max_gap = clock, duration, max_gap
        self.start, self.last = clock.monotonic(), None
        self.samples, self.interrupted = [], False

    def sample(self, *, route_reachable, authentication_healthy):
        now = self.clock.monotonic()
        if now > self.start + self.duration:
            return False
        gap = now - (self.last if self.last is not None else self.start)
        healthy = route_reachable is True and authentication_healthy is True
        self.interrupted |= not healthy or gap > self.max_gap
        self.samples.append({"at": self.clock.utc().isoformat(), "elapsed": now - self.start,
                             "route": route_reachable, "authentication": authentication_healthy})
        self.last = now
        return True

    @property
    def status(self):
        if self.samples and min(self.clock.monotonic(), self.start + self.duration) - self.last > self.max_gap:
            self.interrupted = True
        return "interrupted" if self.interrupted else "observed-healthy" if self.samples else "unknown"

    @property
    def release_allowed(self):
        return self.status == "observed-healthy" and self.clock.monotonic() < self.start + self.duration

    def record(self):
        return {"clock": self.clock.mode, "status": self.status, "samples": list(self.samples)}


class HttpAvailabilityObserver:
    """Read the served route and authenticated boundary, never process liveness."""
    def __init__(self, window, route_url, authenticated_url, *, headers=None, timeout=5):
        from .connector_scenario import check_url
        check_url(route_url, "availability route")
        check_url(authenticated_url, "availability authentication")
        if not 0 < timeout <= 30:
            raise ValueError("Availability timeout must be within 30 seconds")
        self.window, self.route_url, self.authenticated_url = window, route_url, authenticated_url
        self.headers, self.timeout = dict(headers or {}), timeout

    async def sample(self):
        import httpx
        async with httpx.AsyncClient(trust_env=False, timeout=self.timeout, follow_redirects=False) as client:
            async def healthy(url, headers):
                try:
                    async with asyncio.timeout(self.timeout):
                        async with client.stream("GET", url, headers=headers) as result:
                            return result.status_code == 200
                except (httpx.HTTPError, TimeoutError):
                    return False
            route = await healthy(self.route_url, {})
            authenticated = await healthy(self.authenticated_url, self.headers)
        self.window.sample(route_reachable=route, authentication_healthy=authenticated)
        return self.window.record()

    async def monitor(self, interval=5):
        if (self.window.clock.mode != "wall" or interval <= 0
                or interval + 2 * self.timeout >= self.window.max_gap):
            raise ValueError("HTTP monitoring needs wall time and room for requests within the observation gap")
        while self.window.clock.monotonic() < self.window.start + self.window.duration:
            await self.sample()
            remaining = self.window.start + self.window.duration - self.window.clock.monotonic()
            if remaining > 0:
                await asyncio.sleep(min(interval, remaining))
        return self.window.record()


class DurableSchedule:
    """Record each due attempt before work. A crash cannot replay its slot."""
    def __init__(self, path, clock):
        self.clock = clock
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, mode TEXT, period REAL, due TEXT, until_at TEXT,
                max_actions INTEGER, actions INTEGER DEFAULT 0, attempts INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS observations (job TEXT, kind TEXT, at TEXT, evidence TEXT);
        """)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.db.close()

    def add(self, identifier, *, every, until, max_actions):
        if every <= 0 or max_actions < 1 or until <= self.clock.utc():
            raise ValueError("Schedule needs a future end, positive interval and action limit")
        with self.db:
            self.db.execute("INSERT INTO jobs(id,mode,period,due,until_at,max_actions) VALUES(?,?,?,?,?,?)",
                            (identifier, self.clock.mode, every,
                             (self.clock.utc() + timedelta(seconds=every)).isoformat(),
                             until.isoformat(), max_actions))

    def _job(self, identifier):
        row = self.db.execute("SELECT * FROM jobs WHERE id=?", (identifier,)).fetchone()
        if row is None or row["mode"] != self.clock.mode:
            raise LabError("Unknown schedule or clock treatment changed", code=3)
        return row

    def attempt(self, identifier):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self._job(identifier)
            now, due = self.clock.utc(), datetime.fromisoformat(row["due"])
            if now < due or self.expired(identifier):
                return None
            next_due = due + timedelta(seconds=row["period"])
            # Missed slots do not become a burst of catch-up sends.
            while next_due <= now:
                next_due += timedelta(seconds=row["period"])
            self.db.execute("UPDATE jobs SET due=?,attempts=attempts+1 WHERE id=?",
                            (next_due.isoformat(), identifier))
            self.db.execute("INSERT INTO observations VALUES(?,?,?,?)",
                            (identifier, "attempt", now.isoformat(), json.dumps({"due": due.isoformat()})))
            return row["attempts"] + 1

    def action_allowed(self, identifier):
        row = self._job(identifier)
        return row["actions"] < row["max_actions"] and not self.expired(identifier)

    def record(self, identifier, kind, *, evidence):
        if kind not in ("read", "effect", "cleanup", "end") or not evidence:
            raise ValueError("Schedule observations need an explicit kind and evidence")
        with self.db:
            self._job(identifier)
            if kind == "effect":
                # Record even a late or excessive effect honestly.
                self.db.execute("UPDATE jobs SET actions=actions+1 WHERE id=?", (identifier,))
            self.db.execute("INSERT INTO observations VALUES(?,?,?,?)",
                            (identifier, kind, self.clock.utc().isoformat(), evidence))

    def expired(self, identifier):
        return self.clock.utc() >= datetime.fromisoformat(self._job(identifier)["until_at"])

    def cleaned(self, identifier):
        return bool(self.db.execute("SELECT 1 FROM observations WHERE job=? AND kind='cleanup'",
                                    (identifier,)).fetchone())
