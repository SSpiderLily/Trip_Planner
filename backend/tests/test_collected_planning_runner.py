"""独立装配保留阶段结果、失败边界与未知计量，不启动预览/交通。"""
import importlib.util
from pathlib import Path
import unittest
from app.models.planning_runtime import PlanningRunIdentity
from app.services.native_collection_adapter import NativeRequirementAdapter, NativeCollectionSession
from app.services.planning_runtime_adapter import SynchronousPlanningRuntime
from app.agents.native_daily_plan_agent import NativeDailyPlanAgent
from native_planning_fixtures import fixture_planner

PATH=Path(__file__).parents[1]/'scripts/run_collected_planning.py'

class RunnerTests(unittest.TestCase):
    def runner(self):
        spec=importlib.util.spec_from_file_location('collected_runner',PATH)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        return module

    def test_fake_dependency_assembly_delivers_normalized_snapshot(self):
        req,planner,models=fixture_planner(repair=False)
        record=self.runner().run_request(req,identity=PlanningRunIdentity(task_id='p3-task',plan_id='p3-plan'),
            requirements=NativeRequirementAdapter(interpreter=planner.requirement_interpreter),
            collection=NativeCollectionSession(collector=planner.collector),generator=NativeDailyPlanAgent(models[0]),
            runtime=SynchronousPlanningRuntime())
        self.assertTrue(record['delivered'])
        self.assertEqual(record['snapshot']['schema_version'],2)
        self.assertEqual(set(record['stage_elapsed_s']),{'requirements','collection','generation','evaluation'})
        self.assertNotIn('protocol',record['snapshot'])
        self.assertIsNone(record['error_code'])

    def test_generation_failure_preserves_collection_evidence(self):
        req,planner,models=fixture_planner(repair=False)
        class Failing:
            def generate(self,*args,**kwargs):raise RuntimeError('sensitive-provider-message')
        record=self.runner().run_request(req,identity=PlanningRunIdentity(task_id='p3-task',plan_id='p3-plan'),
            requirements=NativeRequirementAdapter(interpreter=planner.requirement_interpreter),
            collection=NativeCollectionSession(collector=planner.collector),generator=Failing(),runtime=SynchronousPlanningRuntime())
        self.assertFalse(record['delivered'])
        self.assertTrue(record['snapshot']['weather_evidence']['executions'])
        self.assertEqual(record['error_code'],'GENERATION_FAILED')
        self.assertNotIn('sensitive-provider-message',str(record))

    def test_cli_registers_task_in_explicit_observation_store(self):
        import tempfile,sys,io,json
        from contextlib import redirect_stdout
        from unittest.mock import patch
        from app.services.task_repository import TaskRepository
        req,planner,models=fixture_planner(repair=False)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory);request_path=path/'request.json';request_path.write_text(req.model_dump_json())
            db=path/'observations.db';module=self.runner();output=io.StringIO()
            with patch.object(sys,'argv',['run_collected_planning.py','--live','--request-file',str(request_path),'--observation-db',str(db)]), \
                    patch.object(sys,'path',[str(PATH.parent),*sys.path]), \
                    patch('app.services.llm_service.get_agent_llm',side_effect=models), \
                    patch('app.agents.native_multiagent_collection.NativeMultiagentCollection',return_value=planner.collector), \
                    patch('app.agents.native_requirement_interpreter.NativeRequirementInterpreter',return_value=planner.requirement_interpreter), \
                    redirect_stdout(output):
                code=module.main()
            self.assertEqual(code,0)
            record=json.loads(output.getvalue());repo=TaskRepository(db)
            self.assertEqual(repo.status(record['identity']['task_id'])['status'],'succeeded')
            self.assertTrue(repo.spans(record['identity']['task_id']))

    def test_cli_observation_initialization_failure_does_not_block_planning(self):
        import tempfile,sys,io,json
        from contextlib import redirect_stdout
        from unittest.mock import patch
        req,planner,models=fixture_planner(repair=False)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory);request_path=path/'request.json';request_path.write_text(req.model_dump_json())
            module=self.runner();output=io.StringIO()
            with patch.object(sys,'argv',['run_collected_planning.py','--live','--request-file',str(request_path),'--observation-db',str(path/'tasks.db')]), \
                    patch.object(sys,'path',[str(PATH.parent),*sys.path]), \
                    patch('app.services.llm_service.get_agent_llm',side_effect=models), \
                    patch('app.agents.native_multiagent_collection.NativeMultiagentCollection',return_value=planner.collector), \
                    patch('app.agents.native_requirement_interpreter.NativeRequirementInterpreter',return_value=planner.requirement_interpreter), \
                    patch('app.services.task_repository.TaskRepository.initialize',side_effect=OSError('private-storage-error')), \
                    redirect_stdout(output):
                code=module.main()
            r=json.loads(output.getvalue())
            self.assertEqual(code,0);self.assertTrue(r['delivered']);self.assertTrue(r['observation']['incomplete'])
            self.assertNotIn('private-storage-error',output.getvalue())

    def test_p4_explicit_assembly_repairs_with_same_normalized_snapshot(self):
        from app.agents.native_day_revision_agent import NativeDayRevisionAgent
        req,planner,models=fixture_planner(repair=True)
        record=self.runner().run_request(req,identity=PlanningRunIdentity(task_id='p4-task',plan_id='p4-plan'),
            requirements=NativeRequirementAdapter(interpreter=planner.requirement_interpreter),
            collection=NativeCollectionSession(collector=planner.collector),generator=NativeDailyPlanAgent(models[0]),
            revision_generator=NativeDayRevisionAgent(models[0]),runtime=SynchronousPlanningRuntime())
        self.assertTrue(record['delivered']);self.assertEqual(record['status'],'complete')
        self.assertEqual(record['initial_result']['draft']['revision'],1)
        self.assertEqual(record['result']['draft']['revision'],2)
        self.assertEqual(record['repair']['repair_states'][0]['completed_rounds'],1)
        self.assertIn('repair',record['stage_elapsed_s'])
        self.assertNotIn('protocol',record['snapshot']);self.assertNotIn('children',record['snapshot'])
