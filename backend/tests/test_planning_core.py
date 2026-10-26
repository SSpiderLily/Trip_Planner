import unittest
from app.models.planning_execution import ProposalSubmission
from app.planning.core import generate_draft
from app.models.planning_errors import PlanningError
from test_planning_core_contracts import snapshot, load_case

class Runtime:
    def __init__(self): self.cancelled=False; self.events=[]
    def check_cancelled(self):
        if self.cancelled: raise PlanningError('CANCELLED','取消')
    def report(self,stage,completed=0,total=1,message=None):
        self.check_cancelled(); self.events.append(stage)

class Generator:
    def generate(self,context,*,accept,runtime):
        self.context=context
        return accept(ProposalSubmission.model_validate(load_case()['submission']))

class CoreTests(unittest.TestCase):
    def test_generate_with_stage_only_context(self):
        gen=Generator(); value=snapshot();before=value.model_dump()
        result=generate_draft(value,generator=gen,runtime=Runtime())
        self.assertEqual(result.identity,value.identity)
        self.assertFalse(hasattr(gen.context,'messages'))
        self.assertEqual(value.model_dump(),before)
    def test_unaccepted_result_is_rejected(self):
        class Forged:
            def generate(self,*args,**kwargs):return 'looks like a plan'
        with self.assertRaisesRegex(PlanningError,'未提交'):
            generate_draft(snapshot(),generator=Forged(),runtime=Runtime())
    def test_late_result_is_discarded(self):
        class Late(Generator):
            def generate(self,context,*,accept,runtime):
                value=super().generate(context,accept=accept,runtime=runtime)
                runtime.cancelled=True
                return value
        with self.assertRaises(PlanningError) as cm:
            generate_draft(snapshot(),generator=Late(),runtime=Runtime())
        self.assertEqual(cm.exception.code,'CANCELLED')
    def test_duplicate_accept_is_rejected(self):
        class Twice(Generator):
            def generate(self,context,*,accept,runtime):
                super().generate(context,accept=accept,runtime=runtime)
                return super().generate(context,accept=accept,runtime=runtime)
        with self.assertRaises(PlanningError) as cm:
            generate_draft(snapshot(),generator=Twice(),runtime=Runtime())
        self.assertEqual(cm.exception.code,'PLAN_ALREADY_ACCEPTED')
    def test_callback_failure_propagates(self):
        class Broken(Runtime):
            def report(self,*args,**kwargs):raise RuntimeError('callback failed')
        with self.assertRaisesRegex(RuntimeError,'规划进度回调失败'):
            generate_draft(snapshot(),generator=Generator(),runtime=Broken())
    def test_context_mutation_does_not_modify_facts(self):
        class Mutating(Generator):
            def generate(self,context,*,accept,runtime):
                context.reference_information['fact_sources'][0]['effective_params']['city']='篡改'
                return super().generate(context,accept=accept,runtime=runtime)
        result=generate_draft(snapshot(),generator=Mutating(),runtime=Runtime())
        self.assertEqual(result.facts.sources[0].effective_params['city'],'上海')

class AsyncRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_adapter_reports_on_original_event_loop(self):
        import asyncio
        from app.services.native_planning_runtime import NativePlanningRuntime
        from app.services.planning_runtime_adapter import NativePlanningRuntimeAdapter
        original=asyncio.get_running_loop();stages=[]
        async def progress(value):
            self.assertIs(asyncio.get_running_loop(),original)
            stages.append(value.stage)
        runtime=NativePlanningRuntime(progress=progress)
        result=await runtime.blocking(generate_draft,snapshot(),generator=Generator(),runtime=NativePlanningRuntimeAdapter(runtime))
        self.assertEqual(result.identity,snapshot().identity)
        self.assertEqual(stages,['proposal','validation','validation'])
    async def test_native_callback_failure_propagates(self):
        from app.services.native_planning_runtime import NativePlanningRuntime
        from app.services.planning_runtime_adapter import NativePlanningRuntimeAdapter
        from app.services.native_tool_loop import NativeToolLoopError
        async def progress(value):raise RuntimeError('callback')
        runtime=NativePlanningRuntime(progress=progress)
        with self.assertRaises(NativeToolLoopError) as cm:
            await runtime.blocking(generate_draft,snapshot(),generator=Generator(),runtime=NativePlanningRuntimeAdapter(runtime))
        self.assertEqual(cm.exception.code,'PROGRESS_CALLBACK_FAILED')
