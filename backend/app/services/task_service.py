"""应用持有的单活动任务：HTTP 断连不会取消规划。"""
import asyncio
from uuid import uuid4
from starlette.concurrency import run_in_threadpool
from .observation_service import span, sanitize


class TaskError(RuntimeError):
    def __init__(self, code, message, status=409):
        super().__init__(message)
        self.code, self.status = code, status
        self.details = {}


class TaskService:
    def __init__(self, repository, observation, planner_factory):
        self.repository, self.observation, self.planner_factory = repository, observation, planner_factory
        self.lock = asyncio.Lock()
        self.active_id = None
        self.background = set()
        self.accepting = True
        self.save_failures = {}  # 只保存错误摘要，绝不作为备用结果存储。

    def hold(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.background.add(task)
        def done(future):
            self.background.discard(future)
            if not future.cancelled():
                future.exception()  # 客户端断连后也取走异常。
        task.add_done_callback(done)
        return task

    async def submit(self, request):
        return await asyncio.shield(self.hold(self._accept(request)))

    async def _accept(self, request):
        async with self.lock:
            if not self.accepting:
                raise TaskError('SERVICE_STOPPING', '服务暂不接收任务', 503)
            if self.active_id is not None:
                raise TaskError('TASK_BUSY', '已有规划正在执行')
            task_id = 'trip_' + uuid4().hex
            self.active_id = task_id
            try:
                await asyncio.to_thread(self.repository.cleanup)
                # 输入需求也需脱敏；执行使用原始已校验对象，不读取脱敏快照。
                await asyncio.to_thread(self.repository.create, task_id, sanitize(request.model_dump()))
                self.hold(self._execute(task_id, request))
            except Exception as exc:
                try:
                    await asyncio.to_thread(self.repository.fail, task_id, 'TASK_START_FAILED', '任务启动失败')
                except Exception:
                    pass
                self.active_id = None
                raise TaskError('TASK_REGISTRATION_FAILED', '任务登记失败，未启动规划', 503) from exc
            return {'task_id':task_id, 'status':'accepted'}

    def _plan(self, task_id, request):
        with self.observation.task(task_id):
            with span('planning', 'planning', request.model_dump()) as record:
                with span('planning.initialize', 'planning'):
                    planner = self.planner_factory()
                result = planner.plan_trip(request)
                record.output = {'completed':True}
                return result.model_dump()

    async def _execute(self, task_id, request):
        try:
            await asyncio.to_thread(self.repository.start, task_id)
            result = await run_in_threadpool(self._plan, task_id, request)
            try:
                await asyncio.to_thread(self.repository.succeed, task_id, sanitize(result), task_id in self.observation.incomplete)
            except Exception:
                await self._fail(task_id, 'RESULT_SAVE_FAILED', '结果保存失败')
        except Exception as exc:
            code = getattr(exc, 'code', 'PLANNING_FAILED')
            messages = {'INSUFFICIENT_INFORMATION':'必要查询没有有效结果',
                        'REQUIRED_TOOL_NOT_CALLED':'模型未实际调用必要工具',
                        'TOOL_FAILED':'工具调用失败', 'TOOL_RESPONSE_INVALID':'工具返回内容无法验证',
                        'INVALID_ITINERARY':'行程日期与需求不一致'}
            await self._fail(task_id, code, messages.get(code, '规划失败，请查看执行记录'), getattr(exc,'step',None))
        finally:
            self.active_id = None
            if task_id not in self.save_failures:
                self.observation.incomplete.discard(task_id)

    async def _fail(self, task_id, code, message, step=None):
        try:
            await asyncio.to_thread(self.repository.fail, task_id, code, message, step, task_id in self.observation.incomplete)
        except Exception:
            self.save_failures[task_id] = {'code':code, 'message':message, 'step':step}
            # 错误摘要有界；完整结果不驻留。
            while len(self.save_failures) > 100:
                expired = next(iter(self.save_failures))
                self.save_failures.pop(expired)
                self.observation.incomplete.discard(expired)

    async def status(self, task_id):
        failure = self.save_failures.get(task_id)
        try:
            record = await asyncio.to_thread(self.repository.status, task_id)
        except Exception as exc:
            if failure:
                return {'task_id':task_id, 'status':'failed', 'persisted_status':None,
                        'current_step':None, 'observation_incomplete':True, 'persistence_error':failure,
                        'error_code':failure['code'], 'error_message':failure['message']}
            raise TaskError('STORAGE_UNAVAILABLE', '存储暂不可用，无法确认任务状态', 503) from exc
        if record is None:
            raise TaskError('TASK_NOT_FOUND', '任务不存在或已被清理', 404)
        record['observation_incomplete'] = bool(record['observation_incomplete'] or task_id in self.observation.incomplete)
        record['persistence_error'] = failure
        if failure:
            record['persisted_status'] = record['status']
            record['status'] = 'failed'
            record['current_step'] = None
        return record

    async def list_tasks(self, status=None, limit=50, offset=0):
        try:
            rows = await asyncio.to_thread(self.repository.list_tasks, status, limit, offset, tuple(self.save_failures))
        except Exception as exc:
            raise TaskError('STORAGE_UNAVAILABLE', '存储暂不可用，无法查询任务列表', 503) from exc
        for row in rows:
            row['observation_incomplete'] = bool(row['observation_incomplete'] or row['task_id'] in self.observation.incomplete)
            row['persistence_error'] = self.save_failures.get(row['task_id'])
            if row['persistence_error']:
                row['persisted_status'] = row['status']
                row['status'] = 'failed'
        return rows

    async def result(self, task_id):
        record = await self.status(task_id)
        if record['status'] != 'succeeded':
            code = {'failed':'TASK_FAILED', 'interrupted':'TASK_INTERRUPTED'}.get(record['status'], 'RESULT_NOT_READY')
            error = TaskError(code, record['error_message'] or '任务未成功完成')
            error.details = {'task_status': record['status'], 'error_code': record['error_code']}
            raise error
        result = await asyncio.to_thread(self.repository.result, task_id)
        if result is None:
            raise TaskError('RESULT_UNAVAILABLE', '结果不可用', 503)
        return result

    async def close(self):
        self.accepting = False
        while self.background:
            await asyncio.gather(*list(self.background), return_exceptions=True)
