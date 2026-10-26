"""源只读检查和真实PG三仓库迁移演练；测试仅使用随机隔离schema。"""
import hashlib,json,os,sqlite3,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4
from app.services.postgres_migration import migrate_sqlite_sources,inspect_sqlite_sources,content_fingerprint,MigrationError
from app.services.task_repository import TaskRepository
from app.services.preview_repository import PreviewRepository
from app.services.destination_candidate_repository import DestinationCandidateRepository
from app.services import relational_storage
from test_preview_repository import baseline_snapshot

def seed(folder):
    sources={ns:Path(folder)/(ns+'.sqlite3') for ns in ('observation','preview','destination')}
    task=TaskRepository(sources['observation']);task.initialize(recover_running=False)
    task.create('migrated-task',{'city':'合成上海','nested':['?','100%']});task.start('migrated-task')
    for span,parent in (('z-first',None),('a-second','z-first')):
        task.begin_span((span,'migrated-task',parent,'llm.native_tool_loop','llm','2026-10-26T00:00:00Z','{"role":"master"}',0))
        task.end_span(span,'succeeded','{"kept":true}',False,None,dict(input_tokens=1,output_tokens=2,total_tokens=3))
    task.succeed('migrated-task',{'values':['合成',1,None]})
    preview=PreviewRepository(sources['preview']);preview.initialize();snapshot=baseline_snapshot()
    preview.create_job('migrated-task',snapshot.preview.draft.plan_id,'synthetic-access')
    preview.create(snapshot,access_token='synthetic-access')
    with preview._write_transaction() as c:
        c.execute('ALTER TABLE preview_drafts ADD COLUMN timing_json TEXT')
        c.execute('UPDATE preview_drafts SET timing_json=?',('{"legacy":"kept"}',))
        for i in range(3):
            c.execute('INSERT INTO preview_mutations(plan_id,mutation_kind,mutation_id,request_hash,result_status,result_store_version,created_at) VALUES (?,?,?,?,?,?,?)',
                      ('plan','save','receipt-'+str(i),'hash','saved',1,'2026-10-26'))
        c.execute('DELETE FROM preview_mutations WHERE mutation_seq>1')
    candidate=DestinationCandidateRepository(sources['destination']);candidate.initialize()
    with candidate._write_transaction() as c:
        c.execute('INSERT INTO destination_search_candidates VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            ('synthetic-candidate-hash','migrated-task',snapshot.preview.draft.plan_id,hashlib.sha256(b'synthetic-access').hexdigest(),
             'search','sightseeing','synthetic-poi','{"full":"合成旧资料"}','{"source":"kept"}',1234567.123456,1234627.123456))
    return sources

class MigrationSourceTests(unittest.TestCase):
    def test_source_inspection_is_complete_and_read_only(self):
        with tempfile.TemporaryDirectory() as folder:
            sources=seed(folder)
            before={ns:hashlib.sha256(path.read_bytes()).hexdigest() for ns,path in sources.items()}
            report=inspect_sqlite_sources(sources)
            self.assertEqual(sum(len(tables) for tables in report.values()),11)
            self.assertEqual(report['observation']['spans']['rows'],2)
            self.assertEqual(before,{ns:hashlib.sha256(path.read_bytes()).hexdigest() for ns,path in sources.items()})
    def test_fingerprint_is_order_independent_and_preserves_values(self):
        rows=[{'x':'中文','y':None},{'x':'?','y':1.25}]
        self.assertEqual(content_fingerprint(rows,['x','y']),content_fingerprint(reversed(rows),['x','y']))
        self.assertNotEqual(content_fingerprint(rows,['x','y']),content_fingerprint([{'x':'中文','y':0}],['x','y']))

