"""Allowlisted typed metadata projection and immutable export sealing."""
import hashlib
import hmac
import json
import re

from .delivery_models import (ExportEnvelope, ManifestEntry, PreparedExport, SafeConfiguration,
                              SafeDependency, SafeModelCall, SafeOperations, SafeRetrieval,
                              SafeRetrievalItem, SafeRun, SafeToolStep)

HEX = re.compile(r'^(?:sha256:)?[a-f0-9]{64}$')
ERRORS = {'TimeoutError', 'CancelledError', 'ValueError', 'ConnectionError', 'RuntimeError'}
OUTCOMES = {'success', 'error', 'timeout', 'cancelled', 'partial', 'disabled', 'invalid', 'skipped'}
KNOWN_UNAVAILABLE = {'payloads_not_stored', 'corpus_index_acl_versions_not_configured',
                     'cost_not_calculated', 'identity_hash_key_not_configured'}


def _error(value):
    return None if value is None else value if value in ERRORS else 'OtherError'


def pseudonym(policy, tenant, domain, value):
    if not tenant or not policy.identity_key or not isinstance(value, str) or not value:
        return None
    message = json.dumps([domain, tenant, value], ensure_ascii=False, separators=(',', ':')).encode()
    return 'hmac-sha256:' + hmac.new(policy.identity_key.encode(), message, hashlib.sha256).hexdigest()


def _hash(value):
    return value if isinstance(value, str) and HEX.fullmatch(value) else None


def _allowed(value, choices):
    return value if isinstance(value, str) and value in choices else None


def _attributes(attributes, policy):
    result = {}
    for key, value in attributes.items():
        valid = False
        if key in {'cache.hit', 'planner.fallback'}:
            valid = type(value) is bool
        elif key in {'memory.message_count', 'retrieval.result_count', 'retrieval.limit', 'embedding.dimensions', 'retry.count'}:
            valid = type(value) is int and 0 <= value <= 2**31 - 1
        elif key == 'http.response.status_code':
            valid = type(value) is int and 100 <= value <= 599
        else:
            choices = {'cache.skip_reason': ('disabled', 'no_session', 'error', 'miss'),
                       'planner.action': ('retrieve', 'tool', 'respond', 'finish'),
                       'retrieval.vector_status': OUTCOMES, 'retrieval.graph_status': OUTCOMES,
                       'tool.name': policy.allowed_tools, 'db.collection.name': policy.allowed_collections,
                       'embedding.model': policy.allowed_models}.get(key, ())
            valid = isinstance(value, str) and value in choices
        if valid:
            result[key] = value
    return result


