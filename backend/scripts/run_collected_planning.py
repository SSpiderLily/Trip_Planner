"""P4独立采集与按日修订入口；显式--live，每次只运行一个公开样本，不保存预览。"""
from __future__ import annotations
import argparse
from contextlib import redirect_stdout, redirect_stderr
import json
from pathlib import Path
from uuid import uuid4
from app.models.planning_runtime import PlanningRunIdentity
from app.models.planning_v4 import TripRequest
from app.services.collected_planning_service import run_collected_request as run_request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', required=True)
    parser.add_argument('--request-file', type=Path, required=True, help='一个公开/合成TripRequest JSON；不要传真实用户资料')
    parser.add_argument('--observation-db', type=Path, help='默认使用配置中的现有任务观测库；可显式指定研究库')
    args = parser.parse_args()
    request = TripRequest.model_validate_json(args.request_file.read_text())
    # 只有显式live才装配模型和高德；保留现有模型工厂及重试/超时策略。
    from app.agents.native_requirement_interpreter import NativeRequirementInterpreter
    from app.agents.native_multiagent_collection import NativeMultiagentCollection
    from app.agents.native_daily_plan_agent import NativeDailyPlanAgent
    from app.agents.native_day_revision_agent import NativeDayRevisionAgent
    from app.services.native_collection_adapter import NativeRequirementAdapter, NativeCollectionSession
    from app.services.planning_runtime_adapter import SynchronousPlanningRuntime
    from app.services.llm_service import get_agent_llm
    from run_planning_core import MeasuredModel
    from app.config import settings
    from app.services.observation_service import ObservationService
    from app.services.task_repository import TaskRepository
    phase = {'name': 'requirements'}

    class Measured(MeasuredModel):
        def invoke_native(self, *args, **kwargs):
            name = phase['name']
            try:
                return super().invoke_native(*args, **kwargs)
            finally:
                self.attempts[-1]['stage'] = name

    def progress(value):
        phases = {'planning_input': 'requirements', 'dispatch': 'collection',
                  'proposal': 'generation', 'validation': 'generation', 'repair': 'repair'}
        phase['name'] = phases[value.stage]

    with open('/dev/null', 'w') as quiet, redirect_stdout(quiet), redirect_stderr(quiet):
        repository = TaskRepository(args.observation_db or settings.task_db_path,
            retention_days=settings.observation_retention_days, max_bytes=settings.observation_max_bytes)
        # CLI挂接共享仓库，不执行服务启动时的进程恢复/历史清理。
        observation = ObservationService(repository, settings.observation_content_limit)
        identity = PlanningRunIdentity(task_id='p3_'+uuid4().hex, plan_id='p3_plan_'+uuid4().hex)
        try:
            repository.initialize(recover_running=False)
        except Exception as exc:
            observation.missing(identity.task_id, 'modular_repository_initialize_failed', exc)
        models = {role: Measured(get_agent_llm(role)) for role in ('master', 'attractions', 'dining', 'lodging')}
        runtime = SynchronousPlanningRuntime(progress=progress)
        record = run_request(request, identity=identity,
            requirements=NativeRequirementAdapter(interpreter=NativeRequirementInterpreter(llm=models['master'])),
            collection=NativeCollectionSession(collector=NativeMultiagentCollection(master_llm=models['master'],
                attraction_llm=models['attractions'], dining_llm=models['dining'], lodging_llm=models['lodging'])),
            generator=NativeDailyPlanAgent(models['master']), revision_generator=NativeDayRevisionAgent(models['master']),
            runtime=runtime, observation=observation)
    record['observation']['database'] = str(repository.path)
    record['models'] = {role: {'model': getattr(model, 'model', None), 'physical_requests': len(model.attempts),
        'usage': model.usage(), 'attempts': model.attempts} for role, model in models.items()}
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0 if record['delivered'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
