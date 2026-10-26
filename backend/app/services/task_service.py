"""应用持有的单活动任务：HTTP 断连不会取消规划。"""
import asyncio
from datetime import datetime, timezone
from uuid import uuid4
from starlette.concurrency import run_in_threadpool
from .observation_service import span, sanitize
from .metrics_service import build_metrics


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
        self.completed_observation_failures = {}  # 已就绪预览的观测完成状态仅保留有界摘要。
        self.preview_completion_resolver = None
        self.preview_completion_batch_resolver = None

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

    async def submit_managed(self, request, *, prepare, executor):
        """接受由独立仓库管理结果的异步任务，不写入 task_results。"""
        return await asyncio.shield(self.hold(self._accept_managed(request, prepare, executor)))

    def release_active(self, task_id):
        """允许首屏预览就绪后接受新规划，即使该任务的后台补齐仍在运行。"""
        if self.active_id == task_id:
            self.active_id = None

    async def _accept_managed(self, request, prepare, executor):
        async with self.lock:
            if not self.accepting:
                raise TaskError('SERVICE_STOPPING', '服务暂不接收任务', 503)
            if self.active_id is not None:
                raise TaskError('TASK_BUSY', '已有规划正在执行')
            task_id = 'trip_' + uuid4().hex
            self.active_id = task_id
            try:
                await asyncio.to_thread(self.repository.cleanup)
                await asyncio.to_thread(self.repository.create, task_id, sanitize(request.model_dump()))
                await prepare(task_id)
                self.hold(self._execute_managed(task_id, executor))
            except Exception as exc:
                try:
                    await asyncio.to_thread(self.repository.fail, task_id, 'TASK_START_FAILED', '任务启动失败')
                except Exception:
                    pass
                self.active_id = None
                raise TaskError('TASK_REGISTRATION_FAILED', '任务登记失败，未启动规划', 503) from exc
            return {'task_id': task_id, 'status': 'accepted'}

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
            self.release_active(task_id)
            if task_id not in self.save_failures:
                self.observation.incomplete.discard(task_id)

    async def _record_observation_completion_failure(self, task_id):
        """预览已提交；观测状态写入失败只标记观测不完整，不回滚规划成功。"""
        record = {
            'finished_at': datetime.now(timezone.utc).isoformat(timespec='microseconds'),
            'persistence_error': {
                'code': 'OBSERVATION_COMPLETION_INCOMPLETE',
                'message': '预览已持久化，但任务观测状态未能同步',
            },
        }
        self.completed_observation_failures[task_id] = record
        while len(self.completed_observation_failures) > 100:
            expired = next(iter(self.completed_observation_failures))
            self.completed_observation_failures.pop(expired)
            self.observation.incomplete.discard(expired)
        self.observation.incomplete.add(task_id)
        try:
            await asyncio.to_thread(self.repository.mark_incomplete, task_id)
        except Exception:
            pass

    @staticmethod
    def _can_recover_as_preview_success(record):
        return (
            record['status'] in ('accepted', 'running', 'interrupted')
            or (record['status'] == 'failed' and record.get('error_code') == 'RESULT_SAVE_FAILED')
        )

    @staticmethod
    def _apply_preview_success_evidence(record, evidence):
        persisted_status = record.get('status')
        persisted_error_code = record.get('error_code')
        record['persisted_status'] = persisted_status
        record['persisted_error_code'] = persisted_error_code
        record['status'] = 'succeeded'
        record['finished_at'] = evidence['finished_at']
        record['current_step'] = None
        record['observation_incomplete'] = True
        record['persistence_error'] = evidence['persistence_error']
        record['error_code'] = None
        record['error_message'] = None
        record['error_step'] = None
        return record

    @staticmethod
    def _preview_completion_evidence(ready):
        return {
            # 老记录的 generation_finished_at 可能为空；不把后台进度时间冒充首屏完成时间。
            'finished_at': ready.get('generation_finished_at'),
            'persistence_error': {
                'code': 'OBSERVATION_COMPLETION_INCOMPLETE',
                'message': '预览已持久化，但任务观测状态未能同步',
            },
        }

    async def _execute_managed(self, task_id, executor):
        try:
            try:
                await asyncio.to_thread(self.repository.start, task_id)
            except Exception:
                # v4 job/token 已在 PreviewRepository 持久化；观测库状态写入失败
                # 不能让已接受的预览任务停在 accepted。继续执行并标记观测不完整。
                self.observation.missing(task_id)
            await executor(task_id)
            try:
                await asyncio.to_thread(
                    self.repository.mark_preview_succeeded,
                    task_id,
                    task_id in self.observation.incomplete,
                )
            except Exception:
                await self._record_observation_completion_failure(task_id)
        except asyncio.CancelledError:
            await self._fail(task_id, 'PROCESS_INTERRUPTED', '任务未完成')
            raise
        except Exception as exc:
            await self._fail(task_id, getattr(exc, 'code', 'PLANNING_FAILED'), '规划失败', getattr(exc, 'step', None))
        finally:
            self.release_active(task_id)
            if task_id not in self.save_failures and task_id not in self.completed_observation_failures:
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
        completion = self.completed_observation_failures.get(task_id)
        try:
            record = await asyncio.to_thread(self.repository.status, task_id)
        except Exception as exc:
            if completion:
                return self._apply_preview_success_evidence({
                    'task_id': task_id, 'status': None, 'created_at': None,
                    'finished_at': None, 'current_step': None,
                    'error_code': None, 'error_message': None, 'error_step': None,
                }, completion) | {'persisted_status': None, 'persisted_error_code': None}
            if failure:
                return {'task_id':task_id, 'status':'failed', 'persisted_status':None,
                        'current_step':None, 'observation_incomplete':True, 'persistence_error':failure,
                        'error_code':failure['code'], 'error_message':failure['message']}
            raise TaskError('STORAGE_UNAVAILABLE', '存储暂不可用，无法确认任务状态', 503) from exc
        if record is None:
            raise TaskError('TASK_NOT_FOUND', '任务不存在或已被清理', 404)
        record['observation_incomplete'] = bool(record['observation_incomplete'] or task_id in self.observation.incomplete)
        record['persistence_error'] = failure
        real_failure = failure is not None and failure.get('code') != 'RESULT_SAVE_FAILED'
        if completion is None and not real_failure and self._can_recover_as_preview_success(record):
            resolver = self.preview_completion_resolver
            if resolver is not None:
                try:
                    ready = await asyncio.to_thread(resolver, task_id)
                except Exception as exc:
                    raise TaskError('STORAGE_UNAVAILABLE', '无法核实行程预览状态', 503) from exc
                if ready is not None:
                    completion = self._preview_completion_evidence(ready)
        if completion is not None and not real_failure and self._can_recover_as_preview_success(record):
            return self._apply_preview_success_evidence(record, completion)
        if failure:
            record['persisted_status'] = record['status']
            record['status'] = 'failed'
            record['current_step'] = None
            record['error_code'] = failure['code']
            record['error_message'] = failure['message']
            record['error_step'] = failure.get('step')
        return record

    async def list_tasks(self, status=None, limit=50, offset=0):
        # 对有效状态筛选不能直接让 SQLite 按原始观测状态分页：ready 预览可能
        # 对应 running/interrupted，必须先用批量摘要修正状态，再筛选和分页。
        page_size = max(100, min(max(limit, 1), 500))
        scan_offset = offset if status is None else 0
        matched = 0
        result = []
        while len(result) < limit:
            try:
                rows = await asyncio.to_thread(
                    self.repository.list_tasks, None, page_size, scan_offset, tuple(self.save_failures)
                )
            except Exception as exc:
                raise TaskError('STORAGE_UNAVAILABLE', '存储暂不可用，无法查询任务列表', 503) from exc
            if not rows:
                break

            ready_by_task = {}
            batch_resolver = self.preview_completion_batch_resolver
            if batch_resolver is not None:
                try:
                    ready_by_task = await asyncio.to_thread(
                        batch_resolver, tuple(row['task_id'] for row in rows)
                    )
                except Exception as exc:
                    raise TaskError('STORAGE_UNAVAILABLE', '无法核实行程预览状态', 503) from exc

            for row in rows:
                task_id = row['task_id']
                row['observation_incomplete'] = bool(
                    row['observation_incomplete'] or task_id in self.observation.incomplete
                )
                completion = self.completed_observation_failures.get(task_id)
                if completion is None and task_id in ready_by_task:
                    completion = self._preview_completion_evidence(ready_by_task[task_id])
                failure = self.save_failures.get(task_id)
                # 保存了真正的规划错误时，ready 证据不能把它改成成功。只有
                # 结果写入失败属于可由持久化预览恢复的观测状态。
                real_failure = failure is not None and failure.get('code') != 'RESULT_SAVE_FAILED'
                if completion is not None and not real_failure and self._can_recover_as_preview_success(row):
                    self._apply_preview_success_evidence(row, completion)
                else:
                    row['persistence_error'] = failure
                    if failure:
                        row['persisted_status'] = row['status']
                        row['status'] = 'failed'
                        row['current_step'] = None
                        row['error_code'] = failure['code']
                        row['error_message'] = failure['message']
                        row['error_step'] = failure.get('step')
                if status is not None and row['status'] != status:
                    continue
                if matched >= (0 if status is None else offset):
                    result.append(row)
                    if len(result) >= limit:
                        break
                matched += 1

            if len(result) >= limit or len(rows) < page_size:
                break
            scan_offset += len(rows)
        return result

    async def result(self, task_id):
        record = await self.status(task_id)
        if record['status'] != 'succeeded':
            code = {'failed':'TASK_FAILED', 'interrupted':'TASK_INTERRUPTED'}.get(record['status'], 'RESULT_NOT_READY')
            error = TaskError(code, record['error_message'] or '任务未成功完成')
            error.details = {'task_status': record['status'], 'error_code': record['error_code']}
            raise error
        result = await asyncio.to_thread(self.repository.result, task_id)
        if result is None:
            if task_id in self.completed_observation_failures:
                raise TaskError(
                    'PREVIEW_RESULT_USE_PREVIEW_API',
                    f'此 v4 任务使用预览存储，请通过 /api/trip/v4/tasks/{task_id}/preview 读取',
                    409,
                )
            resolver = self.preview_completion_resolver
            if resolver is not None:
                try:
                    ready = await asyncio.to_thread(resolver, task_id)
                except Exception as exc:
                    raise TaskError('STORAGE_UNAVAILABLE', '无法核实行程预览状态', 503) from exc
                if ready is not None:
                    raise TaskError(
                        'PREVIEW_RESULT_USE_PREVIEW_API',
                        f'此 v4 任务使用预览存储，请通过 /api/trip/v4/tasks/{task_id}/preview 读取',
                        409,
                    )
            raise TaskError('RESULT_UNAVAILABLE', '结果不可用', 503)
        return result

    async def metrics(self, task_id):
        task = await self.status(task_id)
        if task['status'] in ('accepted', 'running'):
            raise TaskError('METRICS_NOT_READY', '任务结束后可查看工程指标')
        try:
            spans = await asyncio.to_thread(self.repository.metric_spans, task_id)
        except Exception as exc:
            raise TaskError('METRICS_UNAVAILABLE', '工程指标暂不可用', 503) from exc
        if spans is None:
            raise TaskError('TASK_NOT_FOUND', '任务不存在或已被清理', 404)
        return build_metrics(task, spans)

    async def close(self):
        self.accepting = False
        while self.background:
            pending = tuple(self.background)
            await asyncio.gather(*pending, return_exceptions=True)
            # gather已确认这批任务完成；不等待可能尚未调度的移除回调。
            # 等待期间新加入的任务不在pending内，下一轮继续等待。
            self.background.difference_update(pending)
