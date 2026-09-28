import asyncio
import json
from uuid import uuid4

import pytest

from services.api.app.evaluation.context import RunEvidenceCollector, bind_collector, content_hash
from services.api.app.evaluation.delivery_models import ExportPolicy, FixtureRecord, PayloadSlot
from services.api.app.evaluation.models import ConfigurationFingerprint
from services.api.app.evaluation.provenance import Provenance
from test_capture import config


def fixture_policy(*sources):
    return ExportPolicy(identity_key='local-evidence-only', allowed_models=('fake-chat',),
        allowed_tools=('calculator',), synthetic_sources=tuple(FixtureRecord(
            tenant_id='synthetic-tenant', case_id='case', source_id=str(i),
            source_kind=kind, source_hash=content_hash(text), **identity)
            for i, (kind, text, identity) in enumerate(sources)))


def collector(run_id):
    return RunEvidenceCollector(run_id=str(run_id), session_id='session', input_text='question',
        tenant_id='synthetic-tenant', configuration=ConfigurationFingerprint())


def test_reverse_model_completion_preserves_slot_mapping(terminal_event, monkeypatch):
    from services.api.app.clients.ray_llm import RayLLMClient, LLMResult, settings
    from services.api.app.evaluation.capture import CapturePool, bind_capture
    monkeypatch.setattr(settings, 'LLM_MODEL_ID', 'fake-chat')
    policy = fixture_policy(('input', 'first', {}), ('input', 'second', {}))
    run_id = uuid4()
    session = CapturePool(config(), policy).open(run_id, 'synthetic-tenant', 'case')
    evidence = collector(run_id)
    client = RayLLMClient()
    async def run():
        release = asyncio.Event()
        async def complete(messages, *args):
            text = messages[0]['content']
            if text == 'first':
                await release.wait()
            else:
                release.set()
            return LLMResult(text=text + '-answer')
        monkeypatch.setattr(client, '_completion', complete)
        with bind_capture(session), bind_collector(evidence):
            results = await asyncio.gather(client.chat_completion_result([{'role': 'user', 'content': 'first'}]),
                                           client.chat_completion_result([{'role': 'user', 'content': 'second'}]))
        assert [r.text for r in results] == ['first-answer', 'second-answer']
    asyncio.run(run())
    captured = session.detach(evidence.finish('success'))
    assert [m.ordinal for m in captured.prepared.safe_run.model_calls] == [2, 1]
    payloads = {(p.slot.kind, p.slot.ordinal): p.data for p in captured.prepared.payloads}
    assert b'first-answer' in payloads['model_response', 1]
    assert b'second-answer' in payloads['model_response', 2]
    assert json.loads(payloads['model_request', 1]) == {
        'messages': [{'role': 'user', 'content': 'first'}], 'model': 'fake-chat',
        'temperature': 0.7, 'max_tokens': 1024}


@pytest.mark.parametrize('approved', [False, True])
def test_flattened_context_cannot_launder_sources(terminal_event, monkeypatch, approved):
    from services.api.app.agents.nodes import responder
    from services.api.app.clients.ray_llm import RayLLMClient, LLMResult, settings
    from services.api.app.evaluation.capture import CapturePool, bind_capture
    monkeypatch.setattr(settings, 'LLM_MODEL_ID', 'fake-chat')
    sources = [('input', 'question', {}), ('template', responder.RETRIEVAL_TEMPLATE,
                {'template_id': 'responder-retrieve-v1'})]
    if approved:
        sources.append(('history', 'context', {}))
    policy = fixture_policy(*sources)
    run_id = uuid4()
    session = CapturePool(config(), policy).open(run_id, 'synthetic-tenant', 'case')
    evidence = collector(run_id)
    client = RayLLMClient()
    async def complete(*args):
        return LLMResult(text='answer')
    monkeypatch.setattr(client, '_completion', complete)
    monkeypatch.setattr(responder, 'llm_client', client)
    async def run():
        with bind_capture(session), bind_collector(evidence):
            session.record(PayloadSlot(kind='input', ordinal=1), {'text': 'question'}, session.source('question'))
            result = await responder.generate_node({'current_query': 'question', 'documents': ['context']})
        assert result == {'messages': [{'role': 'assistant', 'content': 'answer'}]}
    asyncio.run(run())
    captured = session.detach(evidence.finish('success'))
    kinds = {p.slot.kind for p in captured.prepared.payloads}
    assert ('model_request' in kinds) == approved
    assert ('model_response' in kinds) == approved
    assert ('output' in kinds) == approved
    assert 'input' in kinds
    assert all(e.reason == 'unapproved_source' for e in captured.prepared.unavailable)


