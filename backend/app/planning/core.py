"""固定资料 → 生成提案 → 后端接受 → 结果；没有隐式外部实现。"""
from app.models.planning_errors import PlanningError
from app.models.planning_execution import PlanningSnapshot, DraftResult
from .contracts import ProposalPort, PlanningRuntimePort
from .context import build_context
from .validation import validate_snapshot, accept_submission


def generate_draft(snapshot: PlanningSnapshot, *, generator: ProposalPort,
                   runtime: PlanningRuntimePort) -> DraftResult:
    runtime.check_cancelled()
    value = PlanningSnapshot.model_validate(snapshot.model_dump(mode='python'))
    validate_snapshot(value)

    def report(stage, completed=0, total=1, message=None):
        try:
            runtime.report(stage, completed, total, message)
        except Exception:
            # 回调可抛出与领域校验相同的异常类型；转换为运行故障，避免模型纠错吞掉它。
            runtime.check_cancelled()
            raise PlanningError('PROGRESS_CALLBACK_FAILED', '规划进度回调失败') from None

    report('proposal', message='根据固定参考资料生成每日安排')
    accepted = None
    issued = None

    def accept(submission):
        nonlocal accepted, issued
        runtime.check_cancelled()
        if accepted is not None:
            raise PlanningError('PLAN_ALREADY_ACCEPTED', '本次提案已接受')
        report('validation', message='校验每日地点与候选引用')
        result = accept_submission(value, submission)
        runtime.check_cancelled()
        report('validation', 1, message='每日地点校验完成')
        runtime.check_cancelled()
        accepted = result.model_copy(deep=True)
        issued = result
        return result

    returned = generator.generate(build_context(value), accept=accept, runtime=runtime)
    runtime.check_cancelled()
    if accepted is None or returned is not issued or returned != accepted:
        raise PlanningError('PLAN_SUBMISSION_MISSING', '生成器未提交并返回后端接受的结果')
    return accepted
