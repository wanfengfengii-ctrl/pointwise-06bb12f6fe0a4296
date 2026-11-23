import json
import random
import sqlite3
from contextlib import closing, contextmanager
from fractions import Fraction as F
from pathlib import Path
from tempfile import TemporaryDirectory

from rework.analysis.assessment import publish_assessment, publish_audit_ticket
from rework.process.observations import Observation, ObservationBatch
from rework.process.ports import ProcessPorts
from rework.process.revision_store import RevisionStore
from rework.process.revisions import freeze_process, open_correction
from rework.report.export import export_report
from rework.report.ledger import ReportLedger, read_audit_ticket, read_report
from rework.routing.assignments import RouteAssignment, TerminalDisposition
from rework.routing.audit_intake import receive_audit_companion
from rework.routing.catalog import RoutingCatalog
from rework.routing.intake import receive_evidence
from rework.routing.plans import freeze_route
from rework.routing.port_checks import PortRef
from rework.station.codec import encode_evidence
from rework.station.issue import (
    StationEvidenceStore, issue_audit_companion, issue_evidence,
    redeem_audit_ticket,
)


def make_batch(station, exits, observations):
    ports = ProcessPorts(('workbench',), (('start', 'workbench'),), exits)
    return ObservationBatch(station, ports, tuple(Observation('workbench', exit_name, minutes)
                                                  for exit_name, minutes in observations))


def freeze_and_issue(path, batch, old_revision_id=None):
    with closing(sqlite3.connect(path)) as connection:
        revisions = RevisionStore(connection)
        issued = StationEvidenceStore(connection)
        revisions.initialize()
        issued.initialize()
        if old_revision_id is None:
            draft = revisions.save_draft(batch)
        else:
            draft = open_correction(revisions, old_revision_id, batch)
        revision = freeze_process(revisions, draft.draft_id)
        return issue_evidence(revisions, issued, revision.revision_id)


@contextmanager
def center(path):
    with closing(sqlite3.connect(path)) as connection:
        catalog = RoutingCatalog(connection)
        ledger = ReportLedger(connection)
        catalog.initialize()
        ledger.initialize()
        yield catalog, ledger


def assess(catalog, ledger, alpha, beta):
    alpha_id = alpha.identity.evidence_id
    beta_id = beta.identity.evidence_id
    for piece in (alpha, beta):
        receive_evidence(catalog, encode_evidence(piece))
    start = PortRef(alpha_id, 'start')
    route = freeze_route(catalog, (alpha_id, beta_id), (
        RouteAssignment(PortRef(alpha_id, 'send'), PortRef(beta_id, 'start')),
        RouteAssignment(PortRef(beta_id, 'qualified'), TerminalDisposition.PASS),
        RouteAssignment(PortRef(beta_id, 'return'), start),
        RouteAssignment(PortRef(beta_id, 'discard'), TerminalDisposition.SCRAP),
    ))
    return publish_assessment(catalog, ledger, route.route_id, start)


def statistics(report):
    return report.probability, report.mean, report.variance


