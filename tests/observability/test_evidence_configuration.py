def test_build_revision_is_part_of_configuration_fingerprint(route, monkeypatch):
    from services.api.app.evaluation.configuration import configuration_fingerprint
    from services.api.app.config import settings
    assert hasattr(settings, "AGENT_BUILD_SHA"), "Missing explicit build identity"
    monkeypatch.setattr(settings, "AGENT_BUILD_SHA", "build-one")
    first = configuration_fingerprint()
    monkeypatch.setattr(settings, "AGENT_BUILD_SHA", "build-two")
    second = configuration_fingerprint()
    assert first.agent_build_sha == "build-one"
    assert first.fingerprint != second.fingerprint
