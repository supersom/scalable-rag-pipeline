"""Test-owned HTTP server and disposable namespaces. Never consume a shared queue."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4

import boto3
import httpx
from botocore.config import Config
from botocore.exceptions import ClientError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from services.api.app.evaluation.bounded_io import BoundedIO
from services.api.app.evaluation.delivery_config import DeliveryConfig
from services.api.app.evaluation.migrate import migrate
from services.api.app.evaluation.outbox import OutboxRepository
from services.api.app.evaluation.payloads import S3PayloadStore
from services.api.app.evaluation.publisher import RunEvidencePublisher, parse_envelope
from services.api.app.evaluation.runtime import EvidenceRuntime
from services.evaluation_delivery.worker import DeliveryWorker

LITERAL = 'Contact alice@example.test; token=fixture-secret'


class LiveDelivery:
    def __init__(self, values, policy, monkeypatch):
        self.values, self.policy, self.monkeypatch = values, policy, monkeypatch
        self.name = 'eval_test_' + uuid4().hex[:16]
        self.bucket = self.name.replace('_', '-')
        self.clock = lambda: self.now
        self.now = datetime.now(timezone.utc)
        self.created_schema = self.created_bucket = False
        self.queue = None
        self.engine = create_async_engine(values['EVAL_TEST_DATABASE_URL'], poolclass=NullPool,
            connect_args={'server_settings': {'search_path': self.name}})
        self.admin = create_async_engine(values['EVAL_TEST_DATABASE_URL'], poolclass=NullPool)
        kwargs = dict(region_name='us-east-1', aws_access_key_id='test', aws_secret_access_key='test',
                      config=Config(connect_timeout=1, read_timeout=2, retries={'max_attempts': 0}))
        self.s3 = boto3.client('s3', endpoint_url=values['EVAL_TEST_S3_ENDPOINT'], **kwargs)
        self.sqs = boto3.client('sqs', endpoint_url=values['EVAL_TEST_SQS_ENDPOINT'], **kwargs)

    async def sql(self, statement):
        async with self.engine.begin() as conn:
            return (await conn.execute(text(statement))).all()

    def setup(self):
        # S3 CreateBucket (in us-east-1) and SQS CreateQueue can succeed for an
        # existing resource owned by these credentials. Check both names before
        # creating anything, including the disposable database schema.
        try:
            self.s3.head_bucket(Bucket=self.bucket)
        except ClientError as exc:
            if exc.response['Error']['Code'] not in ('404', 'NoSuchBucket', 'NotFound'):
                raise
        else:
            raise AssertionError('test evidence bucket name already exists')
        try:
            self.sqs.get_queue_url(QueueName=self.name)
        except ClientError as exc:
            if exc.response['Error']['Code'] not in ('AWS.SimpleQueueService.NonExistentQueue',
                                                     'QueueDoesNotExist'):
                raise
        else:
            raise AssertionError('test evidence queue name already exists')

        async def db():
            async with self.admin.begin() as conn:
                await conn.execute(text(f'CREATE SCHEMA "{self.name}"'))
            self.created_schema = True
            await migrate(self.engine)
        asyncio.run(db())
        self.s3.create_bucket(Bucket=self.bucket)
        self.created_bucket = True
        self.s3.put_bucket_versioning(Bucket=self.bucket, VersioningConfiguration={'Status': 'Enabled'})
        self.queue = self.sqs.create_queue(QueueName=self.name)['QueueUrl']
        # Check explicitly requested model service before starting our own API.
        response = httpx.get(self.values['EVAL_TEST_MODEL_URL'] + '/health', timeout=3)
        response.raise_for_status()
        self.repo = OutboxRepository(async_sessionmaker(self.engine, expire_on_commit=False), self.clock)
        from prometheus_client import CollectorRegistry
        from services.api.app.evaluation.delivery_metrics import DeliveryMetrics
        self.registry = CollectorRegistry()
        self.metrics = DeliveryMetrics(self.registry)
        self.store = S3PayloadStore(self.s3, bucket=self.bucket, kms_key_id='local-test-key',
                                    environment='local', io=BoundedIO(2), clock=self.clock)
        self.publisher = RunEvidencePublisher(self.sqs, queue_url=self.queue, io=BoundedIO(2),
                                               max_body_bytes=131072, metrics=self.metrics)

    def cleanup(self):
        async def close():
            if hasattr(self, 'store'):
                await self.store.close(time.monotonic() + 3)
                await self.publisher.close(time.monotonic() + 3)
            if self.created_schema:
                async with self.admin.begin() as conn:
                    await conn.execute(text(f'DROP SCHEMA "{self.name}" CASCADE'))
            await self.engine.dispose()
            await self.admin.dispose()
        asyncio.run(close())
        if self.queue:
            self.sqs.delete_queue(QueueUrl=self.queue)
        if self.created_bucket:
            for page in self.s3.get_paginator('list_object_versions').paginate(Bucket=self.bucket):
                versions = page.get('Versions', []) + page.get('DeleteMarkers', [])
                if versions:
                    self.s3.delete_objects(Bucket=self.bucket, Delete={'Objects': [
                        {'Key': v['Key'], 'VersionId': v['VersionId']} for v in versions]})
            self.s3.delete_bucket(Bucket=self.bucket)
        self.s3.close()
        self.sqs.close()

    async def wait_run(self, run_id):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            async with self.engine.begin() as conn:
                await conn.execute(text('SET TRANSACTION READ ONLY'))
                row = (await conn.execute(text('SELECT envelope FROM evaluation_runs WHERE run_id=:id'),
                                          {'id': run_id})).first()
            if row:
                data = bytes(row[0])
                return parse_envelope(data), data
            await asyncio.sleep(.02)
        raise AssertionError('accepted HTTP run did not commit before fixed deadline')

    async def publish(self, repo=None):
        return await DeliveryWorker(repo or self.repo, self.publisher, self.clock, metrics=self.metrics).run_once()

    def publish_in_fresh_process(self):
        """Restart with only committed PG state, an endpoint and an injected test clock."""
        env = dict(os.environ, EVAL_ACCEPT_DB=self.values['EVAL_TEST_DATABASE_URL'],
                   EVAL_ACCEPT_SCHEMA=self.name, EVAL_ACCEPT_QUEUE=self.queue,
                   EVAL_ACCEPT_CLOCK=self.now.isoformat(), AWS_ACCESS_KEY_ID='test', AWS_SECRET_ACCESS_KEY='test',
                   AWS_DEFAULT_REGION='us-east-1', AWS_EC2_METADATA_DISABLED='true',
                   PYTHONPATH=str(Path(__file__).resolve().parents[2]))
        result = subprocess.run([sys.executable, __file__], env=env, capture_output=True, timeout=15)
        assert result.returncode == 0, 'test-owned restarted worker failed'

    def receive(self):
        messages = self.sqs.receive_message(QueueUrl=self.queue, MaxNumberOfMessages=1,
                                           MessageAttributeNames=['All'], WaitTimeSeconds=1).get('Messages', [])
        assert len(messages) == 1, 'test-owned queue has no delivery'
        self.sqs.delete_message(QueueUrl=self.queue, ReceiptHandle=messages[0]['ReceiptHandle'])
        return messages[0]

    @asynccontextmanager
    async def api(self, mode='normal', repo=None, store=None, **limits):
        import uvicorn
        from fastapi import FastAPI
        from jose import jwt
        from services.api.app.routes import chat
        from services.api.app.agents.nodes import responder
        from services.api.app.clients.ray_llm import RayLLMClient
        from services.api.app.config import settings
        from services.api.app.evaluation import runtime as runtime_module

        class Cache:
            async def get_cached_response(self, message):
                return LITERAL if mode == 'cache' else None
            async def set_cached_response(self, *args):
                pass
        class Memory:
            async def get_history(self, *args, **kwargs):
                return []
            async def add_message(self, *args):
                pass
        class Graph:
            async def astream(self, state, config):
                yield {'planner': {'action': 'direct_answer'}}
                state['action'] = 'direct_answer'
                if mode == 'unapproved':
                    state['current_query'] = LITERAL + ' unapproved flattened source'
                yield {'responder': await responder.generate_node(state, config)}

        cfg = DeliveryConfig(enabled=True, capture_content=True, environment='local',
            database_url=self.values['EVAL_TEST_DATABASE_URL'], queue_url=self.queue,
            identity_key=self.policy.identity_key, content_policy='restricted-synthetic-v1',
            fixture_manifest='server-configured-manifest', fixture_case='literal', bucket=self.bucket,
            kms_key_id='local-test-key', s3_endpoint_url=self.values['EVAL_TEST_S3_ENDPOINT'], **limits)
        runtime = EvidenceRuntime(cfg, self.policy, repo or self.repo, store or self.store, metrics=self.metrics)
        self.monkeypatch.setattr(runtime_module, '_active', runtime)
        self.monkeypatch.setattr(chat, 'agent_app', Graph())
        self.monkeypatch.setattr(settings, 'LLM_MODEL_NAME', 'fake-chat')
        self.monkeypatch.setattr(settings, 'LLM_MODEL_ID', None)
        self.monkeypatch.setattr(settings, 'LLM_API_KEY', None)
        llm = RayLLMClient()
        llm.endpoint = self.values['EVAL_TEST_MODEL_URL'] + '/v1/chat/completions'
        await llm.start()
        self.monkeypatch.setattr(responder, 'llm_client', llm)
        app = FastAPI()
        app.include_router(chat.router, prefix='/chat')
        app.dependency_overrides[chat.get_semantic_cache] = lambda: Cache()
        app.dependency_overrides[chat.get_memory] = lambda: Memory()
        app.dependency_overrides[chat.get_llm_client] = lambda: llm
        endpoint = urlsplit(self.values['EVAL_TEST_API_URL'])
        sock = socket.socket()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((endpoint.hostname, endpoint.port))
        server = uvicorn.Server(uvicorn.Config(app, log_level='error', access_log=False, lifespan='off'))
        await runtime.start()
        task = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            deadline = time.monotonic() + 3
            while not server.started:
                if task.done():
                    await task
                assert time.monotonic() < deadline, 'test-owned API did not start'
                await asyncio.sleep(.01)
            async with httpx.AsyncClient(base_url=self.values['EVAL_TEST_API_URL'], timeout=10) as client:
                async def post(tenant='synthetic-tenant'):
                    claims = {'sub': 'synthetic-user', 'exp': time.time() + 60}
                    if tenant:
                        claims['tenant_id'] = tenant
                    token = jwt.encode(claims, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
                    return await client.post('/chat/stream', json={'message': LITERAL},
                                             headers={'Authorization': 'Bearer ' + token})
                async def disconnect(at):
                    claims = {'sub': 'synthetic-user', 'tenant_id': 'synthetic-tenant', 'exp': time.time() + 60}
                    token = jwt.encode(claims, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
                    blocked = asyncio.Event()
                    sent = []
                    received = False
                    async def receive():
                        nonlocal received
                        if not received:
                            received = True
                            return {'type': 'http.request', 'body': json.dumps({'message': LITERAL}).encode()}
                        await blocked.wait()
                        return {'type': 'http.disconnect'}
                    async def send(message):
                        sent.append(message)
                        if message['type'] == at:
                            blocked.set()
                            await asyncio.Event().wait()
                    await asyncio.wait_for(app({'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.0'},
                        'http_version': '1.1', 'method': 'POST', 'scheme': 'http', 'path': '/chat/stream',
                        'query_string': b'', 'headers': [(b'authorization', ('Bearer ' + token).encode()),
                                                        (b'content-type', b'application/json')],
                        'server': ('127.0.0.1', endpoint.port), 'client': ('127.0.0.1', 1)}, receive, send), 3)
                    return sent
                async def authorized_partition(tenant):
                    from services.api.app.auth.jwt import get_current_user
                    from services.api.app.evaluation.metadata_export import pseudonym
                    token = jwt.encode({'sub': 'synthetic-reader', 'tenant_id': tenant, 'exp': time.time() + 60},
                                       settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
                    user = await get_current_user(token)
                    return pseudonym(self.policy, user['tenant_id'], 'tenant', user['tenant_id'])
                yield SimpleNamespace(post=post, runtime=runtime, disconnect=disconnect,
                                      authorized_partition=authorized_partition)
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, 5)
            await runtime.close(time.monotonic() + 3)
            await llm.close()
            sock.close()


if __name__ == '__main__':
    async def worker_once():
        schema = os.environ['EVAL_ACCEPT_SCHEMA']
        import re
        assert re.fullmatch(r'eval_test_[0-9a-f]{16}', schema)
        engine = create_async_engine(os.environ['EVAL_ACCEPT_DB'], poolclass=NullPool,
                                     connect_args={'server_settings': {'search_path': schema}})
        now = datetime.fromisoformat(os.environ['EVAL_ACCEPT_CLOCK'])
        config = DeliveryConfig(enabled=True, database_url=os.environ['EVAL_ACCEPT_DB'],
                                queue_url=os.environ['EVAL_ACCEPT_QUEUE'], identity_key='test-worker-key')
        publisher = RunEvidencePublisher.from_config(config)
        try:
            repo = OutboxRepository(async_sessionmaker(engine, expire_on_commit=False), lambda: now)
            await DeliveryWorker(repo, publisher, lambda: now).run_once()
        finally:
            await publisher.close(time.monotonic() + 3)
            await engine.dispose()
    asyncio.run(worker_once())
