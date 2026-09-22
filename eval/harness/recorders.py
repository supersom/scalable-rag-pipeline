import inspect


class RecorderError(RuntimeError):
    pass


def normalize_payload(payload):
    payload = payload or {}
    meta = payload.get("metadata") or {}
    filename = payload.get("filename") or meta.get("filename") or "unknown"
    page = payload.get("page")
    if page is None:
        page = meta.get("page_number")
    return filename, page, payload.get("text", "")


class _Recorder:
    _last = None

    def reset(self):
        self._last = None

    def last(self):
        return self._last

    def __enter__(self):
        self.install()
        return self

    def __exit__(self, *exc):
        self.uninstall()
        return False


class PlannerRecorder(_Recorder):
    def __init__(self, module):
        if not callable(getattr(module, "_extract_json", None)):
            raise RecorderError("planner module has no callable _extract_json; the harness needs it to capture raw plans")
        self._module = module
        self._original = None

    def install(self):
        original = self._module._extract_json
        recorder = self

        def wrapped(text):
            try:
                plan = original(text)
            except Exception as e:
                recorder._last = {"_error": str(e)}
                raise
            recorder._last = dict(plan)
            return plan

        self._original = original
        self._module._extract_json = wrapped

    def uninstall(self):
        if self._original is not None:
            self._module._extract_json = self._original
            self._original = None


class RetrievalRecorder(_Recorder):
    def __init__(self, client):
        if not inspect.iscoroutinefunction(getattr(client, "search", None)):
            raise RecorderError("client.search must be an async method; the harness needs it to record ranked hits")
        self._client = client
        self._original = None

    def install(self):
        original = self._client.search
        recorder = self

        async def wrapped(*args, **kwargs):
            results = await original(*args, **kwargs)
            hits = []
            for r in results:
                filename, page, text = normalize_payload(r.payload)
                hits.append({"filename": filename, "page": page, "score": r.score, "text": text})
            recorder._last = hits
            return results

        self._original = original
        self._client.search = wrapped

    def uninstall(self):
        if self._original is not None:
            self._client.search = self._original
            self._original = None
