"""显式、可验证的SQLite→PostgreSQL迁移；不删除源文件或覆盖非空目标。"""
from __future__ import annotations
from contextlib import ExitStack, closing
import base64
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from .relational_storage import StorageDatabase

TABLES = {
    'observation': ('tasks','task_results','spans'),
    'preview': ('preview_drafts','preview_finals','preview_mutations','preview_restore_receipts',
                'preview_edit_receipts','preview_route_jobs','preview_jobs'),
    'destination': ('destination_search_candidates',),
}
SEQUENCES = {'spans':'span_sequence','preview_mutations':'mutation_seq',
             'preview_restore_receipts':'restore_seq','preview_edit_receipts':'edit_seq'}
class MigrationError(RuntimeError): pass

def _identifier(name):
    if not re.fullmatch('[A-Za-z_][A-Za-z0-9_]*',name):raise MigrationError('源或目标包含不支持的标识符')
    return '"'+name+'"'

def _bytes_json(value):
    if isinstance(value,(bytes,bytearray,memoryview)):return {'bytes':base64.b64encode(bytes(value)).decode('ascii')}
    raise TypeError('Unsupported migration value')

def content_fingerprint(rows,columns):
    """按行内容哈希再排序，避开两引擎排序规则差异；不输出原始内容。"""
    digests=[]
    for row in rows:
        values=[row[column] for column in columns]
        payload=json.dumps(values,ensure_ascii=False,separators=(',',':'),allow_nan=False,default=_bytes_json).encode()
        digests.append(hashlib.sha256(payload).digest())
    result=hashlib.sha256()
    for digest in sorted(digests):result.update(digest)
    return {'rows':len(digests),'sha256':result.hexdigest()}

