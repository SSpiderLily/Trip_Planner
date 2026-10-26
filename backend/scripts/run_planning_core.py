"""显式选例的仅规划/评估入口；fake离线，live只创建规划模型，每例一次流程。"""
from __future__ import annotations
import argparse
from contextlib import redirect_stdout
import json
from pathlib import Path
import re
import sys
from time import monotonic

from app.models.planning_execution import PlanningSnapshot, ProposalSubmission
from app.models.planning_evaluation import QualityRatings
from app.planning.core import generate_draft
from app.planning.evaluation import evaluate_draft
from app.services.planning_runtime_adapter import SynchronousPlanningRuntime

QUALITY = Path(__file__).resolve().parents[1] / 'tests/fixtures/planning_core/quality'
NOTICE = '固定参考资料的仅规划结果；合成资料不代表真实核实。未查询交通、未保存正式预览。人工质量未知不等于通过。'

class FixedSubmissionGenerator:
    """回放固定提案，不模拟真实模型推荐质量。"""
    def __init__(self, submission):
        self.submission = submission

    def generate(self, context, *, accept, runtime):
        runtime.check_cancelled()
        return accept(self.submission)


def _usage(value):
    if value is None:
        return None
    def field(name):
        item = value.get(name) if isinstance(value, dict) else getattr(value, name, None)
        return item if type(item) is int and item >= 0 else None
    fields = {key: field(name) for key, name in (
        ('input_tokens', 'prompt_tokens'), ('output_tokens', 'completion_tokens'),
        ('total_tokens', 'total_tokens'))}
    return fields if any(v is not None for v in fields.values()) else None


class MeasuredModel:
    """在SDK重试关闭的原生调用边界计数，失败请求也保留；不存消息或推理。"""
    def __init__(self, model):
        self.model_client = model
        self.attempts = []

    def __getattr__(self, name):
        return getattr(self.model_client, name)

    def invoke_native(self, *args, **kwargs):
        started = monotonic()
        attempt = {'status': 'failed', 'elapsed_s': None, 'usage': None}
        self.attempts.append(attempt)
        try:
            result = self.model_client.invoke_native(*args, **kwargs)
            attempt.update(status='returned', usage=_usage(result.usage))
            return result
        finally:
            attempt['elapsed_s'] = round(monotonic() - started, 6)

    def usage(self):
        if not self.attempts:
            return None
        totals = {}
        for key in ('input_tokens', 'output_tokens', 'total_tokens'):
            values = [(attempt.get('usage') or {}).get(key) for attempt in self.attempts]
            totals[key] = sum(values) if all(value is not None for value in values) else None
        return totals if any(value is not None for value in totals.values()) else None


def run_case(path: Path, *, mode: str, model_factory=None):
    started = monotonic()
    measured = MeasuredModel(None)
    record = {'case_id': path.stem, 'mode': mode, 'source_type': None, 'notice': NOTICE,
              'status': 'failed', 'delivered': False, 'error_code': None, 'result': None,
              'evaluation': None, 'human_quality': QualityRatings().model_dump(mode='json')}
    try:
        if mode not in {'fake', 'live'}:
            raise ValueError('mode')
        data = json.loads(path.read_text(encoding='utf-8'))
        if data.get('source_type') not in {'synthetic', 'public_reference'}:
            raise ValueError('source_type')
        record['source_type'] = data['source_type']
        snapshot = PlanningSnapshot.model_validate(data['snapshot'])
        if mode == 'fake':
            generator = FixedSubmissionGenerator(ProposalSubmission.model_validate(data['submission']))
        else:
            # fake路径不会导入配置、模型工厂或观测数据库。
            from app.agents.native_daily_plan_agent import NativeDailyPlanAgent
            if model_factory is None:
                from app.services.llm_service import get_agent_llm
                model_factory = lambda: get_agent_llm('master')
            with redirect_stdout(sys.stderr):
                measured = MeasuredModel(model_factory())
            generator = NativeDailyPlanAgent(measured)
        with redirect_stdout(sys.stderr):
            result = generate_draft(snapshot, generator=generator, runtime=SynchronousPlanningRuntime())
        # 仅保留公开草稿/报告，不落模型协议、内部推理或原始输入事实。
        record['result'] = {key: getattr(result, key).model_dump(mode='json')
                            for key in ('identity', 'draft', 'report')}
        evaluation = evaluate_draft(snapshot, result)
        record.update(delivered=True, status=('complete' if evaluation.deterministic.required_coverage_complete else 'partial'),
                      evaluation=evaluation.model_dump(mode='json'),
                      human_quality=evaluation.human.model_dump(mode='json'))
    except Exception:
        # 不复制供应商异常/输入值到诊断字段，也不伪造空DraftResult进行评估。
        record['error_code'] = 'SOLE_PLANNING_FAILED'
    # 运行配置只取白名单，不遍历客户端属性或保存地址/密钥。
    configuration = {}
    for name in ('model', 'temperature', 'reasoning_effort'):
        value = getattr(measured.model_client, name, None)
        configuration[name] = value if type(value) in (str, int, float) else None
    record['run'] = {'model_configuration': configuration,
                     'elapsed_s': round(monotonic() - started, 6),
                     'physical_model_attempts': len(measured.attempts),
                     'attempts': measured.attempts, 'usage': measured.usage()}
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', required=True, choices=('fake', 'live'))
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument('--input-file', type=Path, help='单个固定资料JSON，兼容原入口')
    selector.add_argument('--cases', nargs='+', help='显式案例名列表，不自动扫描或追加重跑')
    parser.add_argument('--fixture-dir', type=Path, default=QUALITY)
    args = parser.parse_args(argv)
    if args.cases and (len(set(args.cases)) != len(args.cases)
                       or any(not re.fullmatch(r'[A-Za-z0-9_-]+', name) for name in args.cases)):
        parser.error('案例名必须唯一，且只能包含字母、数字、下划线和连字符')
    paths = [args.input_file] if args.input_file else [args.fixture_dir / (name + '.json') for name in args.cases]
    records = [run_case(path, mode=args.mode) for path in paths]
    payload = records[0] if args.input_file else {'mode': args.mode, 'notice': NOTICE, 'runs': records}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if all(record['delivered'] for record in records) else 1


if __name__ == '__main__':
    raise SystemExit(main())
