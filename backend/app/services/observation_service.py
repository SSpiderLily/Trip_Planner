"""增量采集与脱敏；采集失败不改变业务异常。"""
import os
import re
from contextlib import contextmanager
from contextvars import ContextVar
from uuid import uuid4
from .task_repository import dumps, now

_active_model_span = ContextVar('active_model_span', default=None)
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
        value = re.sub(r'(?i)((?:api[ _-]?key|token|secret|password|authorization|key)[\"\']?\s*[=:]\s*[\"\']?)[^\s,;&"\']+', r'\1[REDACTED]', value)
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

    def missing(self, task_id, reason="task_state_write_failed", error=None):
        # 只输出分类、异常类型和数值错误码；异常原文可能携带凭据或响应内容。
        exception_type = type(error).__name__ if error is not None else "none"
        sqlite_code = getattr(error, "sqlite_errorcode", None)
        if type(sqlite_code) is not int:
            sqlite_code = "unknown"
        try:
            print(f"[observation] task={task_id} reason={reason} exception={exception_type} sqlite_code={sqlite_code}", flush=True)
        except Exception:
            pass  # 控制台不可写也不能打断规划。
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
        self.usage = None
        self.model_diagnostics = None
        self.model_configuration = None


def token_usage(value):
    """仅接受供应商给出的非负整数，缺失字段保留未知。"""
    if value is None:
        return None
    def field(name):
        item = value.get(name) if isinstance(value, dict) else getattr(value, name, None)
        return item if type(item) is int and item >= 0 else None
    usage = {'input_tokens': field('prompt_tokens'), 'output_tokens': field('completion_tokens'),
             'total_tokens': field('total_tokens')}
    return usage if any(item is not None for item in usage.values()) else None


def record_model_diagnostics(value):
    item = _active_model_span.get()
    if item is not None:
        item.model_diagnostics = value


def record_model_configuration(value):
    """只由SDK请求边界传入已解析的白名单配置，不保存客户端或凭据。"""
    item = _active_model_span.get()
    if item is not None:
        item.model_configuration = value


def mark_observation_missing(reason, error=None):
    context = _context.get()
    if context is not None:
        recorder, task_id, _, _ = context
        recorder.missing(task_id, reason, error)


@contextmanager
def span(name, kind, input_data=None):
    # 即使未挂观测仓库也恢复上下文，避免跨任务/线程串线。
    with _span_impl(name, kind, input_data) as item:
        token = _active_model_span.set(item if kind == "llm" else None)
        try:
            yield item
        finally:
            _active_model_span.reset(token)
            if item.model_diagnostics is not None:
                output = item.output if isinstance(item.output, dict) else {"result": item.output}
                item.output = {**output, "model_call": item.model_diagnostics}
            if item.model_configuration is not None:
                output = item.output if isinstance(item.output, dict) else {"result": item.output}
                item.output = {**output, "model_configuration": item.model_configuration}


@contextmanager
def _span_impl(name, kind, input_data=None):
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
            recorder.missing(task_id, "parent_not_saved" if not parent_saved else "capacity_exceeded")
    except Exception as exc:
        recorder.missing(task_id, "span_begin_failed", exc)
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
                recorder.repository.end_span(span_id, 'failed' if failure else 'succeeded', output, truncated, error, item.usage)
            except Exception as exc:
                recorder.missing(task_id, "span_end_failed", exc)


class ObservedLLM:
    """仅代理 invoke，保留调用者实际收到的完整消息顺序。"""
    def __init__(self, llm):
        self.llm = llm

    def __getattr__(self, name):
        return getattr(self.llm, name)

    def invoke(self, messages, **kwargs):
        with span('llm.invoke', 'llm', {'messages':messages, 'model':getattr(self.llm,'model',None), 'options':kwargs}) as record:
            if callable(getattr(type(self.llm), 'invoke_with_usage', None)):
                result, usage = self.llm.invoke_with_usage(messages, **kwargs)
                record.usage = token_usage(usage)
            else:
                result = self.llm.invoke(messages, **kwargs)
            record.output = result
            return result