@pytest.mark.parametrize('approved_filename', [False, True])
def test_retrieval_filename_proof_precedes_flattening(terminal_event, approved_filename):
    from services.api.app.agents.nodes.retriever import _record_evidence, _item, RetrievalResult, CITATION_TEMPLATE, settings
    from services.api.app.evaluation.capture import CapturePool, bind_capture
    sources = [('input', 'query', {}), ('retrieval_chunk', 'chunk',
        {'collection': settings.QDRANT_COLLECTION, 'document_id': 'doc'}),
        ('template', CITATION_TEMPLATE, {'template_id': 'retrieval-citation-v1'})]
    if approved_filename:
        sources.append(('filename', 'synthetic.txt', {}))
    session = CapturePool(config(), fixture_policy(*sources)).open(uuid4(), 'synthetic-tenant', 'case')
    result = RetrievalResult([_item('qdrant', {'text': 'chunk', 'document_id': 'doc',
                                             'filename': 'synthetic.txt', 'safe': True}, 1)])
    with bind_capture(session):
        _record_evidence('query', result)
    assert session.has_payload(PayloadSlot(kind='retrieval_chunk', ordinal=1, item_ordinal=1))
    assert session.approved(session.source('chunk [Source: synthetic.txt]')) == approved_filename
    assert not session.approved(session.source('chunk [Source: forged.txt]'))


@pytest.mark.parametrize('tool_name, expression, result, approved', [
    ('calculator', '2+2', '4', True), ('calculator', '1/0', 'Error: private detail', False),
    ('calculator', '__import__("os")', '4', False), ('web_search', 'query', 'private web result', False)])
def test_tool_io_policy_and_derived_provenance(terminal_event, monkeypatch, tool_name, expression, result, approved):
    from services.api.app.agents.nodes import tool
    from services.api.app.evaluation.capture import CapturePool, bind_capture
    policy = fixture_policy(('input', expression, {}))
    run_id = uuid4()
    session = CapturePool(config(), policy).open(run_id, 'synthetic-tenant', 'case')
    evidence = collector(run_id)
    monkeypatch.setitem(tool.TOOL_REGISTRY, tool_name, {'handler': lambda value: result})
    async def run():
        with bind_capture(session), bind_collector(evidence):
            outcome = await tool.tool_node({'tool_name': tool_name, 'tool_input': expression})
        assert outcome['documents'] == [result]
    asyncio.run(run())
    assert session.approved(session.source(result)) == approved
    captured = session.detach(evidence.finish('success'))
    assert len(captured.event.trajectory) == 1
    if tool_name != 'calculator':
        assert [e.reason for e in captured.prepared.unavailable] == ['unapproved_tool', 'unapproved_tool']
    assert (len(captured.prepared.payloads) == 2) == approved
    kinds = {p.slot.kind for p in captured.prepared.payloads}
    # A result derived from denied arguments is never captured, even if it looks safe.
    assert ('tool_result' in kinds) == approved
    assert len(captured.prepared.payloads) + len(captured.prepared.unavailable) == 2


@pytest.mark.parametrize('approved_template', [False, True])
def test_planner_does_not_inherit_input_approval_for_template(terminal_event, monkeypatch, approved_template):
    from services.api.app.agents.nodes import planner
    from services.api.app.clients.ray_llm import RayLLMClient, LLMResult, settings
    from services.api.app.evaluation.capture import CapturePool, bind_capture
    monkeypatch.setattr(settings, 'LLM_MODEL_ID', 'fake-chat')
    sources = [('input', 'question', {})]
    if approved_template:
        sources.append(('template', planner.SYSTEM_PROMPT, {'template_id': 'planner-v1'}))
    session = CapturePool(config(), fixture_policy(*sources)).open(uuid4(), 'synthetic-tenant', 'case')
    client = RayLLMClient()
    async def complete(*args):
        return LLMResult(text='{"action":"retrieve","refined_query":"derived query"}')
    monkeypatch.setattr(client, '_completion', complete)
    monkeypatch.setattr(planner, 'llm_client', client)
    async def run():
        with bind_capture(session, session.source('question')):
            result = await planner.planner_node({'messages': [{'role': 'user', 'content': 'question'}]})
        assert result['current_query'] == 'derived query'
    asyncio.run(run())
    assert session.approved(session.source('derived query')) == approved_template


