"""Sample observations and callback fixture for the audit-ticket handoff."""
import json
import sqlite3
from contextlib import closing
from fractions import Fraction as F

from rework.analysis.assessment import publish_assessment
from rework.process.revision_store import RevisionStore
from rework.process.revisions import freeze_process
from rework.report.ledger import ReportLedger
from rework.routing.catalog import RoutingCatalog
from rework.station.issue import StationEvidenceStore, issue_evidence
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
        return issue_evidence(revisions, issued, frozen.revision_id)


def main():
    direct, retry = issue_v1(alpha_batch()), issue_v1(alpha_batch(retry=True))
    beta, returns = issue_v1(beta_batch()), issue_v1(beta_batch(returning=True))
    if direct.rows != retry.rows or len(retry.rows) != 1:
        raise AssertionError("V1 does not show the expected zero-minute evidence collision")
    row = retry.rows[0]
    if (row.probability, row.first, row.second) != (F(1), F(0), F(0)):
        raise AssertionError("unexpected V1 moments in the sample")
    actual = {}
    with closing(sqlite3.connect(":memory:")) as connection:
        catalog, ledger = RoutingCatalog(connection), ReportLedger(connection)
        catalog.initialize()
        ledger.initialize()
        for piece in (direct, retry, beta, returns):
            receive_piece(catalog, piece)
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
    print(json.dumps({"v1_assessments": actual}, sort_keys=True))


if __name__ == "__main__":
    main()
