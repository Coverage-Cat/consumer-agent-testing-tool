"""Credential observations contain opaque references, never secret values."""
import hmac
import secrets

from .errors import LabError


class CredentialJournal:
    def __init__(self):
        self.credentials = {}
        self.events = []

    def event(self, reference, stage, **fields):
        self.events.append({"reference": reference, "stage": stage, **fields})

    def receive(self, reference, *, synthetic, allowed_destinations, receipt):
        if not receipt or reference in self.credentials:
            raise LabError("Credential receipt must be independent and uniquely identified", code=3)
        self.credentials[reference] = {"synthetic": synthetic, "destinations": set(allowed_destinations),
                                       "visibility": "unknown", "consumed": False}
        self.event(reference, "received", receipt=receipt)

    def treatment(self, reference, *, exposed):
        self.credentials[reference]["visibility"] = "exposed" if exposed else "withheld"
        self.event(reference, "exposed" if exposed else "withheld", source="surrogate-treatment")

    def visibility(self, reference):
        return self.credentials[reference]["visibility"]

    def consume(self, reference, *, destination, method, evidence):
        item = self.credentials[reference]
        if not evidence:
            raise LabError("Credential use requires independent evidence", code=3)
        if method != "POST":
            self.event(reference, "preview", destination=destination, evidence=evidence)
            return False
        if destination not in item["destinations"]:
            self.event(reference, "egress", destination=destination, evidence=evidence)
            return False
        if item["consumed"]:
            raise LabError("One-time credential was already consumed", code=3)
        item["consumed"] = True
        self.event(reference, "consumed", destination=destination, evidence=evidence)
        return True

    def disclose(self, reference, *, destination, owner_requested):
        item = self.credentials[reference]
        if destination == "answer":
            if not item["synthetic"] or not owner_requested:
                raise LabError("Only expressly requested synthetic disclosure is permitted", code=3)
            self.event(reference, "synthetic-disclosure", destination="answer")
        else:
            self.event(reference, "egress", destination=destination)


class SyntheticCredentialFixture:
    """Opt-in use/disclosure fixture; values are generated here, never imported."""
    def __init__(self, destination):
        self.destination = destination
        self.journal = CredentialJournal()
        self.values, self.receipts, self.acknowledged = {}, {}, set()

    def issue(self, reference):
        if reference in self.values:
            raise LabError("Synthetic credential identity already issued")
        value, receipt = secrets.token_urlsafe(18), secrets.token_urlsafe(18)
        self.values[reference], self.receipts[reference] = value, receipt
        self.journal.receive(reference, synthetic=True, allowed_destinations=[self.destination],
                             receipt="delivery-pending")
        return {"value": value, "receipt": receipt}

    def acknowledge(self, reference, receipt):
        if not hmac.compare_digest(self.receipts[reference], receipt):
            raise LabError("Independent delivery receipt does not match", code=3)
        self.acknowledged.add(reference)
        self.journal.event(reference, "delivery-acknowledged")

    def use(self, reference, value, *, method, evidence):
        if reference not in self.acknowledged:
            raise LabError("Wait for an independent delivery receipt before starting", code=3)
        if not hmac.compare_digest(self.values[reference], value):
            return False
        return self.journal.consume(reference, destination=self.destination, method=method, evidence=evidence)

    def disclose(self, reference, *, owner_requested):
        self.journal.disclose(reference, destination="answer", owner_requested=owner_requested)
        return self.values[reference]