@unittest.skipUnless(os.environ.get('TRIP_TEST_DATABASE_URL'),'未配置真实PostgreSQL迁移验收连接')
class PostgresMigrationTests(unittest.TestCase):
    def setUp(self):
        self.url=os.environ['TRIP_TEST_DATABASE_URL'];name=urlsplit(self.url).path.lstrip('/')
        self.assertTrue(name=='personal_web_dev' or any(x in name for x in ('test','validation')))
        self.folder=tempfile.TemporaryDirectory();self.addCleanup(self.folder.cleanup)
        self.sources=seed(self.folder.name)
        self.schemas={ns:'trip_migration_test_'+uuid4().hex for ns in relational_storage.SCHEMAS}
        self.schema_patch=patch.dict(relational_storage.SCHEMAS,self.schemas);self.schema_patch.start()
        self.addCleanup(self.schema_patch.stop);self.addCleanup(self.cleanup)
    def cleanup(self):
        import psycopg
        from psycopg import sql
        with psycopg.connect(self.url,autocommit=True) as c:
            for schema in self.schemas.values():c.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(sql.Identifier(schema)))
    def test_all_sources_old_columns_credentials_sequences_and_span_order_survive(self):
        report=migrate_sqlite_sources(self.sources,self.url)
        self.assertTrue(report['verified']);self.assertEqual(len(report['tables']),11)
        self.assertEqual(report['sequences']['preview.preview_mutations'],3)
        verified=migrate_sqlite_sources(self.sources,self.url,verify_only=True);self.assertTrue(verified['verified'])
        preview=PreviewRepository(self.url)
        self.assertIsNotNone(preview.job_status('migrated-task','synthetic-access'))
        self.assertIsNone(preview.job_status('migrated-task','wrong'))
        self.assertIsNotNone(preview.get_preview('synthetic-access'))
        with preview._write_transaction() as c:
            self.assertEqual(c.execute('SELECT timing_json FROM preview_drafts').fetchone()[0],'{"legacy":"kept"}')
            c.execute('INSERT INTO preview_mutations(plan_id,mutation_kind,mutation_id,request_hash,result_status,result_store_version,created_at) VALUES (?,?,?,?,?,?,?)',('plan','save','after','h','saved',1,'now'))
            self.assertEqual(c.execute('SELECT MAX(mutation_seq) FROM preview_mutations').fetchone()[0],4)
        tasks=TaskRepository(self.url)
        self.assertEqual([r['span_id'] for r in tasks.spans('migrated-task')],['z-first','a-second'])
        self.assertEqual(tasks.result('migrated-task'),{'values':['合成',1,None]})
    def test_nonempty_target_is_never_overwritten(self):
        migrate_sqlite_sources(self.sources,self.url)
        initialized=[]
        def initialize_probe(_target): initialized.append(True)
        with patch('app.services.postgres_migration._initialize_target',initialize_probe):
            with self.assertRaises(MigrationError):migrate_sqlite_sources(self.sources,self.url)
            self.assertEqual(len(initialized),0)
        self.assertEqual(TaskRepository(self.url).result('migrated-task'),{'values':['合成',1,None]})
    def test_failed_copy_rolls_back_all_three_namespaces(self):
        with sqlite3.connect(self.sources['destination']) as c:
            c.execute('PRAGMA ignore_check_constraints=ON');c.execute("UPDATE destination_search_candidates SET category='invalid'")
        with self.assertRaises(Exception):migrate_sqlite_sources(self.sources,self.url)
        tasks=TaskRepository(self.url)
        with tasks.connection() as c:self.assertEqual(c.execute('SELECT count(*) FROM tasks').fetchone()[0],0)
        preview=PreviewRepository(self.url)
        with preview._read_connection() as c:self.assertEqual(c.execute('SELECT count(*) FROM preview_drafts').fetchone()[0],0)
        from app.services.postgres_migration import TABLES,SEQUENCES
        with tasks.connection() as c:
            for namespace,tables in TABLES.items():
                for table in tables:
                    relation='"'+self.schemas[namespace]+'"."'+table+'"'
                    self.assertEqual(c.execute('SELECT count(*) FROM '+relation).fetchone()[0],0)
                    if table in SEQUENCES:
                        sequence=c.execute('SELECT pg_get_serial_sequence(?,?)',(relation,SEQUENCES[table])).fetchone()[0]
                        state=c.execute('SELECT last_value,is_called FROM '+sequence).fetchone()
                        self.assertEqual((state[0],state[1]),(1,False))
            self.assertEqual(tasks.size(),0)
    def test_verification_detects_tampering_without_repairing_target(self):
        migrate_sqlite_sources(self.sources,self.url)
        tasks=TaskRepository(self.url)
        with tasks.connection() as c:c.execute('UPDATE tasks SET request_data=?',('{"changed":true}',))
        with self.assertRaises(MigrationError):migrate_sqlite_sources(self.sources,self.url,verify_only=True)
        self.assertEqual(tasks.request('migrated-task'),{'changed':True})
