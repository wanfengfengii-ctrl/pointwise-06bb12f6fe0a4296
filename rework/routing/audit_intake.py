"""Center-side intake for audit companions."""
from hashlib import sha256

from rework.station.audit_companion import decode_audit_companion, encode_audit_companion
from rework.station.codec import encode_evidence

from .catalog import RoutingCatalog
from .intake import get_received_evidence


def receive_audit_companion(catalog: RoutingCatalog, payload: bytes) -> str:
    document = decode_audit_companion(payload)
    identity = document['identity']
    evidence_id = identity['evidence_id']
    # The corresponding V1 issue must already be received and re-verify.
    piece = get_received_evidence(catalog, evidence_id)
    if (piece.identity.evidence_id, piece.identity.station, piece.identity.revision_id) != (
        identity['evidence_id'], identity['station'], identity['revision_id']
    ):
        raise ValueError('audit companion identity does not match the received V1 issue')
    canonical_v1 = encode_evidence(piece)
    if document['v1_sha256'] != sha256(canonical_v1).hexdigest():
        raise ValueError('audit companion V1 digest does not match the received issue')
    v1_rows = [(row.entry, row.exit) for row in piece.rows]
    companion_rows = [(entry, exit_name) for entry, exit_name, _ in document['rows']]
    if companion_rows != v1_rows:
        raise ValueError('audit companion rows must correspond one-to-one with V1 rows')
    canonical_payload = encode_audit_companion(document)
    catalog.put_companion(evidence_id, canonical_payload)
    return document['digest']


def get_received_audit_companion(catalog: RoutingCatalog, evidence_id: str) -> dict:
    return decode_audit_companion(catalog.load_companion(evidence_id))