def audit_demo():
    """Close-the-center ticket signing, station redemption and ticket readback."""
    from audit_example import alpha_batch, beta_batch

    def station_issue(source):
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
        return connection, revisions, issued, piece, companion

    center_dir = TemporaryDirectory()
    center_path = str(Path(center_dir.name) / 'audit-center.sqlite')
    alpha_conn, alpha_rev, alpha_issued, alpha, alpha_companion = station_issue(
        alpha_batch(retry=True)
    )
    beta_conn, beta_rev, beta_issued, beta, beta_companion = station_issue(
        beta_batch(returning=True)
    )
    center = sqlite3.connect(center_path)
    catalog = RoutingCatalog(center)
    ledger = ReportLedger(center)
    catalog.initialize()
    ledger.initialize()
    receive_evidence(catalog, encode_evidence(alpha))
    receive_evidence(catalog, encode_evidence(beta))
    receive_audit_companion(catalog, alpha_companion)
    receive_audit_companion(catalog, beta_companion)
    a, b = alpha.identity.evidence_id, beta.identity.evidence_id
    route = freeze_route(catalog, (a, b), (
        RouteAssignment(PortRef(a, 'send'), PortRef(b, 'start')),
        RouteAssignment(PortRef(b, 'qualified'), TerminalDisposition.PASS),
        RouteAssignment(PortRef(b, 'return'), PortRef(a, 'start')),
        RouteAssignment(PortRef(b, 'discard'), TerminalDisposition.SCRAP),
    ))
    randbelow = random.Random(20261123).randrange
    ticket = publish_audit_ticket(
        catalog, ledger, route.route_id, PortRef(a, 'start'), randbelow
    )
    ticket_id = ticket['ticket_id']
    selected = ticket['visits'][ticket['selected']]
    # The center is now closed; the original station still holds its private data.
    center.close()
    if selected[0] == a:
        revisions, issued, number = alpha_rev, alpha_issued, a
    else:
        revisions, issued, number = beta_rev, beta_issued, b
    observations, mark = redeem_audit_ticket(
        revisions, issued, number, ticket, randbelow
    )
    # Reopen the center database and read the original ticket back by number.
    with closing(sqlite3.connect(center_path)) as reopened:
        reopened_ledger = ReportLedger(reopened)
        reopened_ledger.initialize()
        readback = read_audit_ticket(reopened_ledger, ticket_id)
    print('POINTWISE_SCENARIO ' + json.dumps({
        'case_id': 'audit_ticket.close_sign_station_redeem',
        'ticket_id': ticket_id,
        'visits': ticket['visits'],
        'selected': ticket['selected'],
        'selected_visit': selected,
        'redeemed_steps': [
            [observation.source, observation.target, observation.minutes]
            for observation in observations
        ],
        'marked_position': mark,
        'readback_matches': readback == ticket,
    }, sort_keys=True))
    center_dir.cleanup()


def main():
    with TemporaryDirectory() as work:
        root = Path(work)
        alpha_file = root / 'alpha.sqlite'
        beta_file = root / 'beta.sqlite'
        center_file = root / 'center.sqlite'
        exits = ('qualified', 'return', 'discard')
        alpha = freeze_and_issue(alpha_file, make_batch('Alpha', ('send',), [('send', 1)]))
        beta = freeze_and_issue(beta_file, make_batch('Beta', exits, [
            ('qualified', 2), ('qualified', 2), ('return', 0), ('discard', 0),
        ]))
        with center(center_file) as (catalog, ledger):
            original = assess(catalog, ledger, alpha, beta)
            saved_bytes = export_report(read_report(ledger, original.report_id))
        replacement = freeze_and_issue(beta_file, make_batch('Beta', exits, [
            ('qualified', 5), ('qualified', 5), ('return', 0), ('discard', 0),
        ]), old_revision_id=beta.identity.revision_id)
        with center(center_file) as (catalog, ledger):
            corrected = assess(catalog, ledger, alpha, replacement)
        with center(center_file) as (_, ledger):
            old = read_report(ledger, original.report_id)
            new = read_report(ledger, corrected.report_id)
            expected_old = (F(2, 3), F(10, 3), F(4, 9))
            expected_new = (F(2, 3), F(19, 3), F(4, 9))
            old_unchanged = export_report(old) == saved_bytes and old == original
            new_origin = set(new.provenance.pieces) == {alpha.identity, replacement.identity}
            if statistics(old) != expected_old or statistics(new) != expected_new:
                raise AssertionError('REWORK_REPORT_MISMATCH')
            if not old_unchanged or not new_origin or new.provenance.start != old.provenance.start:
                raise AssertionError('report history or fixed provenance disagrees')
            print(export_report(old).decode('utf-8'), end='')
            print(export_report(new).decode('utf-8'), end='')
            print('POINTWISE_SCENARIO ' + json.dumps({
                'case_id': 'historical_review.same_entry_new_report',
                'input': {'old_beta_pass_minutes': 2, 'new_beta_pass_minutes': 5,
                          'alpha_external_minutes': 1, 'beta_return_probability': '1/4'},
                'expected': {'old': list(map(str, expected_old)), 'new': list(map(str, expected_new))},
                'actual': {'old': list(map(str, statistics(old))), 'new': list(map(str, statistics(new)))},
                'downstream_expected': {'old_export_unchanged': True, 'new_origin_matches': True},
                'downstream_actual': {'old_export_unchanged': old_unchanged, 'new_origin_matches': new_origin},
                'code_paths': ['rework/process/revisions.py', 'rework/station/issue.py',
                               'rework/analysis/assessment.py', 'rework/report/ledger.py', 'rework/report/export.py'],
                'oracle': 'The correction adds three minutes to each successful path, preserving probability and variance.',
            }, sort_keys=True))
        audit_demo()


if __name__ == '__main__':
    main()
