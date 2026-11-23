import json
import random
import sqlite3
from fractions import Fraction as F
from hashlib import sha256

from audit_example import alpha_batch, beta_batch
from rework.analysis.assessment import publish_audit_ticket
from rework.process.revision_store import RevisionStore
from rework.process.revisions import freeze_process, open_correction
from rework.report.ledger import ReportLedger, read_audit_ticket
from rework.routing.assignments import RouteAssignment, TerminalDisposition
from rework.routing.audit_intake import (
    get_received_audit_companion, receive_audit_companion,
)
from rework.routing.catalog import RoutingCatalog
from rework.routing.plans import freeze_route
from rework.routing.port_checks import PortRef
from rework.station.audit_companion import canonical_digest
from rework.station.codec import encode_evidence
from rework.station.issue import (
    StationEvidenceStore, issue_audit_companion, issue_evidence,
    read_station_evidence, redeem_audit_ticket,
)
from tests.support import ProjectCase, batch, port, receive_piece


class UniformRandBelow:
    def __init__(self, seed=987654321):
        self.rng = random.Random(seed)

    def __call__(self, stop):
        return self.rng.randrange(stop)


def issue_with_companion(revisions, issued, source):
    frozen = freeze_process(revisions, revisions.save_draft(source).draft_id)
    piece = issue_evidence(revisions, issued, frozen.revision_id)
    payload = issue_audit_companion(revisions, issued, piece.identity.evidence_id)
    return piece, payload


class TestAuditCompanion(ProjectCase):
    def test_companion_shape_and_determinism(self):
        with self.station('alpha') as (_, revisions, issued):
            piece, payload = issue_with_companion(revisions, issued, alpha_batch(retry=True))
            again = issue_audit_companion(
                revisions, issued, piece.identity.evidence_id
            )
            self.assertEqual(payload, again)
            document = json.loads(payload)
            self.assertEqual(document['kind'], 'audit-companion')
            self.assertEqual(document['identity'], {
                'evidence_id': piece.identity.evidence_id,
                'station': piece.identity.station,
                'revision_id': piece.identity.revision_id,
            })
            self.assertEqual(
                document['v1_sha256'],
                sha256(encode_evidence(piece)).hexdigest(),
            )
            self.assertEqual(document['rows'], [['start', 'send', '2']])
            self.assertEqual(document['digest'], canonical_digest(
                {key: value for key, value in document.items() if key != 'digest'}
            ))

    def test_weighted_steps_at_least_exit_probability(self):
        with self.station('beta') as (_, revisions, issued):
            piece, payload = issue_with_companion(
                revisions, issued, beta_batch(returning=True)
            )
            rows = {(entry, exit_name): value for entry, exit_name, value in
                    json.loads(payload)['rows']}
            probabilities = {(row.entry, row.exit): row.probability for row in piece.rows}
            for key, weighted in rows.items():
                self.assertGreaterEqual(F(weighted), probabilities[key])
            self.assertEqual(rows[('start', 'qualified')], '1/2')
            self.assertEqual(rows[('start', 'return')], '1/4')

    def test_intake_requires_v1_first_and_rejects_conflict(self):
        with self.station('alpha') as (_, revisions, issued):
            piece, payload = issue_with_companion(revisions, issued, alpha_batch())
        with self.center() as (db, catalog, _):
            with self.assertRaises(KeyError):
                receive_audit_companion(catalog, payload)
            receive_piece(catalog, piece)
            digest = receive_audit_companion(catalog, payload)
            self.assertEqual(digest, json.loads(payload)['digest'])
            # Identical resubmission returns the same digest.
            self.assertEqual(receive_audit_companion(catalog, payload), digest)
            tampered = json.loads(payload)
            tampered['rows'][0][2] = '3'
            with self.assertRaises(ValueError):
                receive_audit_companion(catalog, json.dumps(tampered).encode())
            self.assertEqual(
                db.execute('SELECT COUNT(*) FROM received_audit_companions').fetchone()[0], 1
            )

    def test_intake_rejects_identity_and_v1_digest_mismatch(self):
        with self.station('alpha') as (_, revisions, issued):
            piece, payload = issue_with_companion(revisions, issued, alpha_batch())
        with self.center() as (_, catalog, _):
            receive_piece(catalog, piece)
            wrong_identity = json.loads(payload)
            wrong_identity['identity']['station'] = 'Other'
            with self.assertRaises(ValueError):
                receive_audit_companion(catalog, json.dumps(wrong_identity).encode())
            wrong_hash = json.loads(payload)
            wrong_hash['v1_sha256'] = '0' * 64
            with self.assertRaises(ValueError):
                receive_audit_companion(catalog, json.dumps(wrong_hash).encode())


