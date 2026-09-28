import time

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


@pytest.mark.parametrize('text', [
    'bob@mail.example.test',
    'see docs.python.org for details',
    'call server-a.internal.example.net now',
])
def test_dotted_hostnames_not_treated_as_jwt(policy, approved_input, text):
    # The JWT alternative only matches base64url text that begins with the "eyJ" literal
    # (the near-universal encoding of a JSON header's leading '{"'), so plain dotted
    # hostnames/domains must never be treated as bearer tokens.
    out = redact_payload(PayloadSlot(kind='output', ordinal=1), {'text': text}, approved_input, policy)
    assert b'REDACTED_SECRET' not in out.data


def test_jwt_pattern_is_bounded_against_adversarial_input(policy, approved_input):
    # A run of hyphens used to make the old generic dot-separated-run alternative back-track
    # catastrophically (~4.4s measured for this exact input). It should process quickly and
    # leave the text untouched (nothing here looks like a real secret).
    adversarial = 'a-' * 32000
    started = time.perf_counter()
    out = redact_payload(PayloadSlot(kind='output', ordinal=1), {'text': adversarial}, approved_input, policy)
    elapsed = time.perf_counter() - started
    assert elapsed < 1.0
    assert out.data == ('{"text":"' + adversarial + '"}').encode()


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


def test_email_with_short_local_and_long_domain_still_redacted(policy, approved_input):
    # The 254-char bound applies to the whole address, not to the domain component alone: a
    # short local part leaves room for a much longer domain and the total can still be well
    # under 254 chars. Regression test for a prior fix that capped the domain quantifier
    # directly and silently stopped matching addresses shaped like this one.
    text = 'a@x' + 'b' * 150 + 'y.io'
    assert len(text) == 157
    out = redact_payload(PayloadSlot(kind='output', ordinal=1), {'text': text}, approved_input, policy)
    assert out.data == b'{"text":"[REDACTED_EMAIL]"}'


def test_overlap_assignment_precedes_email(policy, provenance_for):
    out = redact_payload(PayloadSlot(kind='input', ordinal=1),
        {'text': 'token=alice@example.test'}, provenance_for('overlap'), policy)
    assert out.data == b'{"text":"token=[REDACTED_SECRET]"}'


def test_structure_depth_boundary():
    assert _structure({'text': _nested(7)}) is True
    assert _structure({'text': _nested(8)}) is False


@pytest.mark.parametrize('key', [
    'api_key', 'Api-Key', 'API_KEY', 'apiKey',
    'access_token', 'ACCESS-TOKEN', 'Access.Token',
    'refresh_token', 'Refresh-Token',
    'set-cookie', 'Set_Cookie', 'SETCOOKIE',
    'private-key', 'Private_Key',
    'password', 'PASSWORD',
    'secret', 'SECRET',
    'authorization', 'Authorization',
    'token', 'TOKEN',
])
def test_structure_denies_forbidden_key_variants_directly(key):
    # Exercises _structure's own normalization/denial directly, independent of any calling
    # shape check, so this fails if FORBIDDEN or the normalization logic regresses even
    # though the specific slot shapes used elsewhere would also reject the same input for
    # an unrelated reason (extra key, wrong type, etc).
    assert _structure({'safe': 'value', key: 'x'}) is False


def test_structure_denies_deeply_nested_forbidden_key_directly():
    assert _structure({'a': {'b': {'c': {'api_key': 'x'}}}}) is False
    assert _structure({'a': {'b': {'token': 'x'}}}) is False


def test_structure_allows_clean_nested_structure_directly():
    assert _structure({'a': {'b': {'c': 'ok'}}, 'd': [1, 2.5, 'three', True, None]}) is True


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


@pytest.mark.parametrize('text,expected', [
    ('client_secret=abc123', 'client_secret=[REDACTED_SECRET]'),
    ('CLIENT_SECRET: abc', 'CLIENT_SECRET: [REDACTED_SECRET]'),
    ('aws_secret_access_key=abc', 'aws_secret_access_key=[REDACTED_SECRET]'),
    ('auth_token=abc', 'auth_token=[REDACTED_SECRET]'),
    ('id_token=abc', 'id_token=[REDACTED_SECRET]'),
    ('session_token=abc', 'session_token=[REDACTED_SECRET]'),
    ('secret_key=abc', 'secret_key=[REDACTED_SECRET]'),
    ('clientSecret=abc', 'clientSecret=[REDACTED_SECRET]'),
    ('access_token=abc', 'access_token=[REDACTED_SECRET]'),
    ('x-api-key=abc', 'x-api-key=[REDACTED_SECRET]'),
    ('apikey=abc', 'apikey=[REDACTED_SECRET]'),
    ('mode=fast client_secret=abc', 'mode=fast client_secret=[REDACTED_SECRET]'),
    ('label=client_secret=abc', 'label=client_secret=[REDACTED_SECRET]'),
])
def test_compound_assignment_labels_redacted(policy, approved_input, text, expected):
    out = redact_payload(PayloadSlot(kind='output', ordinal=1), {'text': text}, approved_input, policy)
    assert out.data == ('{"text":"' + expected + '"}').encode()


@pytest.mark.parametrize('text', ['mode=fast', 'user_name=alice', 'tokens are words'])
def test_non_credential_labels_untouched(policy, approved_input, text):
    out = redact_payload(PayloadSlot(kind='output', ordinal=1), {'text': text}, approved_input, policy)
    assert out.data == ('{"text":"' + text + '"}').encode()


@pytest.mark.parametrize('adversarial', [
    'a_' * 32000, 'a-' * 32000, 'token_' * 10000, 'x' * 60000, 'a ' * 32000,
    ('_' * 120 + 'secret ') * 480, 'client_secret' * 4900, 'token:' * 10000,
])
def test_assignment_pattern_is_bounded_against_adversarial_input(policy, approved_input, adversarial):
    started = time.perf_counter()
    redact_payload(PayloadSlot(kind='output', ordinal=1), {'text': adversarial}, approved_input, policy)
    assert time.perf_counter() - started < 1.0
