from dataclasses import dataclass
from fractions import Fraction
from uuid import uuid4

from rework.report.audit_ticket import sign_audit_ticket
from rework.report.ledger import ReportLedger
from rework.report.publication import publish_report
from rework.report.snapshot import ReportSnapshot
from rework.routing.audit_intake import get_received_audit_companion
from rework.routing.catalog import RoutingCatalog
from rework.routing.intake import get_received_evidence
from rework.routing.plans import FrozenRoute, read_frozen_route
from rework.routing.port_checks import PortRef, require_selected_entry, selected_ports
from rework.station.evidence import StationEvidence

from .audit_tickets import reach_and_labeled_steps, sample_audit_visits
from .composition import compose_route
from .moments import QualificationStatistics, calculate_moments


@dataclass(frozen=True)
class Assessment:
    route: FrozenRoute
    pieces: tuple[StationEvidence, ...]
    start: PortRef
    statistics: QualificationStatistics


def publish_assessment(
    catalog: RoutingCatalog, ledger: ReportLedger, route_id: int, start: PortRef
) -> ReportSnapshot:
    route = read_frozen_route(catalog, route_id)
    pieces = tuple(get_received_evidence(catalog, identity) for identity in route.pieces)
    require_selected_entry(selected_ports(pieces), start)
    graph = compose_route(route, pieces)
    statistics = calculate_moments(graph, start)
    return publish_report(ledger, Assessment(route, pieces, start, statistics))


def _companion_weighted_steps(
    catalog: RoutingCatalog, route: FrozenRoute, pieces: tuple[StationEvidence, ...]
) -> dict[tuple[PortRef, str], Fraction]:
    weighted: dict[tuple[PortRef, str], Fraction] = {}
    for identity, piece in zip(route.pieces, pieces):
        companion = get_received_audit_companion(catalog, identity.evidence_id)
        if (companion['identity']['evidence_id'], companion['identity']['station'],
                companion['identity']['revision_id']) != (
            identity.evidence_id, identity.station, identity.revision_id
        ):
            raise ValueError('received audit companion identity does not match the frozen route')
        for entry, exit_name, value in companion['rows']:
            weighted[(PortRef(identity.evidence_id, entry), exit_name)] = Fraction(value)
    return weighted


def publish_audit_ticket(
    catalog: RoutingCatalog,
    ledger: ReportLedger,
    route_id: int,
    start: PortRef,
    randbelow,
) -> dict | None:
    if not callable(randbelow):
        raise ValueError('randbelow callback required')
    route = read_frozen_route(catalog, route_id)
    pieces = tuple(get_received_evidence(catalog, identity) for identity in route.pieces)
    require_selected_entry(selected_ports(pieces), start)
    # Every selected piece must have its exact received audit companion.
    weighted_steps = _companion_weighted_steps(catalog, route, pieces)
    graph = compose_route(route, pieces)
    total_mass, _, _ = reach_and_labeled_steps(graph, start, weighted_steps)
    if total_mass <= 0:
        return None
    visits, selected = sample_audit_visits(graph, start, weighted_steps, randbelow)
    companions = [
        {
            'evidence_id': identity.evidence_id,
            'station': identity.station,
            'revision_id': identity.revision_id,
            'digest': get_received_audit_companion(
                catalog, identity.evidence_id
            )['digest'],
        }
        for identity in route.pieces
    ]
    ticket = sign_audit_ticket({
        'ticket_id': f'ticket-{uuid4().hex}',
        'route_id': route.route_id,
        'start': [start.evidence_id, start.port],
        'companions': companions,
        'visits': [[entry.evidence_id, entry.port, exit_name] for entry, exit_name in visits],
        'selected': selected,
    })
    return ledger.append_audit_ticket(ticket)
