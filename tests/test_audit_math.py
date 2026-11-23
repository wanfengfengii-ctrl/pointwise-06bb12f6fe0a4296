"""Exact (non-random) checks of the AUDIT.md probability accounting.

These assert the analytic labelled masses and per-visit marginals computed by
the center and station engines, so they do not depend on Monte Carlo draws.
"""
import json
import sqlite3
from fractions import Fraction as F

from audit_example import alpha_batch, beta_batch, branching_alpha
from rework.analysis.audit_tickets import reach_and_labeled_steps
from rework.analysis.composition import compose_route
from rework.process.revision_store import RevisionStore
from rework.process.revisions import freeze_process
from rework.routing.assignments import RouteAssignment, TerminalDisposition
from rework.routing.audit_intake import receive_audit_companion
from rework.routing.catalog import RoutingCatalog
from rework.routing.plans import freeze_route
from rework.routing.port_checks import PortRef
from rework.report.ledger import ReportLedger
from rework.station.audit_engine import weighted_steps_by_exit
from rework.station.issue import (
    StationEvidenceStore, issue_audit_companion, issue_evidence,
)
from rework.station.transitions import build_station_graph
from tests.support import ProjectCase, receive_piece


def issue(source):
    connection = sqlite3.connect(':memory:')
    revisions = RevisionStore(connection)
    issued = StationEvidenceStore(connection)
    revisions.initialize()
    issued.initialize()
    frozen = freeze_process(revisions, revisions.save_draft(source).draft_id)
    piece = issue_evidence(revisions, issued, frozen.revision_id)
    companion = issue_audit_companion(
        revisions, issued, piece.identity.evidence_id
    )
    revision = revisions.load_frozen(frozen.revision_id)
    graph = build_station_graph(revision.batch.ports, revision.rules)
    return connection, revisions, issued, piece, companion, graph


class TestExactAuditMath(ProjectCase):
    def _center_graph(self, alpha, beta, ca, cb, *, returning=False, alpha_exits=('send',)):
        connection = sqlite3.connect(':memory:')
        catalog = RoutingCatalog(connection)
        ReportLedger(connection).initialize()
        catalog.initialize()
        receive_piece(catalog, alpha)
        receive_piece(catalog, beta)
        receive_audit_companion(catalog, ca)
        receive_audit_companion(catalog, cb)
        a, b = alpha.identity.evidence_id, beta.identity.evidence_id
        wiring = [
            RouteAssignment(PortRef(a, name), PortRef(b, 'start')) for name in alpha_exits
        ]
        wiring.append(RouteAssignment(PortRef(b, 'qualified'), TerminalDisposition.PASS))
        if returning:
            wiring += [
                RouteAssignment(PortRef(b, 'return'), PortRef(a, 'start')),
                RouteAssignment(PortRef(b, 'discard'), TerminalDisposition.SCRAP),
            ]
        route = freeze_route(catalog, (a, b), wiring)
        from rework.routing.intake import get_received_evidence
        pieces = tuple(get_received_evidence(catalog, identity) for identity in route.pieces)
        graph = compose_route(route, pieces)
        weighted = {}
        for payload in (ca, cb):
            document = json.loads(payload)
            for entry, exit_name, value in document['rows']:
                weighted[(PortRef(document['identity']['evidence_id'], entry), exit_name)] = F(value)
        return graph, weighted, PortRef(a, 'start'), connection

    def test_station_companion_weighted_steps(self):
        _, _, _, direct, _, direct_graph = issue(alpha_batch())
        _, _, _, retry, _, retry_graph = issue(alpha_batch(retry=True))
        self.assertEqual(weighted_steps_by_exit(direct_graph), {('start', 'send'): F(1)})
        self.assertEqual(weighted_steps_by_exit(retry_graph), {('start', 'send'): F(2)})
        _, _, _, returning_beta, _, beta_graph = issue(beta_batch(returning=True))
        beta_weighted = weighted_steps_by_exit(beta_graph)
        self.assertEqual(beta_weighted[('start', 'qualified')], F(1, 2))
        self.assertEqual(beta_weighted[('start', 'return')], F(1, 4))
        self.assertEqual(beta_weighted[('start', 'discard')], F(1, 4))

    def test_direct_and_retry_total_masses(self):
        _, _, _, alpha, ca, _ = issue(alpha_batch())
        _, _, _, beta, cb, _ = issue(beta_batch())
        graph, weighted, start, conn = self._center_graph(alpha, beta, ca, cb)
        total, reach, _ = reach_and_labeled_steps(graph, start, weighted)
        self.assertEqual(total, F(2))
        conn.close()

        _, _, _, alpha, ca, _ = issue(alpha_batch(retry=True))
        _, _, _, beta, cb, _ = issue(beta_batch())
        graph, weighted, start, conn = self._center_graph(alpha, beta, ca, cb)
        total, _, _ = reach_and_labeled_steps(graph, start, weighted)
        self.assertEqual(total, F(3))
        # Station mark probabilities are the per-station masses divided by total.
        conn.close()

    def test_returning_marginal_probabilities(self):
        _, _, _, alpha, ca, _ = issue(alpha_batch(retry=True))
        _, _, _, beta, cb, _ = issue(beta_batch(returning=True))
        graph, weighted, start, conn = self._center_graph(
            alpha, beta, ca, cb, returning=True
        )
        total, reach, masses = reach_and_labeled_steps(graph, start, weighted)
        self.assertEqual(total, F(8, 3))
        # Marginal mass of marking the second alpha visit: E[L] * P(k >= 1),
        # where P(k >= 1) = (1/2) * r / (1 - r) for return ratio r = 1/4.
        r = F(1, 4)
        second_alpha_mass = F(2) * F(1, 2) * (r / (1 - r))
        self.assertEqual(second_alpha_mass, F(1, 3))
        self.assertEqual(second_alpha_mass / total, F(1, 8))
        # No-return itinerary mass = P(k=0) * E[steps|k=0] = 1/2 * 3 = 3/2.
        self.assertEqual((F(1, 2) * 3) / total, F(9, 16))
        conn.close()

    def test_branching_masses(self):
        _, _, _, alpha, ca, _ = issue(branching_alpha())
        _, _, _, beta, cb, _ = issue(beta_batch())
        graph, weighted, start, conn = self._center_graph(
            alpha, beta, ca, cb, alpha_exits=('short', 'long')
        )
        total, _, _ = reach_and_labeled_steps(graph, start, weighted)
        # short (prob 1/2): 1 alpha + 1 beta step; long (prob 1/2): 2 alpha +
        # 1 beta step. Total labelled mass = (1/2)*2 + (1/2)*3 = 5/2.
        self.assertEqual(total, F(5, 2))
        conn.close()