class TestAuditTickets(ProjectCase):
    def _wire(self, catalog, alpha, beta, *, returning=False, companions=True):
        receive_piece(catalog, alpha)
        receive_piece(catalog, beta)
        if companions:
            receive_audit_companion(catalog, self.companion_alpha)
            receive_audit_companion(catalog, self.companion_beta)
        a, b = alpha.identity.evidence_id, beta.identity.evidence_id
        wiring = [
            RouteAssignment(PortRef(a, 'send'), PortRef(b, 'start')),
            RouteAssignment(PortRef(b, 'qualified'), TerminalDisposition.PASS),
        ]
        if returning:
            wiring += [
                RouteAssignment(PortRef(b, 'return'), PortRef(a, 'start')),
                RouteAssignment(PortRef(b, 'discard'), TerminalDisposition.SCRAP),
            ]
        return freeze_route(catalog, (a, b), wiring)

    def setUp(self):
        super().setUp()
        self.alpha_conn = sqlite3.connect(':memory:')
        self.alpha_rev = RevisionStore(self.alpha_conn)
        self.alpha_issued = StationEvidenceStore(self.alpha_conn)
        self.alpha_rev.initialize()
        self.alpha_issued.initialize()
        self.alpha, self.companion_alpha = issue_with_companion(
            self.alpha_rev, self.alpha_issued, alpha_batch(retry=True)
        )
        self.beta_conn = sqlite3.connect(':memory:')
        self.beta_rev = RevisionStore(self.beta_conn)
        self.beta_issued = StationEvidenceStore(self.beta_conn)
        self.beta_rev.initialize()
        self.beta_issued.initialize()
        self.beta, self.companion_beta = issue_with_companion(
            self.beta_rev, self.beta_issued, beta_batch(returning=True)
        )

    def test_ticket_fields_and_persistence(self):
        with self.center() as (_, catalog, ledger):
            route = self._wire(catalog, self.alpha, self.beta, returning=True)
            ticket = publish_audit_ticket(
                catalog, ledger, route.route_id, port(self.alpha), UniformRandBelow()
            )
            self.assertIsNotNone(ticket)
            self.assertTrue(ticket['ticket_id'].startswith('ticket-'))
            self.assertEqual(ticket['route_id'], route.route_id)
            self.assertEqual(ticket['start'], [self.alpha.identity.evidence_id, 'start'])
            self.assertEqual(len(ticket['companions']), 2)
            self.assertEqual(
                [item['evidence_id'] for item in ticket['companions']],
                [identity.evidence_id for identity in route.pieces],
            )
            self.assertTrue(all(len(item['digest']) == 64 for item in ticket['companions']))
            self.assertGreaterEqual(len(ticket['visits']), 2)
            self.assertIn(ticket['selected'], range(len(ticket['visits'])))
            stored = read_audit_ticket(ledger, ticket['ticket_id'])
            self.assertEqual(stored, ticket)

    def test_missing_companion_raises_keyerror_and_no_ticket(self):
        with self.center() as (db, catalog, ledger):
            route = self._wire(
                catalog, self.alpha, self.beta, returning=True, companions=False
            )
            with self.assertRaises(KeyError):
                publish_audit_ticket(
                    catalog, ledger, route.route_id, port(self.alpha),
                    UniformRandBelow(),
                )
            self.assertEqual(db.execute('SELECT COUNT(*) FROM audit_tickets').fetchone()[0], 0)

    def test_no_qualifying_path_returns_none(self):
        trap_batch = batch(
            'Trap', [('bench', 'bench', 1)], exits=('send',)
        )
        with self.station('trap') as (_, revisions, issued):
            frozen = freeze_process(
                revisions, revisions.save_draft(trap_batch).draft_id
            )
            trap_piece = issue_evidence(revisions, issued, frozen.revision_id)
            trap_companion = issue_audit_companion(
                revisions, issued, trap_piece.identity.evidence_id
            )
        with self.center() as (db, catalog, ledger):
            receive_piece(catalog, trap_piece)
            receive_audit_companion(catalog, trap_companion)
            route = freeze_route(catalog, [trap_piece.identity.evidence_id], [
                RouteAssignment(
                    PortRef(trap_piece.identity.evidence_id, 'send'),
                    TerminalDisposition.PASS,
                ),
            ])
            self.assertIsNone(publish_audit_ticket(
                catalog, ledger, route.route_id, port(trap_piece), UniformRandBelow()
            ))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM audit_tickets').fetchone()[0], 0)

    def test_bad_randbelow_rejected(self):
        with self.center() as (_, catalog, ledger):
            route = self._wire(catalog, self.alpha, self.beta, returning=True)
            for bad in (lambda n: n, lambda n: 1.0, lambda n: -1):
                with self.subTest(bad=bad):
                    with self.assertRaises(ValueError):
                        publish_audit_ticket(
                            catalog, ledger, route.route_id, port(self.alpha), bad
                        )
            with self.assertRaises(ValueError):
                publish_audit_ticket(
                    catalog, ledger, route.route_id, port(self.alpha), None
                )

    def test_unknown_ticket_and_invalid_number(self):
        with self.center() as (_, _, ledger):
            with self.assertRaises(KeyError):
                read_audit_ticket(ledger, 'ticket-missing')
            with self.assertRaises(ValueError):
                read_audit_ticket(ledger, '')

    def test_redeem_returns_private_observations_and_unique_mark(self):
        with self.center() as (_, catalog, ledger):
            route = self._wire(catalog, self.alpha, self.beta, returning=True)
            ticket = publish_audit_ticket(
                catalog, ledger, route.route_id, port(self.alpha),
                UniformRandBelow(),
            )
            selected_evidence = ticket['visits'][ticket['selected']][0]
            if selected_evidence == self.alpha.identity.evidence_id:
                revisions, issued, number = (
                    self.alpha_rev, self.alpha_issued, self.alpha.identity.evidence_id
                )
            else:
                revisions, issued, number = (
                    self.beta_rev, self.beta_issued, self.beta.identity.evidence_id
                )
            observations, mark = redeem_audit_ticket(
                revisions, issued, number, ticket, UniformRandBelow()
            )
            self.assertGreaterEqual(len(observations), 1)
            self.assertIn(mark, range(len(observations)))
            _, _, exit_name = ticket['visits'][ticket['selected']]
            self.assertEqual(observations[-1].target, exit_name)
            self.assertNotIn(b'bench', json.dumps(ticket).encode())

    def test_redeem_rejects_wrong_station_and_tampered_ticket(self):
        with self.center() as (_, catalog, ledger):
            route = self._wire(catalog, self.alpha, self.beta, returning=True)
            ticket = publish_audit_ticket(
                catalog, ledger, route.route_id, port(self.alpha),
                UniformRandBelow(),
            )
            with self.assertRaises(ValueError):
                redeem_audit_ticket(
                    self.alpha_rev, self.alpha_issued,
                    self.beta.identity.evidence_id, ticket, UniformRandBelow(),
                )
            tampered = dict(ticket)
            tampered['selected'] = len(ticket['visits'])
            with self.assertRaises(ValueError):
                redeem_audit_ticket(
                    self.alpha_rev, self.alpha_issued,
                    self.alpha.identity.evidence_id, tampered, UniformRandBelow(),
                )
            # A digest field that no longer matches the canonical body is rejected.
            bad_digest = dict(ticket)
            bad_digest['digest'] = '0' * 64
            with self.assertRaises(ValueError):
                redeem_audit_ticket(
                    self.alpha_rev, self.alpha_issued,
                    self.alpha.identity.evidence_id, bad_digest, UniformRandBelow(),
                )

    def test_station_rejects_bad_randbelow(self):
        with self.center() as (_, catalog, ledger):
            route = self._wire(catalog, self.alpha, self.beta, returning=True)
            ticket = publish_audit_ticket(
                catalog, ledger, route.route_id, port(self.alpha),
                UniformRandBelow(),
            )
        selected_evidence = ticket['visits'][ticket['selected']][0]
        if selected_evidence == self.alpha.identity.evidence_id:
            revisions, issued, number = (
                self.alpha_rev, self.alpha_issued, self.alpha.identity.evidence_id
            )
        else:
            revisions, issued, number = (
                self.beta_rev, self.beta_issued, self.beta.identity.evidence_id
            )
        for bad in (lambda n: n, lambda n: 0.5):
            with self.assertRaises(ValueError):
                redeem_audit_ticket(revisions, issued, number, ticket, bad)

    def test_original_ticket_survives_station_correction(self):
        with self.center() as (_, catalog, ledger):
            route = self._wire(catalog, self.alpha, self.beta, returning=True)
            ticket = publish_audit_ticket(
                catalog, ledger, route.route_id, port(self.alpha),
                UniformRandBelow(),
            )
        correction = open_correction(
            self.alpha_rev, self.alpha.identity.revision_id, alpha_batch()
        )
        new_revision = freeze_process(self.alpha_rev, correction.draft_id)
        new_piece = issue_evidence(self.alpha_rev, self.alpha_issued, new_revision.revision_id)
        self.assertNotEqual(new_piece.identity.evidence_id, self.alpha.identity.evidence_id)
        # The original ticket still redeems against the bound original revision.
        selected_evidence = ticket['visits'][ticket['selected']][0]
        if selected_evidence == self.alpha.identity.evidence_id:
            revisions, issued, number = (
                self.alpha_rev, self.alpha_issued, self.alpha.identity.evidence_id
            )
        else:
            revisions, issued, number = (
                self.beta_rev, self.beta_issued, self.beta.identity.evidence_id
            )
        observations, mark = redeem_audit_ticket(
            revisions, issued, number, ticket, UniformRandBelow()
        )
        self.assertIn(mark, range(len(observations)))

    def test_signing_failure_leaves_no_new_ticket(self):
        with self.center() as (db, catalog, ledger):
            route = self._wire(catalog, self.alpha, self.beta, returning=True)
            before = db.execute('SELECT COUNT(*) FROM audit_tickets').fetchone()[0]
            db.execute('PRAGMA foreign_keys = ON')
            db.executescript('''
                CREATE TABLE ticket_gate (ticket_id TEXT PRIMARY KEY);
                CREATE TABLE ticket_blocked (
                    ticket_id TEXT NOT NULL REFERENCES ticket_gate(ticket_id)
                    DEFERRABLE INITIALLY DEFERRED
                );
                CREATE TRIGGER block_audit_ticket AFTER INSERT ON audit_tickets
                BEGIN
                    INSERT INTO ticket_blocked VALUES (NEW.ticket_id);
                END;
            ''')
            with self.assertRaises(sqlite3.IntegrityError):
                publish_audit_ticket(
                    catalog, ledger, route.route_id, port(self.alpha),
                    UniformRandBelow(),
                )
            self.assertFalse(db.in_transaction)
            self.assertEqual(
                db.execute('SELECT COUNT(*) FROM audit_tickets').fetchone()[0], before
            )
        # A retry on a clean database succeeds and can be read back by number.
        with self.center() as (_, catalog2, ledger2):
            route2 = self._wire(catalog2, self.alpha, self.beta, returning=True)
            ticket = publish_audit_ticket(
                catalog2, ledger2, route2.route_id, port(self.alpha),
                UniformRandBelow(),
            )
            self.assertEqual(read_audit_ticket(ledger2, ticket['ticket_id']), ticket)
