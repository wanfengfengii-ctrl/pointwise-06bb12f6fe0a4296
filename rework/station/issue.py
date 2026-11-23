import sqlite3
from hashlib import sha256
from uuid import uuid4

from rework.process.revision_store import RevisionStore
from rework.process.revisions import read_frozen_revision

from .audit_companion import (
    build_audit_companion, canonical_digest, decode_audit_companion,
    encode_audit_companion,
)
from .audit_engine import sample_local_audit_path, weighted_steps_by_exit
from .codec import decode_evidence, encode_evidence
from .evidence import EvidenceIdentity, StationEvidence
from .exit_analysis import analyze_station_exits
from .transitions import build_station_graph


class StationEvidenceStore:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def initialize(self) -> None:
        with self.connection:
            self.connection.execute('''
                CREATE TABLE IF NOT EXISTS issued_evidence (
                    evidence_id TEXT PRIMARY KEY, payload BLOB NOT NULL, digest TEXT NOT NULL
                )
            ''')

    def _insert(self, piece: StationEvidence) -> None:
        payload = encode_evidence(piece)
        with self.connection:
            self.connection.execute('INSERT INTO issued_evidence VALUES (?, ?, ?)', (
                piece.identity.evidence_id, payload, sha256(payload).hexdigest(),
            ))


def issue_evidence(
    revisions: RevisionStore, issued: StationEvidenceStore, revision_id: str
) -> StationEvidence:
    revision = read_frozen_revision(revisions, revision_id)
    graph = build_station_graph(revision.batch.ports, revision.rules)
    piece = StationEvidence(
        EvidenceIdentity(f'piece-{uuid4().hex}', revision.batch.station, revision.revision_id),
        tuple(name for name, _ in graph.entries), graph.exits, analyze_station_exits(graph),
    )
    issued._insert(piece)
    return piece


def read_station_evidence(issued: StationEvidenceStore, evidence_id: str) -> StationEvidence:
    row = issued.connection.execute(
        'SELECT payload, digest FROM issued_evidence WHERE evidence_id = ?', (evidence_id,),
    ).fetchone()
    if row is None:
        raise KeyError(evidence_id)
    payload, digest = row
    if not isinstance(payload, bytes) or sha256(payload).hexdigest() != digest:
        raise ValueError('issued evidence content has changed')
    piece = decode_evidence(payload)
    if piece.identity.evidence_id != evidence_id:
        raise ValueError('issued evidence identity mismatch')
    return piece


def issue_audit_companion(
    revisions: RevisionStore, issued: StationEvidenceStore, evidence_id: str
) -> bytes:
    """Issue the deterministic audit companion bound to an existing V1 piece."""
    piece = read_station_evidence(issued, evidence_id)
    revision = read_frozen_revision(revisions, piece.identity.revision_id)
    graph = build_station_graph(revision.batch.ports, revision.rules)
    weighted = weighted_steps_by_exit(graph)
    missing = {(row.entry, row.exit) for row in piece.rows}.difference(weighted)
    if missing:
        raise ValueError('companion cannot cover every fixed V1 evidence row')
    document = build_audit_companion(piece, weighted)
    return encode_audit_companion(document)


def _validate_ticket(ticket: object) -> dict:
    if not isinstance(ticket, dict):
        raise ValueError('audit ticket object required')
    required = {'ticket_id', 'route_id', 'start', 'companions', 'visits', 'selected', 'digest'}
    if set(ticket) != required:
        raise ValueError('invalid audit ticket fields')
    if not isinstance(ticket['ticket_id'], str) or not ticket['ticket_id']:
        raise ValueError('invalid audit ticket number')
    if type(ticket['route_id']) is not int or ticket['route_id'] <= 0:
        raise ValueError('invalid audit ticket route number')
    start = ticket['start']
    if not isinstance(start, list) or len(start) != 2 or not all(isinstance(v, str) and v for v in start):
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


def redeem_audit_ticket(
    revisions: RevisionStore,
    issued: StationEvidenceStore,
    evidence_id: str,
    ticket: dict,
    randbelow,
) -> tuple[tuple, int]:
    """Replay the single station visit selected by a signed audit ticket."""
    ticket = _validate_ticket(ticket)
    try:
        visit = ticket['visits'][ticket['selected']]
    except (IndexError, KeyError, TypeError):
        raise ValueError('selected audit visit is unavailable')
    visit_evidence, entry, exit_name = visit
    if visit_evidence != evidence_id:
        raise ValueError('ticket selected visit belongs to another station')
    piece = read_station_evidence(issued, evidence_id)
    revision = read_frozen_revision(revisions, piece.identity.revision_id)

    companion_document = decode_audit_companion(
        issue_audit_companion(revisions, issued, evidence_id)
    )
    expected_digest = companion_document['digest']
    bindings = {item['evidence_id']: item for item in ticket['companions']}
    binding = bindings.get(evidence_id)
    if binding is None:
        raise ValueError('audit ticket has no provenance for this station')
    if binding['digest'] != expected_digest:
        raise ValueError('audit ticket companion digest does not match the original issue')
    if (binding['station'], binding['revision_id']) != (
        piece.identity.station, piece.identity.revision_id
    ):
        raise ValueError('audit ticket provenance does not match the original V1 issue')
    if entry not in piece.entries or exit_name not in piece.exits:
        raise ValueError('audit visit ports are outside the fixed V1 issue')
    if not any(row.entry == entry and row.exit == exit_name for row in piece.rows):
        raise ValueError('audit visit has no matching V1 evidence row')

    graph = build_station_graph(revision.batch.ports, revision.rules)
    return sample_local_audit_path(graph, entry, exit_name, randbelow)
