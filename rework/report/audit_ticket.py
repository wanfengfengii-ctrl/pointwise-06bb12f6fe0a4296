"""Structural validation and canonical digest for signed audit tickets."""
from rework.station.audit_companion import canonical_digest

TICKET_FIELDS = {'ticket_id', 'route_id', 'start', 'companions', 'visits', 'selected', 'digest'}


def validate_audit_ticket(ticket: object) -> dict:
    if not isinstance(ticket, dict) or set(ticket) != TICKET_FIELDS:
        raise ValueError('invalid audit ticket fields')
    if not isinstance(ticket['ticket_id'], str) or not ticket['ticket_id']:
        raise ValueError('invalid audit ticket number')
    if type(ticket['route_id']) is not int or ticket['route_id'] <= 0:
        raise ValueError('invalid audit ticket route number')
    start = ticket['start']
    if (not isinstance(start, list) or len(start) != 2
            or not all(isinstance(value, str) and value for value in start)):
        raise ValueError('invalid audit ticket start')
    companions = ticket['companions']
    if not isinstance(companions, list) or not companions:
        raise ValueError('audit ticket requires companion provenance')
    for item in companions:
        if not isinstance(item, dict) or set(item) != {
            'evidence_id', 'station', 'revision_id', 'digest'
        } or any(not isinstance(item[key], str) or not item[key] for key in (
            'evidence_id', 'station', 'revision_id', 'digest'
        )) or len(item['digest']) != 64:
            raise ValueError('invalid audit ticket companion provenance')
    visits = ticket['visits']
    if not isinstance(visits, list) or not visits:
        raise ValueError('audit ticket requires at least one visit')
    for visit in visits:
        if not isinstance(visit, list) or len(visit) != 3 or not all(
            isinstance(value, str) and value for value in visit
        ):
            raise ValueError('invalid audit ticket visit')
    selected = ticket['selected']
    if type(selected) is not int or not 0 <= selected < len(visits):
        raise ValueError('audit ticket selected index is outside its visits')
    if not isinstance(ticket['digest'], str) or len(ticket['digest']) != 64:
        raise ValueError('invalid audit ticket digest')
    without_digest = {key: value for key, value in ticket.items() if key != 'digest'}
    if canonical_digest(without_digest) != ticket['digest']:
        raise ValueError('audit ticket digest mismatch')
    return ticket


def sign_audit_ticket(ticket_without_digest: dict) -> dict:
    ticket = dict(ticket_without_digest)
    ticket['digest'] = canonical_digest(ticket)
    return validate_audit_ticket(ticket)
