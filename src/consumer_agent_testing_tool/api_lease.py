"""Private retained-test-state lease extension for developer API adapters.

This is a lifecycle primitive, not a public-response reconciler. Reconnecting
must never reset the developer's service or authorize a saved tool proposal.
"""
import copy
import os
import stat
import uuid
from datetime import UTC, datetime

from .errors import LabError, Unsupported
from .serialization import digest, load
from .surface import atomic_json

CAPABILITY = "retained-trial-v1"
CAPABILITIES = {CAPABILITY, "acquire_retained", "resume", "suspend"}


class RetainedApiLease:
    def __init__(self, client):
        self.client = client
        self.session = client.session
        self.path = self.session.private / "api-lease.private.json"
        self.record = None
        self.clock = None

    def save(self):
        atomic_json(self.path, self.record)

    def load(self):
        try:
            info = self.path.lstat()
        except OSError as error:
            raise LabError("Retained API lease checkpoint is missing") from error
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise LabError("Retained API lease checkpoint must be an owner-only regular file")
        record = load(self.path)
        fields = {"schema_version", "identity", "initial_state_sha256", "expires_at",
                  "lease_id", "state_id", "status", "user_profile"}
        if not isinstance(record, dict) or set(record) != fields \
                or record["schema_version"] != 1 or record["identity"] != self.client.identity \
                or record["initial_state_sha256"] != digest(self.client.scenario["initial_state"]) \
                or record["expires_at"] != self.expires_at \
                or record["status"] not in ("active", "suspending", "suspended") \
                or not isinstance(record["user_profile"], dict) \
                or any(not isinstance(record[key], str) or not record[key]
                       for key in ("lease_id", "state_id")):
            raise LabError("Retained API lease identity, state, or deadline does not match")
        self.record = record
        self.client.evidence.redactor.secrets.extend([record["lease_id"], record["state_id"]])

    @property
    def expires_at(self):
        return datetime.fromtimestamp(self.session.state["deadline"], UTC).isoformat()

    async def open(self, metadata):
        if not CAPABILITIES <= set(metadata["capabilities"]):
            raise Unsupported("Private API adapter lacks retained-trial-v1 lifecycle capabilities")
        self.session.remaining_seconds()
        resuming = self.session.resuming
        if resuming:
            self.load()
        else:
            if self.path.exists() or self.path.is_symlink():
                raise LabError("Existing retained API lease requires explicit continuation")
            self.record = {
                "schema_version": 1, "identity": copy.deepcopy(self.client.identity),
                "initial_state_sha256": digest(self.client.scenario["initial_state"]),
                "expires_at": self.expires_at, "lease_id": uuid.uuid4().hex,
                "state_id": None, "status": "acquiring", "user_profile": None,
            }
            self.client.evidence.redactor.secrets.append(self.record["lease_id"])
            self.save()  # A missing acquisition acknowledgment must never cause a second reset.
        args = {key: self.record[key] for key in
                ("lease_id", "initial_state_sha256", "expires_at")}
        if resuming:
            args["state_id"] = self.record["state_id"]
        else:
            args["initial_state"] = self.client.scenario["initial_state"]
        result = await self.client.rpc("resume" if resuming else "acquire_retained", **args)
        fields = {"lease_id", "state_id", "initial_state_sha256", "expires_at",
                  "clock", "user_profile", "grants_retired",
                  "state_preserved" if resuming else "reset_completed"}
        if not isinstance(result, dict) or set(result) != fields:
            raise LabError("Private API adapter did not acknowledge the retained lease")
        # Register private identifiers before any later diagnostic/report handling.
        if isinstance(result.get("state_id"), str) and result["state_id"]:
            self.client.evidence.redactor.secrets.append(result["state_id"])
        matches = ("lease_id", "initial_state_sha256", "expires_at",
                   *(("state_id", "user_profile") if resuming else ()))
        if any(result[key] != self.record[key] for key in matches) \
                or not isinstance(result["state_id"], str) or not result["state_id"] \
                or not isinstance(result["user_profile"], dict) \
                or result["grants_retired"] is not True \
                or result["state_preserved" if resuming else "reset_completed"] is not True:
            raise LabError("Private API adapter did not confirm retained state and retired authority")
        if not resuming and result["clock"] != self.client.scenario["initial_state"]["clock"]:
            raise LabError("Private API adapter did not confirm the requested reset clock")
        # Resume uses the durable scenario clock, which may differ from the initial clock.
        if resuming:
            path = self.session.private / "scenario.private.json"
            runtime = load(path) if path.exists() else {
                "clock": self.client.scenario["initial_state"]["clock"], "events": {}}
            clocks = {runtime["clock"]}
            # The target may have applied a durable clock event before its reply
            # was lost. Only that exact pending event permits a different clock.
            clocks.update(row["event"]["args"]["clock"] for row in runtime.get("events", {}).values()
                          if row["status"] == "pending" and row["event"]["action"] == "advance_time")
            if result["clock"] not in clocks:
                raise LabError("Retained API fixture clock differs from the scenario checkpoint")
        self.record.update(status="active", state_id=result["state_id"],
                           user_profile=copy.deepcopy(result["user_profile"]))
        self.save()
        self.clock = result["clock"]
        self.client.acquired = True
        self.client.evidence.event("api_lease_resumed" if resuming else "fixture_reset", {
            "retained": True, "clock": result["clock"], "grants_retired": True,
            "state_preserved" if resuming else "reset_completed": True,
            "expires_at": self.expires_at,
        }, provenance="private developer lifecycle acknowledgment; not public outcome evidence")
        return copy.deepcopy(result["user_profile"])

    def verify_clock(self, runtime):
        if runtime.state["clock"] != self.clock:
            raise LabError("Recovered scenario clock differs from the retained API state")

    async def suspend(self):
        if not self.client.acquired or not self.record:
            raise LabError("Cannot suspend an unacknowledged retained API lease")
        self.record["status"] = "suspending"
        self.save()
        args = {key: self.record[key] for key in ("lease_id", "state_id", "expires_at")}
        result = await self.client.rpc("suspend", cleanup=True, **args)
        if result != {**args, "suspended": True, "grants_retired": True}:
            raise LabError("Private API adapter did not confirm suspension and retired authority")
        self.record["status"] = "suspended"
        self.save()
        self.client.acquired = False
        self.client.evidence.event("api_lease_suspended", {
            "state_preserved": True, "grants_retired": True, "expires_at": self.expires_at},
            provenance="private developer lifecycle acknowledgment")

    def released(self):
        self.record["status"] = "released"
        self.save()
