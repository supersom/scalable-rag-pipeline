from uuid import uuid4

import pytest

from services.api.app.evaluation.delivery_config import DeliveryConfig
from services.api.app.evaluation.delivery_models import PayloadSlot
from services.api.app.evaluation.provenance import Provenance


def config(**limits):
    return DeliveryConfig(enabled=True, capture_content=True, environment='local',
        database_url='postgresql://local/test', queue_url='http://localhost/queue',
        identity_key='evidence-only', content_policy='restricted-synthetic-v1',
        fixture_manifest='local-fixture.json', fixture_case='literal', bucket='local-bucket', kms_key_id='local-key',
        s3_endpoint_url='http://localhost:5000', **limits)


def test_disabled_allocates_nothing(policy):
    from services.api.app.evaluation.capture import CapturePool
    pool = CapturePool(DeliveryConfig(), policy)
    assert pool.open(uuid4(), 'synthetic-tenant', 'literal') is None
    assert (pool.active_runs, pool.resident_bytes) == (0, 0)


def test_unknown_source_and_independent_input(policy, provenance_for, terminal_event):
    from services.api.app.evaluation.capture import CapturePool
    pool = CapturePool(config(), policy)
    approved_input = provenance_for('synthetic-question')
    session = pool.open(terminal_event.run_id, 'synthetic-tenant', 'synthetic-question')
    session.record(PayloadSlot(kind='model_request', ordinal=1),
        dict(messages=[dict(role='user', content='unapproved context')], model='fake-chat',
             temperature=0.0, max_tokens=10), Provenance(tenant_id='synthetic-tenant'))
    assert session.unavailable[0].reason == 'unapproved_source'
    session.record(PayloadSlot(kind='output', ordinal=1), {'text': 'derived'}, Provenance())
    session.record(PayloadSlot(kind='input', ordinal=1), {'text': 'synthetic question'}, approved_input)
    captured = session.detach(terminal_event)
    assert [p.slot.kind for p in captured.prepared.payloads] == ['input']
    assert session.resident_bytes == pool.resident_bytes == pool.active_runs == 0
    assert session.detach(terminal_event) is None
    session.record(PayloadSlot(kind='output', ordinal=1), {'text': 'late'}, approved_input)
    session.close()
    assert pool.resident_bytes == 0


def test_limits_and_immutable_inspection(policy, approved_input):
    from services.api.app.evaluation.capture import CapturePool
    pool = CapturePool(config(active_runs=1, payload_slots=2, payload_bytes=64,
                              run_bytes=64, active_bytes=64), policy)
    session = pool.open(uuid4(), 'synthetic-tenant', 'literal')
    assert pool.open(uuid4(), 'synthetic-tenant', 'literal') is None
    for ordinal in range(1, 100):
        session.record(PayloadSlot(kind='model_response', ordinal=ordinal), {'text': 'x' * 40}, approved_input)
    assert session.resident_bytes <= 64
    assert len(session.unavailable) <= 2
    assert isinstance(session.unavailable, tuple)
    assert any(e.reason == 'payload_count_exceeded' for e in session.unavailable)
    session.close()
    assert (pool.active_runs, pool.resident_bytes) == (0, 0)


@pytest.mark.parametrize('value, reason', [({'text': 'x' * 70000}, 'payload_too_large'),
    ({'text': {'a': {'b': []}}}, 'unsupported_shape')])
def test_bad_payloads_are_fixed_omissions(policy, approved_input, value, reason):
    from services.api.app.evaluation.capture import CapturePool
    session = CapturePool(config(), policy).open(uuid4(), 'synthetic-tenant', 'literal')
    session.record(PayloadSlot(kind='model_response', ordinal=1), value, approved_input)
    assert session.unavailable[0].reason == reason
    assert session.resident_bytes == 0


@pytest.mark.parametrize('tenant', [None, '', '../tenant', 'x' * 129, 123])
def test_invalid_tenant_denies_session(policy, tenant):
    from services.api.app.evaluation.capture import CapturePool
    assert CapturePool(config(), policy).open(uuid4(), tenant, 'literal') is None


