"""在真实PG中复跑鉴权矩阵，并验证正式应用三仓库装配。"""
import contextlib,io,json,os,unittest
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
import test_task_scoped_observation as fixtures
from app.api.routes.trip import router
from app.services.relational_storage import StorageDatabase
from app.services.task_repository import TaskRepository
from app.services.preview_repository import PreviewRepository
from app.services.observation_service import ObservationService
from app.services.task_service import TaskService
from app.services.preview_task_service import PreviewTaskService

@unittest.skipUnless(os.environ.get('TRIP_TEST_DATABASE_URL'),'未配置真实PostgreSQL接口验收连接')
class PostgresObservationApiTests(fixtures.TaskScopedObservationTests):
    def setUp(self):
        self.url=os.environ['TRIP_TEST_DATABASE_URL'];name=urlsplit(self.url).path.lstrip('/')
        self.assertTrue(name=='personal_web_dev' or any(x in name for x in ('test','validation')))
        self.prefix='trip_api_test_'+uuid4().hex[:16]
        source=StorageDatabase(self.url,'observation',schema_prefix=self.prefix)
        self.repo=TaskRepository(source);self.repo.initialize(recover_running=False)
        self.previews=PreviewRepository(source);self.previews.initialize()
        self.service=TaskService(self.repo,ObservationService(self.repo),lambda:None)
        app=FastAPI();app.include_router(router,prefix='/api');app.state.task_service=self.service
        app.state.preview_task_service=PreviewTaskService(self.service,self.previews)
        self.client=TestClient(app)
        for task in ('a','b'):
            self.repo.create(task,{});self.repo.start(task)
            self.previews.create_job(task,'plan-'+task,'synthetic-token-'+task)
            self.repo.begin_span(('span-'+task,task,None,'llm.native_tool_loop','llm','2026-10-26T00:00:00+00:00',json.dumps({'role':'master','marker':'private-'+task}),0))
        self.headers={'Authorization':'Bearer synthetic-token-a'}
    def tearDown(self):
        self.client.close()
        self.drop(self.prefix)
    def drop(self,prefix):
        import psycopg
        from psycopg import sql
        with psycopg.connect(self.url,autocommit=True) as c:
            for namespace in ('observation','preview','destination'):
                c.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(sql.Identifier(prefix+'_'+namespace)))
    def test_formal_application_uses_postgres_for_all_three_repositories(self):
        from app.api import main
        from app.config import Settings
        prefix='trip_app_test_'+uuid4().hex[:16]
        self.addCleanup(self.drop,prefix)
        config=Settings(_env_file=None,storage_backend='postgresql',database_url=self.url,database_schema_prefix=prefix)
        with patch.object(main,'settings',config),patch.object(main,'validate_config'),patch.object(main,'print_config'),contextlib.redirect_stdout(io.StringIO()):
            with TestClient(main.app) as client:
                self.assertTrue(main.app.state.task_service.repository.database.is_postgres)
                self.assertTrue(main.app.state.preview_repository.database.is_postgres)
                self.assertIsNone(main.app.state.destination_candidate_repository.path)
                self.assertEqual(main.app.state.destination_candidate_repository.count(),0)
                main.app.state.task_service.repository.create('application-task',{})
                main.app.state.task_service.repository.start('application-task')
                main.app.state.preview_repository.create_job('application-task','plan-app','synthetic-app-token')
                response=client.get('/api/trip/v4/tasks/application-task/metrics',headers={'Authorization':'Bearer synthetic-app-token'})
                self.assertEqual(response.status_code,200)
                self.assertEqual(response.json()['task']['status'],'running')
                self.assertEqual(client.get('/health').status_code,200)
