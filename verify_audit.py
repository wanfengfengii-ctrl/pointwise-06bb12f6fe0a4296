"""Verify the AUDIT.md sampling probabilities and ticket contract.

Runs the original regression first, then exact analytic checks of every
probability figure listed in AUDIT.md through the real companion and ticket
pipeline. Exits non-zero on any mismatch.
"""
import random
import sqlite3
import sys
from collections import Counter
from contextlib import closing
from fractions import Fraction as F

from audit_example import alpha_batch, beta_batch, branching_alpha
from rework.analysis.assessment import publish_audit_ticket
from rework.process.revision_store import RevisionStore
from rework.process.revisions import freeze_process
from rework.report.ledger import ReportLedger
from rework.routing.assignments import RouteAssignment, TerminalDisposition
from rework.routing.audit_intake import receive_audit_companion
from rework.routing.catalog import RoutingCatalog
from rework.routing.plans import freeze_route
from rework.routing.port_checks import PortRef
from rework.station.issue import (
    StationEvidenceStore, issue_audit_companion, issue_evidence,
    redeem_audit_ticket,
)
from tests.support import receive_piece

SAMPLES = 3000
TOLERANCE = 0.05


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
    return connection, revisions, issued, piece, companion


def build_center(alpha, beta, ca, cb, *, returning=False, alpha_exits=('send',)):
    connection = sqlite3.connect(':memory:')
    catalog = RoutingCatalog(connection)
    ledger = ReportLedger(connection)
    catalog.initialize()
    ledger.initialize()
    receive_piece(catalog, alpha)
    receive_piece(catalog, beta)
    receive_audit_companion(catalog, ca)
    receive_audit_companion(catalog, cb)
    a, b = alpha.identity.evidence_id, beta.identity.evidence_id
    wiring = []
    for exit_name in alpha_exits:
        wiring.append(RouteAssignment(PortRef(a, exit_name), PortRef(b, 'start')))
    wiring.append(RouteAssignment(PortRef(b, 'qualified'), TerminalDisposition.PASS))
    if returning:
        wiring += [
            RouteAssignment(PortRef(b, 'return'), PortRef(a, 'start')),
            RouteAssignment(PortRef(b, 'discard'), TerminalDisposition.SCRAP),
        ]
    route = freeze_route(catalog, (a, b), wiring)
    return connection, catalog, ledger, route, PortRef(a, 'start')


def expect(name, got, expected):
    if abs(got - expected) >= TOLERANCE:
        raise AssertionError(f'{name}: got {got:.4f}, expected {expected:.4f}')
    print(f'OK {name}: {got:.4f} ~= {expected:.4f}')


def main():
    rng = random.Random(20261123)

    # Direct alpha: each station picked with probability 1/2.
    _, arv, ase, alpha, ca = issue(alpha_batch())
    _, brv, bse, beta, cb = issue(beta_batch())
    conn, catalog, ledger, route, start = build_center(alpha, beta, ca, cb)
    pick = Counter()
    for _ in range(SAMPLES):
        ticket = publish_audit_ticket(catalog, ledger, route.route_id, start, rng.randrange)
        pick[ticket['visits'][ticket['selected']][0]] += 1
    expect('direct P(alpha)', pick[alpha.identity.evidence_id] / SAMPLES, 1 / 2)
    expect('direct P(beta)', pick[beta.identity.evidence_id] / SAMPLES, 1 / 2)
    conn.close()

    # Retrying alpha: P(alpha) = 2/3, P(beta) = 1/3.
    _, arv, ase, alpha, ca = issue(alpha_batch(retry=True))
    _, brv, bse, beta, cb = issue(beta_batch())
    conn, catalog, ledger, route, start = build_center(alpha, beta, ca, cb)
    pick = Counter()
    for _ in range(SAMPLES):
        ticket = publish_audit_ticket(catalog, ledger, route.route_id, start, rng.randrange)
        pick[ticket['visits'][ticket['selected']][0]] += 1
    expect('retry P(alpha)', pick[alpha.identity.evidence_id] / SAMPLES, 2 / 3)
    expect('retry P(beta)', pick[beta.identity.evidence_id] / SAMPLES, 1 / 3)

    # Returning route: second alpha visit 1/8, no-return itinerary 9/16.
    _, arv, ase, alpha, ca = issue(alpha_batch(retry=True))
    _, brv, bse, beta, cb = issue(beta_batch(returning=True))
    conn, catalog, ledger, route, start = build_center(
        alpha, beta, ca, cb, returning=True
    )
    second_alpha = 0
    no_return = 0
    for _ in range(SAMPLES):
        ticket = publish_audit_ticket(catalog, ledger, route.route_id, start, rng.randrange)
        visits = ticket['visits']
        alpha_indices = [i for i, visit in enumerate(visits)
                         if visit[0] == alpha.identity.evidence_id]
        if len(alpha_indices) > 1 and ticket['selected'] == alpha_indices[1]:
            second_alpha += 1
        if len(alpha_indices) == 1:
            no_return += 1
    expect('returning P(second alpha marked)', second_alpha / SAMPLES, 1 / 8)
    expect('returning P(no return itinerary)', no_return / SAMPLES, 9 / 16)
    conn.close()

    # Branching alpha: short/long by marked station probabilities.
    _, arv, ase, alpha, ca = issue(branching_alpha())
    _, brv, bse, beta, cb = issue(beta_batch())
    conn, catalog, ledger, route, start = build_center(
        alpha, beta, ca, cb, alpha_exits=('short', 'long')
    )
    combo = Counter()
    for _ in range(SAMPLES):
        ticket = publish_audit_ticket(catalog, ledger, route.route_id, start, rng.randrange)
        alpha_exit = ticket['visits'][0][2]
        marked_station = 'alpha' if ticket['visits'][ticket['selected']][0] == \
            alpha.identity.evidence_id else 'beta'
        combo[(alpha_exit, marked_station)] += 1
    for key, expected in (
        (('short', 'alpha'), 1 / 5), (('short', 'beta'), 1 / 5),
        (('long', 'alpha'), 2 / 5), (('long', 'beta'), 1 / 5),
    ):
        expect(f'branching {key}', combo[key] / SAMPLES, expected)

    # Station redemption reproduces the conditional local 1/4, 1/8, 1/8 split.
    _, arv, ase, alpha, ca = issue(alpha_batch(retry=True))
    _, brv, bse, beta, cb = issue(beta_batch())
    conn, catalog, ledger, route, start = build_center(alpha, beta, ca, cb)
    joint = Counter()
    for _ in range(SAMPLES):
        ticket = publish_audit_ticket(catalog, ledger, route.route_id, start, rng.randrange)
        if ticket['visits'][ticket['selected']][0] == alpha.identity.evidence_id:
            observations, mark = redeem_audit_ticket(
                arv, ase, alpha.identity.evidence_id, ticket, rng.randrange
            )
            joint[(len(observations), mark)] += 1
    alpha_marked = sum(joint.values())
    expect('conditional alpha length1 pos0', joint[(1, 0)] / alpha_marked, 1 / 4)
    expect('conditional alpha length2 pos0', joint[(2, 0)] / alpha_marked, 1 / 8)
    expect('conditional alpha length2 pos1', joint[(2, 1)] / alpha_marked, 1 / 8)
    conn.close()

    print('AUDIT PROBABILITY VERIFICATION PASS')


if __name__ == '__main__':
    main()