def test_provenance_tenant_and_case_cannot_override_session(policy, approved_input):
    from services.api.app.evaluation.capture import CapturePool
    session = CapturePool(config(), policy).open(uuid4(), 'other-tenant', 'literal')
    session.record(PayloadSlot(kind='input', ordinal=1), {'text': 'synthetic question'}, approved_input)
    assert session.unavailable[0].reason == 'unapproved_source'


def test_assignment_labels_redacted(policy, approved_input):
    from services.api.app.evaluation.redaction import redact_payload
    result = redact_payload(PayloadSlot(kind='output', ordinal=1),
        {'text': 'access_token=alpha refresh-token=beta SECRET=gamma private_key=delta'}, approved_input, policy)
    assert result.data == b'{"text":"access_token=[REDACTED_SECRET] refresh-token=[REDACTED_SECRET] SECRET=[REDACTED_SECRET] private_key=[REDACTED_SECRET]"}'


def test_all_64_slots_usable_and_overflow_is_one_fixed_marker(policy, approved_input, terminal_event):
    from services.api.app.evaluation.capture import CapturePool
    session = CapturePool(config(), policy).open(terminal_event.run_id, 'synthetic-tenant', 'literal')
    for ordinal in range(1, 65):
        session.record(PayloadSlot(kind='model_response', ordinal=ordinal), {'text': 'safe'}, approved_input)
    assert not session.unavailable
    for ordinal in range(65, 1000):
        session.record(PayloadSlot(kind='model_response', ordinal=ordinal), {'text': 'safe'}, approved_input)
    assert len(session.unavailable) == 1
    assert session.unavailable[0].reason == 'payload_count_exceeded'
    captured = session.detach(terminal_event)
    assert len(captured.prepared.payloads) == 64
    assert len(captured.prepared.unavailable) == 1


def test_active_byte_cap_is_independent_and_release_exactly_once(policy, approved_input):
    from services.api.app.evaluation.capture import CapturePool
    pool = CapturePool(config(payload_bytes=64, run_bytes=64, active_bytes=64), policy)
    one = pool.open(uuid4(), 'synthetic-tenant', 'literal')
    two = pool.open(uuid4(), 'synthetic-tenant', 'literal')
    slot = PayloadSlot(kind='output', ordinal=1)
    one.record(slot, {'text': 'x' * 40}, approved_input)
    two.record(slot, {'text': 'x' * 40}, approved_input)
    assert one.resident_bytes == pool.resident_bytes > 0
    assert two.unavailable[0].reason == 'run_budget_exceeded'
    one.close()
    one.close()
    assert pool.resident_bytes == 0 and pool.active_runs == 1
    two.close()
    assert pool.active_runs == 0


def test_malformed_unicode_and_deep_or_cyclic_payloads_are_fail_open(policy, approved_input):
    from services.api.app.evaluation.capture import CapturePool
    session = CapturePool(config(), policy).open(uuid4(), 'synthetic-tenant', 'literal')
    cyclic = []
    cyclic.append(cyclic)
    for ordinal, value in enumerate(({'text': '\ud800'}, {'text': cyclic},
                                     {'messages': [{}] * 10000}), 1):
        session.record(PayloadSlot(kind='model_response', ordinal=ordinal), value, approved_input)
    assert [e.reason for e in session.unavailable] == ['redaction_failed', 'unsupported_shape', 'unsupported_shape']
    assert not session.approved(session.source('\ud800'))
    assert session.resident_bytes == 0


def test_export_uses_session_tenant_and_preserves_original_snapshot(policy, approved_input, terminal_event):
    from services.api.app.evaluation.capture import CapturePool
    from services.api.app.evaluation.metadata_export import pseudonym
    event = terminal_event.model_copy(update={'tenant_id': 'forged-request-tenant'})
    before = event.model_dump_json()
    session = CapturePool(config(), policy).open(event.run_id, 'synthetic-tenant', 'literal')
    result = session.detach(event)
    assert result.event.model_dump_json() == before
    assert result.prepared.safe_run.tenant_partition == pseudonym(policy, 'synthetic-tenant', 'tenant', 'synthetic-tenant')


