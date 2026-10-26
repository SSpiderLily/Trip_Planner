"""独立采集规划的装配与观测；核心只接收原有生成/运行端口。"""
from contextlib import nullcontext
from time import monotonic
import sqlite3

from app.models.planning_errors import PlanningError
from app.planning.core import generate_draft
from app.planning.evaluation import evaluate_draft
from .native_observation import native_run_scope
from .observation_service import span, sanitize


def _draft_summary(result):
    return {'draft_revision': result.draft.revision, 'day_count': len(result.draft.days),
            'issue_codes': sorted({issue.code for issue in result.report.issues}),
            'repair_required': any(issue.triggers_repair for issue in result.report.issues)}


class _ObservedProposalPort:
    def __init__(self, generator):
        self.generator = generator

    def generate(self, context, *, accept, runtime):
        def observed_accept(submission):
            with span('planning.modular.validation', 'validation',
                      {'submitted_day_count': len(submission.days)}) as record:
                result = accept(submission)
                record.output = {'outcome': 'accepted', **_draft_summary(result)}
                return result
        return self.generator.generate(context, accept=observed_accept, runtime=runtime)


def run_collected_request(request, *, identity, requirements, collection, generator, runtime, observation=None, revision_generator=None):
    """显式注入仓库；无观测参数仅用于已有独立替身装配，不隐式创建客户端/配置。"""
    record = {'identity': identity.model_dump(mode='json'), 'status': 'technical_error',
              'delivered': False, 'error_code': None, 'error_step': None,
              'planning_input': None, 'snapshot': None, 'result': None, 'evaluation': None,
              'failure_details': [], 'stage_elapsed_s': {},
              'notice': ('独立P4结果；未执行交通查询或预览保存。人工质量未验收。' if revision_generator is not None
                         else '独立P3结果；未执行按日修订、交通查询或预览保存。人工质量未验收。')}
    task_id = identity.task_id
    registered = False
    if observation is not None:
        try:
            observation.repository.create(task_id, sanitize({'kind': 'collected_planning',
                'identity': record['identity'], 'request': request.model_dump(mode='json')}))
            registered = True
            observation.repository.start(task_id)
        except sqlite3.IntegrityError:
            # 标识冲突时不能将新执行拼入旧任务，也不能更改旧任务状态。
            raise PlanningError('OBSERVATION_IDENTITY_CONFLICT', '观测任务标识已存在') from None
        except Exception as exc:
            observation.missing(task_id, 'modular_task_start_failed', exc)
    stage = 'requirements'
    started = monotonic()
    try:
        with (observation.task(task_id) if observation is not None else nullcontext()), native_run_scope(identity):
            with span('planning.modular', 'planning', {'identity': record['identity'], 'mode': 'collected_planning'}) as root:
                with span('planning.modular.requirements', 'agent', {'request_id': request.request_id}) as step:
                    p = requirements.run(request, runtime=runtime, cancel_event=runtime.cancel_event)
                    record['planning_input'] = p.model_dump(mode='json')
                    step.output = {'outcome': 'prepared', 'request_id': p.request.request_id,
                        'requirements_id': p.requirements.requirements_id, 'day_count': len(p.days)}
                record['stage_elapsed_s'][stage] = round(monotonic() - started, 6)
                stage, started = 'collection', monotonic()
                with span('planning.modular.collection', 'agent', {'request_id': p.request.request_id}) as step:
                    snapshot = collection.collect(p, identity=identity, runtime=runtime, cancel_event=runtime.cancel_event)
                    record['snapshot'] = snapshot.model_dump(mode='json')
                    step.output = {'outcome': 'normalized', 'schema_version': snapshot.schema_version,
                        'candidate_count': len(snapshot.candidates), 'fact_count': len(snapshot.facts.places),
                        'task_statuses': [{'task_id': t.task_id, 'status': t.status, 'stop_reason': t.stop_reason}
                            for t in snapshot.task_results]}
                record['stage_elapsed_s'][stage] = round(monotonic() - started, 6)
                stage, started = 'generation', monotonic()
                with span('planning.modular.generation', 'agent', {'snapshot_version': snapshot.schema_version}) as step:
                    result = generate_draft(snapshot, generator=_ObservedProposalPort(generator), runtime=runtime)
                    record['result'] = result.model_dump(mode='json')
                    step.output = {'outcome': 'accepted', **_draft_summary(result)}
                record['stage_elapsed_s'][stage] = round(monotonic() - started, 6)
                if revision_generator is not None:
                    import asyncio
                    from app.services.planning_repair_service import repair_draft
                    from app.models.planning_execution import DraftResult, PlanningSnapshot
                    stage, started = 'repair', monotonic()
                    record['initial_result'] = record['result']
                    with span('planning.modular.repair', 'agent') as step:
                        repaired = asyncio.run(repair_draft(snapshot, result.draft, result.report,
                            revision_generator=revision_generator, collection=collection, runtime=runtime))
                        record['repair'] = repaired.model_dump(mode='json')
                        step.output = {'status': repaired.status, 'attempt_count': len(repaired.attempts),
                                       'revision': repaired.draft.revision}
                        runtime.check_cancelled()
                        if repaired.status == 'technical_error':
                            raise PlanningError(repaired.error_code or 'REPAIR_FAILED', '按日修订技术故障')
                        snapshot = PlanningSnapshot.model_validate({**snapshot.model_dump(mode='python'),
                            'facts': repaired.facts, 'candidates': repaired.candidates,
                            'area_hints': repaired.area_hints, 'task_results': repaired.task_results,
                            'task_notes': collection.task_notes})
                        result = DraftResult(identity=snapshot.identity, planning_input=snapshot.planning_input,
                            draft=repaired.draft, report=repaired.report, facts=repaired.facts,
                            candidates=repaired.candidates, area_hints=repaired.area_hints)
                        record['snapshot'] = snapshot.model_dump(mode='json')
                        record['result'] = result.model_dump(mode='json')
                    record['stage_elapsed_s'][stage] = round(monotonic() - started, 6)
                    record['notice'] = '独立P4结果；未执行交通查询或预览保存。人工质量未验收。'
                stage, started = 'evaluation', monotonic()
                with span('planning.modular.evaluation', 'validation') as step:
                    evaluation = evaluate_draft(snapshot, result)
                    runtime.check_cancelled()
                    record['evaluation'] = evaluation.model_dump(mode='json')
                    step.output = record['evaluation']
                runtime.check_cancelled()
                record['delivered'] = True
                record['status'] = (repaired.status if revision_generator is not None else
                    'partial' if any(issue.triggers_repair for issue in result.report.issues) else 'complete')
                root.output = {'status': record['status'], 'delivered': True, **_draft_summary(result)}
    except Exception as exc:
        record['error_code'] = getattr(exc, 'code', None) or stage.upper() + '_FAILED'
        record['error_step'] = stage
    finally:
        record['stage_elapsed_s'][stage] = round(monotonic() - started, 6)
        if observation is not None:
            if registered:
                try:
                    if record['delivered']:
                        # 与独立预览一样，只结束观测任务，不在旧行程结果表伪造TripPlan。
                        observation.repository.mark_preview_succeeded(task_id, task_id in observation.incomplete)
                    else:
                        observation.repository.fail(task_id, record['error_code'], '独立规划失败，请查看步骤诊断',
                            record['error_step'], task_id in observation.incomplete)
                except Exception as exc:
                    observation.missing(task_id, 'modular_task_end_failed', exc)
            try:
                for row in observation.repository.spans(task_id):
                    if row['operation_type'] == 'llm' and row['status'] == 'failed':
                        detail = observation.repository.span(task_id, row['span_id'])
                        record['failure_details'].append({'span_id': row['span_id'],
                            'model_call': (detail['output_data'] or {}).get('model_call'),
                            'model_configuration': (detail['output_data'] or {}).get('model_configuration')})
            except Exception as exc:
                observation.missing(task_id, 'modular_diagnostics_read_failed', exc)
    record['observation'] = {'task_id': task_id, 'registered': registered,
        'incomplete': task_id in observation.incomplete if observation is not None else True}
    return record
