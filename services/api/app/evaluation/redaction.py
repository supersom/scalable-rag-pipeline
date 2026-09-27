"""Finite, versioned local synthetic policy. This is not general-purpose DLP."""
import hashlib
import math
import re

from .delivery_models import ExportPolicy, ManifestEntry, PayloadSlot, RedactedPayload, canonical_bytes
from .provenance import Provenance

private_key_pattern = re.compile(r'-----BEGIN ([A-Z ]{0,32}PRIVATE KEY)-----[\s\S]{0,65536}?-----END \1-----')
private_key_opening = re.compile(r'-----BEGIN [A-Z ]{0,32}PRIVATE KEY-----')
assignment_pattern = re.compile(r'(?i)\b((?:password|token|api[_-]?key)\s{0,8}[:=]\s{0,8})(?:"[^"\r\n]{0,4096}"|\x27[^\x27\r\n]{0,4096}\x27|[^\s;,]{1,4096})')
secret_pattern = re.compile(r'(?i)\bBearer[ \t]{1,8}[A-Za-z0-9._~+/=-]{1,4096}|\b[A-Za-z0-9_-]{2,2048}\.[A-Za-z0-9_-]{2,2048}\.[A-Za-z0-9_-]{2,2048}\b|\b(?:AKIA|ASIA)[A-Z0-9]{16}\b')
email_pattern = re.compile(r'(?<![\w.+-])[A-Za-z0-9.!#$%&\x27*+/=?^_`{|}~-]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9.-]{0,123}[A-Za-z0-9])?\.[A-Za-z]{2,63}(?![\w.-])')
phone_pattern = re.compile(r'(?<![\w+])\+(?:[0-9][ ().-]{0,2}){7,14}[0-9](?![0-9 ().-]*[0-9])')
FORBIDDEN = {'authorization', 'cookie', 'setcookie', 'password', 'secret', 'token', 'accesstoken', 'refreshtoken', 'apikey', 'privatekey'}


def _structure(value, depth=0):
    if depth > 8:
        return False
    if isinstance(value, dict):
        return all(isinstance(k, str) and re.sub(r'[^a-z0-9]', '', k.lower()) not in FORBIDDEN
                   and _structure(v, depth + 1) for k, v in value.items())
    if isinstance(value, list):
        return all(_structure(v, depth + 1) for v in value)
    return value is None or type(value) in (str, int, float, bool)


def _shape(kind, value, policy, provenance):
    if not isinstance(value, dict) or not _structure(value):
        return False
    if kind == 'model_request':
        return (set(value) == {'messages', 'model', 'temperature', 'max_tokens'}
                and value['model'] in policy.allowed_models and isinstance(value['messages'], list)
                and all(isinstance(m, dict) and set(m) == {'role', 'content'}
                        and m['role'] in ('system', 'user', 'assistant') and isinstance(m['content'], str)
                        for m in value['messages'])
                and type(value['temperature']) in (int, float) and math.isfinite(value['temperature'])
                and 0 <= value['temperature'] <= 2 and type(value['max_tokens']) is int and value['max_tokens'] > 0)
    if kind == 'tool_arguments':
        return (set(value) == {'expression'} and isinstance(value['expression'], str)
                and 0 < len(value['expression']) <= 100
                and re.fullmatch(r'(?:[0-9\s.+*/%()\-]|sqrt)+', value['expression']) is not None)
    if kind == 'tool_result':
        if set(value) != {'value'} or not isinstance(value['value'], str) or len(value['value']) > 100:
            return False
        try:
            return math.isfinite(float(value['value']))
        except ValueError:
            return False
    return set(value) == {'text'} and isinstance(value['text'], str)


def redact_payload(slot: PayloadSlot, value: object, provenance: Provenance, policy: ExportPolicy) -> RedactedPayload | ManifestEntry:
    def deny(reason):
        return ManifestEntry(slot=slot, state='unavailable', reason=reason)
    if not provenance.tenant_id:
        return deny('missing_tenant')
    if slot.kind in ('tool_arguments', 'tool_result') and (provenance.tool_name != 'calculator' or 'calculator' not in policy.allowed_tools):
        return deny('unapproved_tool')
    if not _shape(slot.kind, value, policy, provenance):
        return deny('unsupported_shape')
    if not provenance.approved(policy):
        return deny('unapproved_source')
    if slot.kind in ('input', 'retrieval_chunk'):
        digest = 'sha256:' + hashlib.sha256(value['text'].encode()).hexdigest()
        if not any(s.source_kind == slot.kind and s.source_hash == digest for s in provenance.sources):
            return deny('unapproved_source')
    # Bound input before regex work as well as serialized output after substitutions.
    if len(canonical_bytes(value)) > policy.payload_bytes:
        return deny('payload_too_large')

    def redact(text):
        text = private_key_pattern.sub('[REDACTED_PRIVATE_KEY]', text)
        if private_key_opening.search(text):
            raise ValueError('unmatched private key')
        text = assignment_pattern.sub(lambda m: m[1] + '[REDACTED_SECRET]', text)
        text = secret_pattern.sub('[REDACTED_SECRET]', text)
        text = email_pattern.sub('[REDACTED_EMAIL]', text)
        return phone_pattern.sub('[REDACTED_PHONE]', text)

    try:
        if slot.kind == 'model_request':
            safe = {**value, 'messages': [{**m, 'content': redact(m['content'])} for m in value['messages']]}
        else:
            safe = {key: redact(text) for key, text in value.items()}
        data = canonical_bytes(safe)
    except ValueError:
        return deny('redaction_failed')
    if len(data) > policy.payload_bytes:
        return deny('payload_too_large')
    return RedactedPayload(slot=slot, data=data, policy_version=policy.redaction_policy_version)