def test_redaction_is_immediate_and_does_not_retain_mutable_raw_value(policy, approved_input, terminal_event):
    from services.api.app.evaluation.capture import CapturePool
    session = CapturePool(config(), policy).open(terminal_event.run_id, 'synthetic-tenant', 'literal')
    value = {'text': 'password=synthetic-sensitive-value'}
    session.record(PayloadSlot(kind='output', ordinal=1), value, approved_input)
    value['text'] = 'changed after capture'
    captured = session.detach(terminal_event)
    assert captured.prepared.payloads[0].data == b'{"text":"password=[REDACTED_SECRET]"}'


def test_redaction_failure_never_raises_to_chat(policy, approved_input, monkeypatch):
    from services.api.app.evaluation import capture
    session = capture.CapturePool(config(), policy).open(uuid4(), 'synthetic-tenant', 'literal')
    def fail(*args):
        raise RuntimeError('private error detail')
    monkeypatch.setattr(capture, 'redact_payload', fail)
    session.record(PayloadSlot(kind='output', ordinal=1), {'text': 'safe'}, approved_input)
    assert session.unavailable[0].reason == 'redaction_failed'
    assert 'private' not in str(session.unavailable)


def test_reverse_mapping_stays_correct_after_64_completions(policy, approved_input, terminal_event):
    from services.api.app.evaluation.capture import CapturePool
    from services.api.app.evaluation.models import ModelCall
    session = CapturePool(config(), policy).open(terminal_event.run_id, 'synthetic-tenant', 'literal')
    for unused in range(100):
        session.begin_model(approved_input)
    for index, ordinal in enumerate(range(100, 0, -1)):
        session.finish_model(ordinal, index)
    event = terminal_event.model_copy(update={'model_calls': [
        ModelCall(status='success', request_hash='sha256:' + 'a' * 64) for _ in range(100)]})
    captured = session.detach(event)
    assert [m.ordinal for m in captured.prepared.safe_run.model_calls][-64:] == list(range(64, 0, -1))
    assert len({m.ordinal for m in captured.prepared.safe_run.model_calls}) == 100


def test_redaction_runs_outside_the_process_global_pool_lock(policy, approved_input, monkeypatch):
    import threading
    from services.api.app.evaluation import capture
    pool = capture.CapturePool(config(), policy)
    session = pool.open(uuid4(), 'synthetic-tenant', 'literal')
    real, observed = capture.redact_payload, []
    def probe(*args):
        acquired = []
        def other_session():
            acquired.append(pool._lock.acquire(timeout=1))
            if acquired[0]:
                pool._lock.release()
        worker = threading.Thread(target=other_session)
        worker.start()
        worker.join()
        observed.append(acquired[0])
        return real(*args)
    monkeypatch.setattr(capture, 'redact_payload', probe)
    session.record(PayloadSlot(kind='output', ordinal=1), {'text': 'safe'}, approved_input)
    assert observed == [True]
    assert session.has_payload(PayloadSlot(kind='output', ordinal=1))


def test_record_after_close_during_redaction_stores_nothing(policy, approved_input, monkeypatch):
    from services.api.app.evaluation import capture
    pool = capture.CapturePool(config(), policy)
    session = pool.open(uuid4(), 'synthetic-tenant', 'literal')
    real = capture.redact_payload
    def close_then_redact(*args):
        session.close()
        return real(*args)
    monkeypatch.setattr(capture, 'redact_payload', close_then_redact)
    session.record(PayloadSlot(kind='output', ordinal=1), {'text': 'safe'}, approved_input)
    assert (session.resident_bytes, pool.resident_bytes, pool.active_runs) == (0, 0, 0)
    assert not session.has_payload(PayloadSlot(kind='output', ordinal=1))
