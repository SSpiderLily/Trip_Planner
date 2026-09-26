"""增量采集与脱敏；采集失败不改变业务异常。"""
import os
import re
from contextlib import contextmanager
from contextvars import ContextVar
from uuid import uuid4
from .task_repository import dumps, now

_context = ContextVar('trip_observation', default=None)
_sensitive = re.compile(r'api.?key|token|secret|password|authorization|cookie|credential', re.I)


def sanitize(value):
    if isinstance(value, dict):
        return {str(k): '[REDACTED]' if _sensitive.search(str(k)) else sanitize(v) for k,v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize(v) for v in value]
    if isinstance(value, str):
        for key, secret in os.environ.items():
            if _sensitive.search(key) and len(secret) >= 6:
                value = value.replace(secret, '[REDACTED]')
        value = re.sub(r'(?i)(bearer\s+)[^\s"\']+', r'\1[REDACTED]', value)
        value = re.sub(r'(?i)((?:api[_-]?key|token|secret|password|authorization|key)\s*[=:]\s*)[^\s,;&"\']+', r'\1[REDACTED]', value)
        return value
    return value


def bounded(value, limit):
    text = dumps(sanitize(value))
    if len(text.encode('utf-8')) <= limit:
        return text, False
    preview = text.encode('utf-8')[:max(0,limit-64)].decode('utf-8', errors='ignore')
    while len(dumps({'preview':preview}).encode('utf-8')) > limit:
        preview = preview[:len(preview)//2]
    return dumps({'preview':preview}), True


class ObservationService:
    def __init__(self, repository, content_limit=65536):
        self.repository, self.content_limit = repository, content_limit
        self.incomplete = set()

    def missing(self, task_id):
        self.incomplete.add(task_id)
        try:
            self.repository.mark_incomplete(task_id)
        except Exception:
            pass  # 当前进程查询仍能提示；不承诺故障期间可持久化。

    @contextmanager
    def task(self, task_id):
        token = _context.set((self, task_id, None, True))
        try:
            yield
        finally:
            _context.reset(token)


class Span:
    def __init__(self):
        self.output = None


@contextmanager
def span(name, kind, input_data=None):
    item = Span()
    context = _context.get()
    if context is None:
        yield item
        return
    recorder, task_id, parent, parent_saved = context
    span_id, saved = uuid4().hex, False
    try:
        if parent_saved and recorder.repository.size() <= recorder.repository.max_bytes:
            data, truncated = bounded(input_data, recorder.content_limit)
            recorder.repository.begin_span((span_id, task_id, parent, name, kind, now(), data, int(truncated)))
            saved = True
        else:
            recorder.missing(task_id)
    except Exception:
        recorder.missing(task_id)
    token = _context.set((recorder, task_id, span_id, saved))
    failure = None
    try:
        yield item
    except BaseException as exc:
        failure = exc
        if getattr(exc, 'step', None) is None:
            try:
                exc.step = name
            except Exception:
                pass
        raise
    finally:
        _context.reset(token)
        if saved:
            try:
                output, truncated = bounded(item.output, recorder.content_limit)
                # 不保存第三方异常文本，避免凭据、HTTP请求体或完整模型响应外泄。
                error = {'code':getattr(failure,'code','EXECUTION_FAILED'), 'message':'操作失败', 'type':type(failure).__name__} if failure else None
                recorder.repository.end_span(span_id, 'failed' if failure else 'succeeded', output, truncated, error)
            except Exception:
                recorder.missing(task_id)


class ObservedLLM:
    """仅代理 invoke，保留调用者实际收到的完整消息顺序。"""
    def __init__(self, llm):
        self.llm = llm

    def __getattr__(self, name):
        return getattr(self.llm, name)

    def invoke(self, messages, **kwargs):
        with span('llm.invoke', 'llm', {'messages':messages, 'model':getattr(self.llm,'model',None), 'options':kwargs}) as record:
            result = self.llm.invoke(messages, **kwargs)
            record.output = result
            return result
