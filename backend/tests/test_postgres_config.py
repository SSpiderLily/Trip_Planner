import unittest
from app.config import Settings
class PostgresConfigTests(unittest.TestCase):
    def test_runtime_defaults_to_postgres_and_three_sources_share_dsn(self):
        settings=Settings(_env_file=None,database_url='postgresql://owner:synthetic-private@localhost/trip')
        self.assertEqual(settings.storage_backend,'postgresql')
        for kind in ('observation','preview','destination'):
            self.assertEqual(settings.storage_source(kind),settings.database_url)
        self.assertNotIn('synthetic-private',repr(settings))
    def test_missing_postgres_config_never_falls_back_to_sqlite(self):
        settings=Settings(_env_file=None,database_url='')
        with self.assertRaisesRegex(ValueError,'DATABASE_URL'):settings.storage_source('observation')
    def test_sqlite_is_explicit_fixture_mode_and_paths_stay_distinct(self):
        settings=Settings(_env_file=None,storage_backend='sqlite',task_db_path='/tmp/a',preview_db_path='/tmp/b',destination_candidate_db_path='/tmp/c')
        self.assertEqual([settings.storage_source(x) for x in ('observation','preview','destination')],['/tmp/a','/tmp/b','/tmp/c'])
        with self.assertRaises(ValueError):settings.storage_source('arbitrary')
