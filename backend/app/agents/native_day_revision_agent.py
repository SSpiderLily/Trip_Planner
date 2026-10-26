"""单日修订独立工具会话：接受后立即结束，不消费旧协议历史。"""
import json
from pydantic import ValidationError
from app.models.planning_execution import DayRevisionSubmission
from app.models.planning_errors import PlanningError, InvalidPlanningReferences
from app.models.planning_v4 import ProposalDay
from app.services.planning_v4_validator import ContractValidationError
from app.services.native_tool_loop import NativeTool, NativeToolArguments, NativeToolLoop, NativeToolLoopError
from .native_daily_plan_agent import _Cancellation


class SubmitDayRevisionArguments(NativeToolArguments):
    replacement_day: ProposalDay


class NativeDayRevisionAgent:
    def __init__(self, model, *, max_model_calls=5, max_tool_calls=4, emit=None):
        from app.services.native_observation import native_event_emitter
        self.emit = native_event_emitter(emit)
        self.model = model
        self.max_model_calls, self.max_tool_calls = max_model_calls, max_tool_calls

    def generate(self, context, *, accept, runtime):
        accepted, failure = None, None

        def check():
            runtime.check_cancelled()
            if failure is not None:
                raise failure

        target = context.reference_information['target_date_only']['day_id']
        def rejected(codes):
            self.emit('repair.submission_rejected', {'day_id': target, 'codes': codes})
            return {'status': 'rejected', 'codes': codes}

        def submit(args):
            nonlocal accepted, failure
            try:
                check()
                if accepted is not None:
                    return {'status': 'already_submitted'}
                try:
                    result = accept(DayRevisionSubmission.model_validate(args.model_dump(mode='python')))
                except ContractValidationError as exc:
                    return rejected(sorted({i.code for i in exc.issues}))
                except InvalidPlanningReferences as exc:
                    return rejected(list(exc.codes))
                except (ValidationError, ValueError):
                    return rejected(['REVISION_REFERENCE_INVALID'])
                check()
                accepted = result
                return {'status': 'accepted', 'day_id': result.day_id, 'revision': result.revision}
            except Exception as exc:
                failure = exc
                raise

        messages = [
            {'role': 'system', 'content': context.system_instruction + '\n必须调用submit_day_revision提交replacement_day；拒绝后纠正，后端接受后结束。版本和问题ID由后端绑定。'},
            {'role': 'user', 'content': json.dumps(context.reference_information, ensure_ascii=False)},
        ]
        self.emit('planning.phase', {'phase': 'revision', 'day_id': target,
            'round': context.reference_information['repair_round']['round_number']})
        protocol = NativeToolLoop(self.model, [NativeTool('submit_day_revision',
            '仅提交目标日，保留已有有效活动ID、内容和相对顺序。', SubmitDayRevisionArguments, submit)],
            max_model_calls=self.max_model_calls, max_tool_calls=self.max_tool_calls,
            accepted_submission_tools=frozenset({'submit_day_revision'}),
            observation_role='master', observation_stage='revision').run(messages, cancel_event=_Cancellation(check))
        check()
        if accepted is None or protocol.termination_reason != 'submission_accepted':
            raise NativeToolLoopError('MASTER_DID_NOT_SUBMIT_REVISION')
        return accepted
