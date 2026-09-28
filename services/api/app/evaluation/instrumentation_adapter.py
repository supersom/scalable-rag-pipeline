"""Request-local bridge from safe domain records to the existing evidence plane."""

from __future__ import annotations

from contextlib import contextmanager

from libs.observability import tracing
from libs.observability.operation_contracts import ContentCandidate, OperationRecord

from .capture import bind_capture, current_provenance
from .context import bind_collector, model_purpose
from .delivery_models import PayloadSlot
from .models import ContentSlotRef, ModelCall, OperationEvidence, RetrievalEvidence, RetrievedEvidence


class CaptureProvenance:
    """Expose provenance operations without exposing the capture session to call sites."""

    def __init__(self, capture):
        self._capture = capture

    def source(self, value):
        return self._capture.source(value)

    def template(self, value, *, template_id):
        return self._capture.proof('template', value, template_id=template_id)

    def proof(self, kind, value, **identity):
        return self._capture.proof(kind, value, **identity)

    def messages(self, messages, *, explicit=None):
        proof = explicit if explicit is not None else self._capture.messages_provenance(messages)
        ambient = current_provenance.get()
        return self._capture.combine(ambient, proof) if ambient is not None else proof

    def combine(self, *proofs):
        return self._capture.combine(*proofs)

    def remember(self, value, proof):
        return self._capture.remember(value, proof)


