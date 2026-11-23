"""Sample observations and callback fixture for the audit-ticket handoff."""
import json
import random
import sqlite3
from contextlib import closing
from fractions import Fraction as F

from rework.analysis.assessment import publish_assessment, publish_audit_ticket
from rework.process.revision_store import RevisionStore
from rework.process.revisions import freeze_process
from rework.report.ledger import ReportLedger
from rework.routing.audit_intake import receive_audit_companion
from rework.routing.catalog import RoutingCatalog
from rework.station.issue import (
    StationEvidenceStore, issue_audit_companion, issue_evidence,
)
from tests.support import batch, port, receive_piece, two_station_route, values


def alpha_batch(retry=False):
    rows = [("bench", "send", 0)]
    if retry:
        rows.append(("bench", "bench", 0))
    return batch("Alpha", rows)


def beta_batch(returning=False):
    rows = [("bench", "qualified", 0)]
    exits = ("qualified",)
    if returning:
        rows.extend([("bench", "qualified", 0), ("bench", "return", 0), ("bench", "discard", 0)])
        exits += ("return", "discard")
    return batch("Beta", rows, exits=exits)


def branching_alpha():
    return batch("Alpha", [("bench", "short", 0), ("bench", "aux", 0), ("aux", "long", 0)],
                 exits=("short", "long"), workstations=("bench", "aux"))


class ScriptedRandBelow:
    def __init__(self, draws):
        self.draws = iter(draws)
        self.bounds = []

    def __call__(self, stop):
        if type(stop) is not int or stop <= 0:
            raise AssertionError(f"invalid draw bound: {stop!r}")
        self.bounds.append(stop)
        value = next(self.draws)
        if type(value) is not int or not 0 <= value < stop:
            raise AssertionError(f"scripted draw {value!r} outside [0, {stop})")
        return value


def issue_v1(source):
    with closing(sqlite3.connect(":memory:")) as connection:
        revisions = RevisionStore(connection)
        revisions.initialize()
        issued = StationEvidenceStore(connection)
        issued.initialize()
        frozen = freeze_process(revisions, revisions.save_draft(source).draft_id)
        piece = issue_evidence(revisions, issued, frozen.revision_id)
        companion = issue_audit_companion(
            revisions, issued, piece.identity.evidence_id
        )
        return piece, companion


def _weighted(companion_payload):
    return {
        (entry, exit_name): F(weighted)
        for entry, exit_name, weighted in json.loads(companion_payload)["rows"]
    }


def main():
    direct_pair, retry_pair = issue_v1(alpha_batch()), issue_v1(alpha_batch(retry=True))
    beta_pair, returns_pair = issue_v1(beta_batch()), issue_v1(beta_batch(returning=True))
    direct, retry = direct_pair[0], retry_pair[0]
    beta, returns = beta_pair[0], returns_pair[0]
    if direct.rows != retry.rows or len(retry.rows) != 1:
        raise AssertionError("V1 does not show the expected zero-minute evidence collision")
    row = retry.rows[0]
    if (row.probability, row.first, row.second) != (F(1), F(0), F(0)):
        raise AssertionError("unexpected V1 moments in the sample")
    # Companion weighted step masses: direct alpha is 1, retrying alpha is 2.
    if _weighted(direct_pair[1])[("start", "send")] != F(1):
        raise AssertionError("direct alpha companion weighted steps must be 1")
    if _weighted(retry_pair[1])[("start", "send")] != F(2):
        raise AssertionError("retrying alpha companion weighted steps must be 2")
    actual = {}
    with closing(sqlite3.connect(":memory:")) as connection:
        catalog, ledger = RoutingCatalog(connection), ReportLedger(connection)
        catalog.initialize()
        ledger.initialize()
        pairs = (direct_pair, retry_pair, beta_pair, returns_pair)
        for piece, companion in pairs:
            receive_piece(catalog, piece)
            receive_audit_companion(catalog, companion)
        for label, alpha, right, returning, expected_pass in (
            ("direct", direct, beta, False, F(1)),
            ("private_retry", retry, beta, False, F(1)),
            ("public_return", retry, returns, True, F(2, 3)),
        ):
            route = two_station_route(catalog, alpha, right, returning=returning)
            result = values(publish_assessment(catalog, ledger, route.route_id, port(alpha)))
            if result != (expected_pass, F(0), F(0)):
                raise AssertionError(f"unexpected V1 assessment for {label}: {result}")
            actual[label] = list(map(str, result))
        # A route without any qualifying path yields no audit ticket.
        returning_route = two_station_route(catalog, retry, returns, returning=True)
        ticket = publish_audit_ticket(
            catalog, ledger, returning_route.route_id, port(retry),
            random.Random(20261123).randrange,
        )
        if ticket is None:
            raise AssertionError("the returning sample must have qualifying audit paths")
        if ticket["selected"] not in range(len(ticket["visits"])):
            raise AssertionError("audit ticket selected index is outside its visits")
    print(json.dumps({"v1_assessments": actual}, sort_keys=True))


if __name__ == "__main__":
    main()
