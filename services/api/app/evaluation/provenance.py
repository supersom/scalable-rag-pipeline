"""Explicit local fixture ownership; callers must select cases in trusted configuration."""
import re
from pathlib import Path
from typing import Literal

from .delivery_models import Contract, ExportPolicy, FixtureRecord

# Bounded opaque tenant identifier shared by the JWT adapter and capture sessions.
tenant_pattern = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}')


class SyntheticManifest(Contract):
    policy_version: Literal['restricted-synthetic-v1']
    sources: tuple[FixtureRecord, ...]
    calculator_grammar: Literal['numeric-sqrt-v1']

    @classmethod
    def load(cls, path: Path) -> 'SyntheticManifest':
        return cls.model_validate_json(path.read_bytes())


class SourceProof(Contract):
    source_kind: str
    source_id: str
    source_hash: str
    template_id: str | None = None
    collection: str | None = None
    document_id: str | None = None


class Provenance(Contract):
    tenant_id: str | None = None
    case_id: str | None = None
    sources: tuple[SourceProof, ...] = ()
    tool_name: str | None = None

    def approved(self, policy: ExportPolicy) -> bool:
        if (policy.environment != 'local' or policy.redaction_policy_version != 'restricted-synthetic-v1'
                or not self.tenant_id or not self.case_id or not self.sources):
            return False
        return all(any(record.tenant_id == self.tenant_id and record.case_id == self.case_id
                       and all(getattr(record, key) == value for key, value in source.model_dump().items())
                       for record in policy.synthetic_sources) for source in self.sources)