class EvidenceInstrumentation:
    """Synchronous and per-run; no SDK or database I/O on a domain span."""

    def __init__(self, collector, capture):
        self.collector = collector
        self.capture = capture
        self._model_requests = {}
        self._retrievals = {}
        self._tools = {}
        self._generic_ordinals = {}
        self._generic_items = {}
        self._model_next = 0

    def _capture_slot(self, slot, candidate: ContentCandidate | None, *, text_wrapper=False):
        if self.capture is None:
            return
        if candidate is None:
            self.capture.omit(slot, 'not_observed')
            return
        value = {'text': candidate.value} if text_wrapper and isinstance(candidate.value, str) else candidate.value
        self.capture.record(slot, value, candidate.provenance)

    def _generic_content(self, record, content):
        if self.capture is None or not content:
            return {}
        ordinal = self._generic_ordinals.get(record.operation_id)
        if ordinal is None:
            ordinal = self.capture.next_ordinal('operation')
            self._generic_ordinals[record.operation_id] = ordinal
        slots = {}
        for name, candidate in content.items():
            item = self._generic_items.get(record.operation_id, 0) + 1
            self._generic_items[record.operation_id] = item
            slot = PayloadSlot(kind='operation_content', ordinal=ordinal, item_ordinal=item)
            self._capture_slot(slot, candidate, text_wrapper=True)
            slots[name] = ContentSlotRef(ordinal=ordinal, item_ordinal=item)
        return slots

    def record(self, record: OperationRecord, content: dict[str, ContentCandidate]) -> None:
        fields = dict(record.facts)
        generic = record.name not in {'llm.chat', 'retrieval', 'tool.execute'}
        content_slots = self._generic_content(record, content) if generic else {}
        self.collector.record_event(OperationEvidence(
            operation_id=record.operation_id, parent_operation_id=record.parent_operation_id,
            name=record.name, schema_version=record.schema_version, phase=record.phase,
            sequence=record.sequence, timestamp=record.timestamp,
            status=fields.get('status') if fields.get('status') in {
                'success', 'error', 'timeout', 'cancelled', 'partial', 'disabled', 'invalid', 'skipped'} else None,
            latency_ms=fields.get('latency_ms'), facts=fields, content_slots=content_slots,
        ))
        if record.name == 'llm.chat':
            self._model(record, fields, content)
        elif record.name == 'retrieval':
            self._retrieval(record, fields, content)
        elif record.name == 'tool.execute':
            self._tool(record, fields, content)

    def _model(self, record, fields, content):
        if record.phase == 'request':
            provenance = content.get('request').provenance if 'request' in content else None
            if self.capture is not None:
                ordinal = self.capture.begin_model(provenance)
                self._capture_slot(PayloadSlot(kind='model_request', ordinal=ordinal), content.get('request'))
                if not self.capture.has_payload(PayloadSlot(kind='model_request', ordinal=ordinal)):
                    provenance = self.capture.unknown()
            else:
                self._model_next += 1
                ordinal = self._model_next
            self._model_requests[record.operation_id] = (fields, ordinal, provenance)
        elif record.phase == 'result':
            request = self._model_requests.pop(record.operation_id, None)
            if request is None:
                return
            request_fields, ordinal, provenance = request
            index = self.collector.record_model_call(ModelCall(
                purpose=request_fields.get('purpose') or model_purpose.get() or 'unspecified',
                request_hash=request_fields['request_hash'],
                requested_model=request_fields.get('requested_model'),
                model=fields.get('model'), input_tokens=fields.get('input_tokens'),
                output_tokens=fields.get('output_tokens'), cached_tokens=fields.get('cached_tokens'),
                finish_reason=fields.get('finish_reason'),
                provider_request_id=fields.get('provider_request_id'),
                completion_id=fields.get('completion_id'),
                status=fields.get('status', 'success'), error_type=fields.get('error_type'),
                retry_count=fields.get('retry_count', 0), latency_ms=fields.get('latency_ms'),
            ))
            if self.capture is not None:
                candidate = content.get('response')
                response = ContentCandidate(candidate.value, provenance) if candidate is not None else None
                self._capture_slot(PayloadSlot(kind='model_response', ordinal=ordinal), response)
                if response is not None:
                    self.capture.remember(response.value['text'], provenance)
                self.capture.finish_model(ordinal, index)

    def _retrieval(self, record, fields, content):
        key = record.operation_id
        if record.phase == 'request':
            ordinal = self.capture.next_ordinal('retrieval') if self.capture is not None else None
            self._retrievals[key] = {'request': fields, 'items': [], 'ordinal': ordinal}
            if ordinal is not None:
                self._capture_slot(PayloadSlot(kind='retrieval_query', ordinal=ordinal),
                                   content.get('query'), text_wrapper=True)
        elif record.phase == 'hit':
            state = self._retrievals.get(key)
            if state is None:
                return
            state['items'].append(RetrievedEvidence(**fields))
            if state['ordinal'] is not None:
                self._capture_slot(PayloadSlot(kind='retrieval_chunk', ordinal=state['ordinal'],
                                               item_ordinal=len(state['items'])),
                                   content.get('chunk'), text_wrapper=True)
        elif record.phase == 'result':
            state = self._retrievals.pop(key, None)
            if state is None:
                return
            self.collector.record_retrieval(RetrievalEvidence(
                query_hash=state['request']['query_hash'],
                collection=fields.get('collection', state['request'].get('collection')),
                vector_status=fields.get('vector_status', 'success'),
                graph_status=fields.get('graph_status', 'success'),
                graph_error_type=fields.get('graph_error_type'), items=state['items'],
            ))

    def _tool(self, record, fields, content):
        key = record.operation_id
        if record.phase == 'request':
            ordinal = self.capture.next_ordinal('tool') if self.capture is not None else None
            self._tools[key] = {'request': fields, 'ordinal': ordinal, 'started_at': record.timestamp}
            if ordinal is not None:
                self._capture_slot(PayloadSlot(kind='tool_arguments', ordinal=ordinal), content.get('arguments'))
        elif record.phase == 'result':
            state = self._tools.pop(key, None)
            if state is None:
                return
            request = state['request']
            self.collector.record_tool_step(
                tool_name=request.get('tool_name', 'unknown'), registered=request.get('registered'),
                arguments_hash=request.get('arguments_hash'), result_hash=fields.get('result_hash'),
                started_at=state['started_at'], latency_ms=fields.get('latency_ms', 0.0),
                status=fields.get('status', 'success'), error_type=fields.get('error_type'),
                retry_count=fields.get('retry_count', 0),
            )
            if state['ordinal'] is not None:
                self._capture_slot(PayloadSlot(kind='tool_result', ordinal=state['ordinal']), content.get('result'))


@contextmanager
def bind_instrumentation(collector, capture, *, adapter: EvidenceInstrumentation | None = None):
    """Bind both old and rich observers only while a request is executing."""
    from .operation_schemas import register_api_operation_schemas
    register_api_operation_schemas()
    owned = adapter or EvidenceInstrumentation(collector, capture)
    if owned.collector is not collector or owned.capture is not capture:
        raise ValueError('instrumentation adapter belongs to another run')
    with bind_collector(collector), bind_capture(capture, current_provenance.get()):
        record_token = tracing.record_observer.set(owned.record)
        proof_token = tracing.provenance_port.set(CaptureProvenance(capture) if capture is not None else None)
        try:
            yield owned
        finally:
            tracing.provenance_port.reset(proof_token)
            tracing.record_observer.reset(record_token)
