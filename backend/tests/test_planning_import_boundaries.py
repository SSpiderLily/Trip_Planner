"""以新进程验证核心及传递依赖不加载网络/配置/编排器。"""
from pathlib import Path
import subprocess
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
GUARD='''
import importlib.abc, sys, socket
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        allowed={'planning_v4_validator','planning_v4_validation','planning_candidate_policy','planning_lodging_policy'}
        if (fullname.startswith(('app.agents','app.api','app.config')) or
            (fullname.startswith('app.services.') and fullname.split('.')[2] not in allowed)):
            raise AssertionError('Forbidden dependency: '+fullname)
def denied(*args,**kwargs):raise AssertionError('Network forbidden')
socket.socket.connect=denied
socket.create_connection=denied
sys.meta_path.insert(0,Guard())
'''
class ImportBoundaryTests(unittest.TestCase):
    def test_core_dependencies_are_pure(self):
        result=subprocess.run([sys.executable,'-c',GUARD+'''
from app.planning.core import generate_draft
from app.planning.validation import accept_submission
from app.planning.context import build_context
from app.planning.evaluation import evaluate_draft
from app.models.planning_execution import PlanningSnapshot
'''],cwd=ROOT,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
    def test_fake_cli_does_not_load_clients_or_config(self):
        guard=GUARD.replace("'planning_lodging_policy'}","'planning_lodging_policy','planning_runtime_adapter'}")
        result=subprocess.run([sys.executable,'-c',guard+'''
import runpy
sys.argv=['run_planning_core.py','--mode','fake','--input-file','tests/fixtures/planning_core/normal.json']
runpy.run_path('scripts/run_planning_core.py',run_name='__main__')
'''],cwd=ROOT,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        import json
        output=json.loads(result.stdout)
        self.assertEqual(output['mode'],'fake')
        self.assertEqual(output['source_type'],'synthetic')
        self.assertEqual(len(output['result']['draft']['days']),2)
    def test_static_transitive_imports_including_local_and_type_checking(self):
        import ast
        from importlib.util import resolve_name
        allowed={'planning_v4_validator','planning_v4_validation','planning_candidate_policy','planning_lodging_policy'}
        pending=['app.planning.core','app.planning.validation','app.planning.context','app.planning.contracts','app.planning.evaluation']
        visited=set()
        while pending:
            module=pending.pop()
            if module in visited:continue
            visited.add(module)
            path=ROOT.joinpath(*module.split('.')).with_suffix('.py')
            if not path.exists():continue
            package=module.rsplit('.',1)[0]
            for node in ast.walk(ast.parse(path.read_text())):
                names=[]
                if isinstance(node,ast.Import):names=[item.name for item in node.names]
                elif isinstance(node,ast.ImportFrom):
                    name=resolve_name('.'*node.level+(node.module or ''),package) if node.level else node.module
                    names=[name]+[name+'.'+item.name for item in node.names]
                for name in names:
                    if not name.startswith('app.'):continue
                    self.assertFalse(name.startswith(('app.agents','app.api','app.config')), (module,name))
                    if name.startswith('app.services.'):
                        self.assertIn(name.split('.')[2],allowed,(module,name))
                    pending.append(name)