def test_disabled_capture_never_inspects_content(terminal_event, monkeypatch):
    from services.api.app.clients.ray_llm import RayLLMClient, LLMResult
    from services.api.app.evaluation.capture import CaptureSession
    def unexpected(*args, **kwargs):
        pytest.fail('disabled capture allocated or inspected content')
    monkeypatch.setattr(CaptureSession, 'messages_provenance', unexpected)
    client = RayLLMClient()
    calls = []
    async def complete(messages, temperature, max_tokens, span):
        calls.append((messages, temperature, max_tokens))
        return LLMResult(text='unchanged')
    monkeypatch.setattr(client, '_completion', complete)
    messages = [{'role': 'user', 'content': 'ordinary input'}]
    assert asyncio.run(client.chat_completion_result(messages)).text == 'unchanged'
    assert calls == [(messages, 0.7, 1024)]


def test_unapproved_tool_taints_nested_model_even_with_approved_outer_input(terminal_event, monkeypatch):
    from services.api.app.agents.nodes import tool
    from services.api.app.clients.ray_llm import RayLLMClient, LLMResult, settings
    from services.api.app.evaluation.capture import CapturePool, bind_capture
    monkeypatch.setattr(settings, 'LLM_MODEL_ID', 'fake-chat')
    session = CapturePool(config(), fixture_policy(('input', 'question', {}))).open(uuid4(), 'synthetic-tenant', 'case')
    client = RayLLMClient()
    async def complete(*args):
        return LLMResult(text='nested answer')
    async def handler(value):
        return (await client.chat_completion_result([{'role': 'user', 'content': value}])).text
    monkeypatch.setattr(client, '_completion', complete)
    monkeypatch.setitem(tool.TOOL_REGISTRY, 'graph_search', {'handler': handler})
    async def run():
        with bind_capture(session, session.source('question')):
            result = await tool.tool_node({'tool_name': 'graph_search', 'tool_input': 'question'})
        assert result['documents'] == ['nested answer']
    asyncio.run(run())
    assert not session.has_payload(PayloadSlot(kind='model_request', ordinal=1))
    assert not session.has_payload(PayloadSlot(kind='model_response', ordinal=1))
    assert not session.approved(session.source('nested answer'))


@pytest.mark.parametrize('content, approved', [('UNAPPROVED DOC CONTENT', False), ('question', True)])
def test_outer_binding_cannot_launder_inner_model_content(terminal_event, monkeypatch, content, approved):
    from services.api.app.clients.ray_llm import RayLLMClient, LLMResult, settings
    from services.api.app.evaluation.capture import CapturePool, bind_capture
    monkeypatch.setattr(settings, 'LLM_MODEL_ID', 'fake-chat')
    session = CapturePool(config(), fixture_policy(('input', 'question', {}))).open(uuid4(), 'synthetic-tenant', 'case')
    client = RayLLMClient()
    async def complete(*args):
        return LLMResult(text='inner answer')
    monkeypatch.setattr(client, '_completion', complete)
    async def run():
        with bind_capture(session, session.source('question')):
            assert session.approved(session.source('question'))
            await client.chat_completion_result([{'role': 'user', 'content': content}])
    asyncio.run(run())
    assert session.has_payload(PayloadSlot(kind='model_request', ordinal=1)) == approved
    assert session.has_payload(PayloadSlot(kind='model_response', ordinal=1)) == approved
    assert session.approved(session.source('inner answer')) == approved
    if not approved:
        assert [e.reason for e in session.unavailable] == ['unapproved_source', 'unapproved_source']


def test_explicit_call_provenance_cannot_escape_outer_denial(terminal_event, monkeypatch):
    from services.api.app.clients.ray_llm import RayLLMClient, LLMResult, settings
    from services.api.app.evaluation.capture import CapturePool, bind_capture
    monkeypatch.setattr(settings, 'LLM_MODEL_ID', 'fake-chat')
    session = CapturePool(config(), fixture_policy(('input', 'question', {}))).open(uuid4(), 'synthetic-tenant', 'case')
    client = RayLLMClient()
    async def complete(*args):
        return LLMResult(text='inner answer')
    monkeypatch.setattr(client, '_completion', complete)
    async def run():
        with bind_capture(session, session.unknown()):
            await client.chat_completion_result([{'role': 'user', 'content': 'question'}],
                                                provenance=session.source('question'))
    asyncio.run(run())
    assert not session.has_payload(PayloadSlot(kind='model_request', ordinal=1))
    assert not session.approved(session.source('inner answer'))