def project_metadata(event, policy, model_ordinals=()) -> SafeRun:
    def identity(domain, value):
        return pseudonym(policy, event.tenant_id, domain, value)
    def digest(domain, value):
        return identity(domain, _hash(value))
    if len(model_ordinals) != len(event.model_calls) or len(set(model_ordinals)) != len(model_ordinals):
        raise ValueError('explicit unique model ordinal mapping required')
    config = event.configuration
    configuration = SafeConfiguration(
        generation_model=_allowed(config.generation_model, policy.allowed_models),
        embedding_model=_allowed(config.embedding_model, policy.allowed_models),
        embedding_dimensions=config.embedding_dimensions,
        agent_build_sha=config.agent_build_sha if config.agent_build_sha and re.fullmatch(r'[a-f0-9]{40}|[a-f0-9]{64}', config.agent_build_sha) else None,
        fingerprint=_hash(config.fingerprint), corpus_version=_hash(config.corpus_version),
        index_snapshot=_hash(config.index_snapshot), acl_version=_hash(config.acl_version))
    return SafeRun(
        run_id=event.run_id, started_at=event.started_at, completed_at=event.completed_at, status=event.status,
        trace_id=event.trace_id if event.trace_id and re.fullmatch(r'[a-f0-9]{32}', event.trace_id) else None,
        tenant_partition=identity('tenant', event.tenant_id), session_pseudonym=identity('session', event.session_id),
        input_digest_pseudonym=digest('input_digest', event.input_hash), output_digest_pseudonym=digest('output_digest', event.output_hash),
        # event.user_id_hash is already an HMAC, but nothing here proves it was derived with
        # this policy's identity_key rather than some other key: exporting it unverified would
        # be a silent cross-key leak risk, so it stays null until a verifiable key binding
        # (e.g. a shared key fingerprint) exists. See Task 2 review notes.
        error_class=_error(event.error_type),
        configuration=configuration,
        retrieval=tuple(SafeRetrieval(collection=_allowed(r.collection, policy.allowed_collections),
            query_digest_pseudonym=digest('query_digest', r.query_hash), vector_status=r.vector_status, graph_status=r.graph_status,
            items=tuple(SafeRetrievalItem(rank=i.rank, score=i.score, document_pseudonym=identity('document', i.document_id),
                chunk_pseudonym=identity('chunk', i.chunk_id), content_digest_pseudonym=digest('content_digest', i.content_hash)) for i in r.items)) for r in event.retrieval),
        model_calls=tuple(SafeModelCall(ordinal=ordinal, purpose=_allowed(m.purpose, ('planner', 'generation', 'answer', 'unspecified')),
            model=_allowed(m.model, policy.allowed_models), request_digest_pseudonym=digest('request_digest', m.request_hash),
            input_tokens=m.input_tokens, output_tokens=m.output_tokens, cached_tokens=m.cached_tokens,
            status=m.status, latency_ms=m.latency_ms, provider_request_pseudonym=identity('provider_request', m.provider_request_id),
            completion_pseudonym=identity('completion', m.completion_id), error_class=_error(m.error_type), retry_count=m.retry_count)
            for ordinal, m in zip(model_ordinals, event.model_calls)),
        trajectory=tuple(SafeToolStep(step=t.step, tool_name=_allowed(t.tool_name, policy.allowed_tools), status=t.status,
            latency_ms=t.latency_ms, arguments_digest_pseudonym=digest('arguments_digest', t.arguments_hash),
            result_digest_pseudonym=digest('result_digest', t.result_hash), error_class=_error(t.error_type), retry_count=t.retry_count) for t in event.trajectory),
        dependencies=tuple(SafeDependency(sequence=d.sequence, name=_allowed(d.name, policy.allowed_dependencies),
            status=d.status, latency_ms=d.latency_ms, error_class=_error(d.error_type), attributes=_attributes(d.attributes, policy)) for d in event.dependencies),
        operations=SafeOperations(**{key: getattr(event.operations, key) for key in SafeOperations.model_fields}),
        unavailable=tuple(u for u in event.unavailable if u in KNOWN_UNAVAILABLE)
            + (() if event.tenant_id and policy.identity_key else ('identity_unavailable',)))


def prepare_export(event, policy, payloads, unavailable, model_ordinals=()) -> PreparedExport:
    accepted, omitted, used = [], list(unavailable), 0
    slots = set()
    for payload in payloads:
        if payload.slot in slots:
            raise ValueError('duplicate payload slot')
        slots.add(payload.slot)
        reason = None
        if payload.policy_version != policy.redaction_policy_version:
            raise ValueError('payload policy mismatch')
        if len(payload.data) > policy.payload_bytes:
            reason = 'payload_too_large'
        elif len(accepted) >= policy.payload_slots:
            reason = 'payload_count_exceeded'
        elif used + len(payload.data) > policy.run_bytes:
            reason = 'run_budget_exceeded'
        if reason:
            omitted.append(ManifestEntry(slot=payload.slot, state='unavailable', reason=reason))
        else:
            accepted.append(payload)
            used += len(payload.data)
    return PreparedExport(safe_run=project_metadata(event, policy, model_ordinals),
                          redaction_policy_version=policy.redaction_policy_version,
                          payloads=tuple(accepted), unavailable=tuple(omitted))


def seal_export(prepared, entries) -> ExportEnvelope:
    if {e.slot for e in entries} != {p.slot for p in prepared.payloads} or len(entries) != len(prepared.payloads):
        raise ValueError('every prepared payload requires exactly one outcome')
    manifest = (*prepared.unavailable, *entries)
    if len({e.slot for e in manifest}) != len(manifest):
        raise ValueError('duplicate manifest slot')
    for entry in entries:
        if entry.reference and entry.reference.policy_version != prepared.redaction_policy_version:
            raise ValueError('reference policy mismatch')
    return ExportEnvelope(run_id=prepared.safe_run.run_id, safe_run=prepared.safe_run,
        redaction_policy_version=prepared.redaction_policy_version, manifest=manifest,
        completeness='partial' if any(e.state == 'unavailable' for e in manifest) else 'complete' if manifest else 'metadata_only')