def _source_connections(sources,stack):
    if set(sources)!=set(TABLES):raise MigrationError('必须显式提供三个SQLite迁移源')
    result={}
    for namespace,source in sources.items():
        path=Path(source).resolve()
        if not path.is_file():raise MigrationError('SQLite迁移源不存在')
        conn=stack.enter_context(closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)))
        conn.row_factory=sqlite3.Row;conn.execute('PRAGMA query_only=ON');conn.execute('BEGIN')
        tables={r['name'] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables-set(TABLES[namespace])-{'sqlite_sequence'}:
            raise MigrationError('SQLite源存在未支持的业务表；迁移已停止，不能忽略数据')
        if not set(TABLES[namespace]).issubset(tables):raise MigrationError('SQLite源缺少必需表')
        if conn.execute('PRAGMA foreign_key_check').fetchone() is not None:raise MigrationError('SQLite源的外键引用不闭合')
        result[namespace]=conn
    return result

def _column_info(connection,table):
    return list(connection.execute('PRAGMA table_info('+_identifier(table)+')'))

def inspect_sqlite_sources(sources):
    with ExitStack() as stack:
        connections=_source_connections(sources,stack)
        return {ns:{table:content_fingerprint(conn.execute('SELECT * FROM '+_identifier(table)),
                [row['name'] for row in _column_info(conn,table)]) for table in TABLES[ns]}
                for ns,conn in connections.items()}

def _initialize_target(target):
    from .task_repository import TaskRepository
    from .preview_repository import PreviewRepository
    from .destination_candidate_repository import DestinationCandidateRepository
    TaskRepository(target).initialize(recover_running=False)
    PreviewRepository(target).initialize()
    DestinationCandidateRepository(target).initialize()

def _legacy_type(declaration):
    name=declaration.upper()
    if 'INT' in name or name=='BOOLEAN':return 'BIGINT'
    if any(value in name for value in ('REAL','FLOA','DOUB')):return 'DOUBLE PRECISION'
    if 'BLOB' in name:return 'BYTEA'
    if not name or any(value in name for value in ('TEXT','CHAR','CLOB','DATE','TIME','NUMERIC','DECIMAL')):return 'TEXT'
    raise MigrationError('历史字段类型不受支持，迁移已停止')

def migrate_sqlite_sources(sources,target,*,verify_only=False):
    database=StorageDatabase(target,'observation')
    if not database.is_postgres:raise MigrationError('迁移目标必须是PostgreSQL')
    databases={ns:StorageDatabase(target,ns) for ns in TABLES}
    with ExitStack() as stack:
        source_connections=_source_connections(sources,stack)
        if not verify_only:
            # 拒绝非空目标时连初始化DDL也不执行。
            with closing(database.connect(autocommit=True)) as preflight:
                for namespace,tables in TABLES.items():
                    for table in tables:
                        qualified=_identifier(databases[namespace].schema)+'.'+_identifier(table)
                        if preflight.execute('SELECT to_regclass(?)',(qualified,)).fetchone()[0] is not None:
                            if preflight.execute('SELECT count(*) FROM '+qualified).fetchone()[0]:
                                raise MigrationError('PostgreSQL目标非空；请仅验证现有迁移，禁止覆盖')
            _initialize_target(target)
        conn=stack.enter_context(closing(database.connect(autocommit=True)))
        conn.execute('BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY' if verify_only else 'BEGIN')
        try:
            if not verify_only:
                for namespace in TABLES:databases[namespace].lock(conn)
                # 所有检查都在复制之前，非空库绝不隐式合并或覆盖。
                for namespace,tables in TABLES.items():
                    for table in tables:
                        qualified=_identifier(databases[namespace].schema)+'.'+_identifier(table)
                        if conn.execute('SELECT count(*) FROM '+qualified).fetchone()[0]:
                            raise MigrationError('PostgreSQL目标非空；请仅验证现有迁移，禁止覆盖')
            report={'mode':'verify_only' if verify_only else 'migrated','tables':{},'sequences':{},'verified':False}
            for namespace,tables in TABLES.items():
                source=source_connections[namespace];db=databases[namespace]
                for table in tables:
                    qualified=_identifier(db.schema)+'.'+_identifier(table)
                    info=_column_info(source,table);columns=[row['name'] for row in info]
                    target_columns=db.columns(conn,table)
                    missing=[row for row in info if row['name'] not in target_columns]
                    if verify_only and missing:raise MigrationError('目标缺少源字段，校验未通过')
                    for row in missing:
                        conn.execute('ALTER TABLE '+qualified+' ADD COLUMN '+_identifier(row['name'])+' '+_legacy_type(row['type']))
                    if not verify_only:
                        preserve_order=table=='spans' and 'span_sequence' not in columns
                        insert_columns=columns+(['span_sequence'] if preserve_order else [])
                        insert='INSERT INTO '+qualified+'('+','.join(_identifier(c) for c in insert_columns)+') VALUES ('+','.join('?' for _ in insert_columns)+')'
                        select=('SELECT rowid AS __migration_rowid,* FROM ' if preserve_order else 'SELECT * FROM ')+_identifier(table)
                        rows=source.execute(select)
                        while batch:=rows.fetchmany(100):
                            conn.executemany(insert,[tuple(row[c] for c in columns)+((row['__migration_rowid'],) if preserve_order else ()) for row in batch])
                    source_hash=content_fingerprint(source.execute('SELECT * FROM '+_identifier(table)),columns)
                    target_hash=content_fingerprint(conn.execute('SELECT '+','.join(_identifier(c) for c in columns)+' FROM '+qualified),columns)
                    if source_hash!=target_hash:raise MigrationError('迁移内容或计数不一致，已停止提交')
                    report['tables'][namespace+'.'+table]={**source_hash,'matched':True,'columns':len(columns)}
                    if table in SEQUENCES:
                        field=SEQUENCES[table]
                        target_max=conn.execute('SELECT COALESCE(MAX('+_identifier(field)+'),0) FROM '+qualified).fetchone()[0]
                        highwater=target_max
                        if table!='spans':
                            exists=source.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sqlite_sequence'").fetchone()
                            saved=source.execute('SELECT seq FROM sqlite_sequence WHERE name=?',(table,)).fetchone() if exists else None
                            highwater=max(highwater,saved[0] if saved else 0)
                        sequence=conn.execute('SELECT pg_get_serial_sequence(?,?)',(qualified,field)).fetchone()[0]
                        if not sequence:raise MigrationError('目标自增序列缺失')
                        sequence_name='.'.join(_identifier(part) for part in sequence.split('.'))
                        # ALTER SEQUENCE RESTART随复制事务回滚；setval不具备该保证。
                        if not verify_only:
                            if highwater>=9223372036854775807:raise MigrationError('源序列已耗尽')
                            conn.execute('ALTER SEQUENCE '+sequence_name+' RESTART WITH '+str(highwater+1))
                        state=conn.execute('SELECT last_value,is_called FROM '+sequence_name).fetchone()
                        next_value=state[0]+1 if state[1] else state[0]
                        if next_value!=highwater+1:
                            raise MigrationError('迁移自增序列高水位校验失败')
                        report['sequences'][namespace+'.'+table]=highwater
            report['verified']=True
            if verify_only:conn.rollback()
            else:conn.commit()
            return report
        except BaseException:
            conn.rollback();raise
