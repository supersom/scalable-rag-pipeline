import pytest

from services.api.app.evaluation.delivery_models import PayloadSlot
from services.api.app.evaluation.redaction import _structure, redact_payload


def _nested(levels, leaf='v'):
    value = leaf
    for _ in range(levels):
        value = {'a': value}
    return value


def test_literal_approved_synthetic_input(policy, approved_input):
    out = redact_payload(PayloadSlot(kind='input', ordinal=1),
        {'text': 'Contact alice@example.test; token=fixture-secret'}, approved_input, policy)
    assert out.data == b'{"text":"Contact [REDACTED_EMAIL]; token=[REDACTED_SECRET]"}'
    assert out.policy_version == policy.redaction_policy_version


def test_nested_credential_key_denied(policy, approved_input):
    value = {'text': 'ok', 'meta': {'inner': {'api_key': 'x'}}}
    out = redact_payload(PayloadSlot(kind='output', ordinal=1), value, approved_input, policy)
    assert out.reason == 'unsupported_shape'


@pytest.mark.parametrize('key', ['Api-Key', 'ACCESS_TOKEN', 'Set-Cookie', 'Private_Key', 'PASSWORD', 'Authorization'])
def test_mixed_case_separator_forbidden_field_denied(policy, approved_input, key):
    value = {'text': 'ok', key: 'x'}
    out = redact_payload(PayloadSlot(kind='output', ordinal=1), value, approved_input, policy)
    assert out.reason == 'unsupported_shape'


def test_unknown_model_message_role_denied(policy, approved_input):
    value = {'messages': [{'role': 'tool', 'content': 'x'}], 'model': 'fake-chat', 'temperature': 0.1, 'max_tokens': 10}
    out = redact_payload(PayloadSlot(kind='model_request', ordinal=1), value, approved_input, policy)
    assert out.reason == 'unsupported_shape'


def test_unknown_model_message_key_denied(policy, approved_input):
    value = {'messages': [{'role': 'user', 'content': 'x', 'name': 'extra'}], 'model': 'fake-chat', 'temperature': 0.1, 'max_tokens': 10}
    out = redact_payload(PayloadSlot(kind='model_request', ordinal=1), value, approved_input, policy)
    assert out.reason == 'unsupported_shape'


def test_unknown_model_request_top_level_key_denied(policy, approved_input):
    value = {'messages': [{'role': 'user', 'content': 'x'}], 'model': 'fake-chat', 'temperature': 0.1, 'max_tokens': 10, 'stream': True}
    out = redact_payload(PayloadSlot(kind='model_request', ordinal=1), value, approved_input, policy)
    assert out.reason == 'unsupported_shape'


def test_model_request_message_content_redacted(policy, approved_input):
    value = {'messages': [{'role': 'user', 'content': 'call me at +15551234567'}],
             'model': 'fake-chat', 'temperature': 0.2, 'max_tokens': 50}
    out = redact_payload(PayloadSlot(kind='model_request', ordinal=1), value, approved_input, policy)
    assert out.data == (b'{"max_tokens":50,"messages":[{"content":"call me at [REDACTED_PHONE]",'
                        b'"role":"user"}],"model":"fake-chat","temperature":0.2}')


