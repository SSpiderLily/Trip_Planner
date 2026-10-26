"""TravelPlanner 式仅规划：给定参考资料，用一次独立工具会话提交行程。"""
from threading import Event
from time import monotonic
from pydantic import Field, ValidationError
from app.models.planning_execution import ProposalSubmission
from app.models.planning_v4 import ProposalDay
from app.models.planning_errors import PlanningError, InvalidPlanningReferences
from app.services.planning_v4_validator import ContractValidationError
from app.services.native_tool_loop import NativeTool, NativeToolArguments, NativeToolLoop, NativeToolLoopError
import json

class SubmitDailyPlanArguments(NativeToolArguments):
    days: list[ProposalDay] = Field(min_length=1)
    missing_notes: list[str] = Field(default_factory=list, max_length=40)

class _Cancellation:
    """把窄运行端口转换为工具循环需要的协作取消信号。"""
    def __init__(self, check_cancelled):
        self.check_cancelled = check_cancelled
        self.sleeper = Event()

    def is_set(self):
        self.check_cancelled()
        return False

    def wait(self, timeout):
        end = monotonic() + timeout
        while True:
            self.check_cancelled()
            remaining = end - monotonic()
            if remaining <= 0:
                return False
            self.sleeper.wait(min(remaining, 0.05))

class NativeDailyPlanAgent:
    def __init__(self, model, *, max_model_calls=5, max_tool_calls=4, emit=None):
        from app.services.native_observation import native_event_emitter
        self.emit = native_event_emitter(emit)
        self.model = model
        self.max_model_calls, self.max_tool_calls = max_model_calls, max_tool_calls

    def generate(self, context, *, accept, runtime):
        accepted = None
        failure = None

        def check_running():
            runtime.check_cancelled()
            if failure is not None:
                raise failure

        def submit(args):
            nonlocal failure
            try:
                return handle_submission(args)
            except Exception as exc:
                # 工具循环会将 handler 异常转为工具错误；运行故障必须停止，不能让模型重试业务。
                failure = exc
                raise

        def rejected(codes):
            self.emit('planning.submission_rejected', {'codes': codes})
            return {'status': 'rejected', 'codes': codes}

        def handle_submission(args):
            nonlocal accepted
            runtime.check_cancelled()
            if accepted is not None:
                return {'status': 'already_submitted'}
            try:
                submission = ProposalSubmission.model_validate(args.model_dump(mode='python'))
                result = accept(submission)
            except ContractValidationError as exc:
                return rejected(sorted({i.code for i in exc.issues}))
            except InvalidPlanningReferences as exc:
                return rejected(list(exc.codes))
            except ValidationError:
                return rejected(['PLAN_SUBMISSION_INVALID'])
            except PlanningError as exc:
                if exc.code != 'PLAN_REFERENCE_INVALID':
                    raise
                return rejected(list(exc.reference_issue_codes))
            runtime.check_cancelled()
            accepted = result
            return {'status': 'accepted', 'revision': result.draft.revision,
                    'instruction': '本次仅规划已结束；缺项保留，不补查、不重提。'}
        messages = [
            {'role':'system', 'content':context.system_instruction + '\n本阶段覆盖普通文本JSON说明：必须调用submit_daily_plan提交days和missing_notes；拒绝后按codes纠正，接受后结束。按原需求与已有用途证据选择候选；证据未知时保留未知。休息可作为free_time建议，不能替代必需地点；不承诺返程可行性。'},
            {'role':'user', 'content':json.dumps(context.reference_information, ensure_ascii=False)},
        ]
        self.emit('planning.phase', {'phase': 'initial', 'candidate_count': len(context.reference_information.get('candidates', ()))})
        loop = NativeToolLoop(self.model, [NativeTool('submit_daily_plan',
            '提交每日安排，仅选择给定候选；保留缺项，不提供地点事实、价格、时间或版本。',
            SubmitDailyPlanArguments, submit)], max_model_calls=self.max_model_calls, max_tool_calls=self.max_tool_calls,
            accepted_submission_tools=frozenset({'submit_daily_plan'}),
            observation_role='master', observation_stage='daily_plan')
        protocol = loop.run(messages, cancel_event=_Cancellation(check_running))
        check_running()
        if accepted is None or protocol.termination_reason != 'submission_accepted':
            raise NativeToolLoopError('PLAN_SUBMISSION_MISSING')
        return accepted
