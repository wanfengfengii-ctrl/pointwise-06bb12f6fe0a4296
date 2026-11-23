"""Encoding and verification of the audit companion envelope."""
import json
from hashlib import sha256

from .codec import encode_evidence
from .evidence import StationEvidence

AUDIT_COMPANION_KIND = 'audit-companion'


def canonical_digest(document: dict) -> str:
    payload = json.dumps(
        document, sort_keys=True, ensure_ascii=False, separators=(',', ':')
    ).encode('utf-8')
    return sha256(payload).hexdigest()


def build_audit_companion(
    piece: StationEvidence, weighted_steps: dict[tuple[str, str], object]
) -> dict:
    rows = [
        [entry, exit_name, str(weighted_steps[(entry, exit_name)])]
        for entry, exit_name in sorted((row.entry, row.exit) for row in piece.rows)
    ]
    document = {
        'kind': AUDIT_COMPANION_KIND,
        'identity': {
            'evidence_id': piece.identity.evidence_id,
            'station': piece.identity.station,
            'revision_id': piece.identity.revision_id,
        },
        'v1_sha256': sha256(encode_evidence(piece)).hexdigest(),
        'rows': rows,
    }
    document['digest'] = canonical_digest(document)
    return document


def encode_audit_companion(document: dict) -> bytes:
    validate_audit_companion(document)
    return json.dumps(
        document, sort_keys=True, ensure_ascii=False, separators=(',', ':')
    ).encode('utf-8')


def decode_audit_companion(payload: bytes) -> dict:
    if not isinstance(payload, bytes):
        raise ValueError('audit companion must be UTF-8 bytes')
    try:
        document = json.loads(payload.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError('invalid audit companion envelope') from error
    validate_audit_companion(document)
    return document


def validate_audit_companion(document: dict) -> None:
    if not isinstance(document, dict):
        raise ValueError('audit companion object required')
    if set(document) != {'kind', 'identity', 'v1_sha256', 'rows', 'digest'}:
        raise ValueError('invalid audit companion fields')
    if document['kind'] != AUDIT_COMPANION_KIND:
        raise ValueError('invalid audit companion kind')
    identity = document['identity']
    if not isinstance(identity, dict) or set(identity) != {
        'evidence_id', 'station', 'revision_id'
    } or any(not isinstance(value, str) or not value for value in identity.values()):
        raise ValueError('invalid audit companion identity')
    digest = document['digest']
    v1_sha = document['v1_sha256']
    if not isinstance(digest, str) or not isinstance(v1_sha, str) or len(digest) != 64 or len(v1_sha) != 64:
        raise ValueError('invalid audit companion digest fields')
    rows = document['rows']
    if not isinstance(rows, list):
        raise ValueError('audit companion rows must be a list')
    seen = set()
    for fields in rows:
        if not isinstance(fields, list) or len(fields) != 3:
            raise ValueError('invalid audit companion row')
        entry, exit_name, weighted = fields
        if not isinstance(entry, str) or not entry or not isinstance(exit_name, str) or not exit_name:
            raise ValueError('invalid audit companion row ports')
        if (entry, exit_name) in seen:
            raise ValueError('duplicate audit companion row')
        seen.add((entry, exit_name))
        if not isinstance(weighted, str):
            raise ValueError('weighted steps must be a canonical fraction string')
        value = _fraction(weighted)
        if value < 0:
            raise ValueError('weighted steps must be nonnegative')
    without_digest = {key: value for key, value in document.items() if key != 'digest'}
    if canonical_digest(without_digest) != digest:
        raise ValueError('audit companion digest mismatch')


def _fraction(text: str):
    from fractions import Fraction

    value = Fraction(text)
    if str(value) != text:
        raise ValueError('fraction must be canonical')
    return value