def test_bearer_jwt_aws_tokens_redacted(policy, provenance_for):
    out = redact_payload(PayloadSlot(kind='input', ordinal=1),
        {'text': 'Use Bearer abc123DEF456ghi, key AKIAIOSFODNN7EXAMPLE, and jwt '
                 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0ZXN0In0.c2lnbmF0dXJlMTIz for testing'},
        provenance_for('tokens'), policy)
    assert out.data == (b'{"text":"Use [REDACTED_SECRET], key [REDACTED_SECRET], '
                        b'and jwt [REDACTED_SECRET] for testing"}')


def test_private_key_block_redacted(policy, provenance_for):
    out = redact_payload(PayloadSlot(kind='input', ordinal=1),
        {'text': 'Key:\n-----BEGIN RSA PRIVATE KEY-----\nABCD1234\n-----END RSA PRIVATE KEY-----\nDone'},
        provenance_for('private'), policy)
    assert out.data == b'{"text":"Key:\\n[REDACTED_PRIVATE_KEY]\\nDone"}'


def test_unterminated_private_key_denied(policy, provenance_for):
    out = redact_payload(PayloadSlot(kind='input', ordinal=1),
        {'text': 'Key:\n-----BEGIN RSA PRIVATE KEY-----\nABCD1234 with no end'},
        provenance_for('broken'), policy)
    assert out.reason == 'redaction_failed'


@pytest.mark.parametrize('text,expected', [
    ('token=fixture-secret', 'token=[REDACTED_SECRET]'),
    ('Token: fixture-secret', 'Token: [REDACTED_SECRET]'),
    ('PASSWORD=fixture-secret', 'PASSWORD=[REDACTED_SECRET]'),
    ("api-key='fixture-secret'", "api-key=[REDACTED_SECRET]"),
])
def test_assignment_label_preserved_case_insensitive(policy, approved_input, text, expected):
    out = redact_payload(PayloadSlot(kind='output', ordinal=1), {'text': text}, approved_input, policy)
    assert out.data == ('{"text":"' + expected + '"}').encode()


def test_email_and_phone_redacted(policy, provenance_for):
    out = redact_payload(PayloadSlot(kind='input', ordinal=1),
        {'text': 'Reach me at bob@example.test or +15551234567'}, provenance_for('contacts'), policy)
    assert out.data == b'{"text":"Reach me at [REDACTED_EMAIL] or [REDACTED_PHONE]"}'


def test_email_pattern_bounded_to_254_characters(policy, approved_input):
    local, tld = 'a' * 64, 'c' * 63
    boundary_email = f"{local}@x{'b' * 123}y.{tld}"
    assert len(boundary_email) == 254
    out = redact_payload(PayloadSlot(kind='output', ordinal=1), {'text': boundary_email}, approved_input, policy)
    assert out.data == b'{"text":"[REDACTED_EMAIL]"}'

    oversized_email = f"{local}@x{'b' * 124}y.{tld}"
    assert len(oversized_email) == 255
    out = redact_payload(PayloadSlot(kind='output', ordinal=1), {'text': oversized_email}, approved_input, policy)
    assert out.data == ('{"text":"' + oversized_email + '"}').encode()


def test_overlap_assignment_precedes_email(policy, provenance_for):
    out = redact_payload(PayloadSlot(kind='input', ordinal=1),
        {'text': 'token=alice@example.test'}, provenance_for('overlap'), policy)
    assert out.data == b'{"text":"token=[REDACTED_SECRET]"}'


def test_structure_depth_boundary():
    assert _structure({'text': _nested(7)}) is True
    assert _structure({'text': _nested(8)}) is False


def test_redact_payload_denies_excessive_depth(policy, approved_input):
    out = redact_payload(PayloadSlot(kind='output', ordinal=1), {'text': _nested(8)}, approved_input, policy)
    assert out.reason == 'unsupported_shape'


def test_payload_too_large_denied(policy, approved_input):
    small = policy.model_copy(update={'payload_bytes': 16})
    out = redact_payload(PayloadSlot(kind='output', ordinal=1),
        {'text': 'this text is definitely too long for the tiny budget'}, approved_input, small)
    assert out.reason == 'payload_too_large'


@pytest.mark.parametrize('value', [
    {'text': 'x', 'API_KEY': 'secret'},
    {'text': {'Authorization': 'x'}},
    {'messages': [{'role': 'tool', 'content': 'x'}]},
])
def test_bad_shapes(policy, approved_input, value):
    assert redact_payload(PayloadSlot(kind='input', ordinal=1), value, approved_input, policy).reason == 'unsupported_shape'


def test_source_content_mismatch(policy, approved_input):
    assert redact_payload(PayloadSlot(kind='input', ordinal=1), {'text': 'unapproved'}, approved_input, policy).reason == 'unapproved_source'


def test_missing_tenant_denied(policy):
    from services.api.app.evaluation.provenance import Provenance
    out = redact_payload(PayloadSlot(kind='output', ordinal=1), {'text': 'ok'}, Provenance(), policy)
    assert out.reason == 'missing_tenant'


def test_unapproved_tool_denied(policy, provenance_for):
    out = redact_payload(PayloadSlot(kind='tool_arguments', ordinal=1),
        {'expression': '2+2'}, provenance_for(tool_name='other-tool'), policy)
    assert out.reason == 'unapproved_tool'


def test_tool_not_in_policy_allowlist_denied(policy, provenance_for):
    no_calculator = policy.model_copy(update={'allowed_tools': ()})
    out = redact_payload(PayloadSlot(kind='tool_arguments', ordinal=1),
        {'expression': '2+2'}, provenance_for(tool_name='calculator'), no_calculator)
    assert out.reason == 'unapproved_tool'


def test_calculator_arguments_and_result(policy, provenance_for):
    calc = provenance_for(tool_name='calculator')
    args = redact_payload(PayloadSlot(kind='tool_arguments', ordinal=1), {'expression': '2+2'}, calc, policy)
    assert args.data == b'{"expression":"2+2"}'
    result = redact_payload(PayloadSlot(kind='tool_result', ordinal=1), {'value': '4'}, calc, policy)
    assert result.data == b'{"value":"4"}'


@pytest.mark.parametrize('expression', ['x' * 101, 'DROP TABLE', '2+2; rm -rf /'])
def test_calculator_expression_grammar_denied(policy, provenance_for, expression):
    calc = provenance_for(tool_name='calculator')
    out = redact_payload(PayloadSlot(kind='tool_arguments', ordinal=1), {'expression': expression}, calc, policy)
    assert out.reason == 'unsupported_shape'


@pytest.mark.parametrize('value', ['inf', 'nan', 'ZeroDivisionError'])
def test_calculator_result_non_finite_or_error_denied(policy, provenance_for, value):
    calc = provenance_for(tool_name='calculator')
    out = redact_payload(PayloadSlot(kind='tool_result', ordinal=1), {'value': value}, calc, policy)
    assert out.reason == 'unsupported_shape'
