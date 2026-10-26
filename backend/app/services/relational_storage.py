"""PostgreSQL连接边界；显式SQLite路径仅供离线fixture和迁移源使用。

仓库继续使用参数化的问号占位符；这里只转换参数标记，不改写业务SQL。
PostgreSQL写事务使用仓库级事务锁，保留原SQLite串行接受/幂等语义。
"""
from __future__ import annotations
from pathlib import Path
import re
import sqlite3

SCHEMAS = {'observation':'trip_observation','preview':'trip_preview','destination':'trip_destination'}
LOCK_KEYS = {'observation':720260101,'preview':720260102,'destination':720260103}

class StorageRow(dict):
    def __init__(self, names, values):
        self._values=tuple(values)
        super().__init__(zip(names,self._values))
    def __getitem__(self,key):
        return self._values[key] if isinstance(key,int) else super().__getitem__(key)


def postgres_parameters(statement: str) -> str:
    """保留引号内问号，并按psycopg规则转义字面百分号。"""
    result=[]; quote=None; index=0
    while index<len(statement):
        char=statement[index]
        if quote:
            result.append('%%' if char=='%' else char)
            if char==quote:
                if index+1<len(statement) and statement[index+1]==quote:
                    result.append(quote);index+=1
                else:quote=None
        elif char in ("'",'"'):
            quote=char;result.append(char)
        else:result.append('%s' if char=='?' else '%%' if char=='%' else char)
        index+=1
    return ''.join(result)


def _row_factory(cursor):
    names=[column.name for column in cursor.description] if cursor.description else []
    return lambda values:StorageRow(names,values)


class PostgresConnection:
    def __init__(self, connection, namespace, schema):
        self.raw=connection;self.namespace=namespace;self.schema=schema;self._locked=False
    @property
    def in_transaction(self):
        from psycopg.pq import TransactionStatus
        return self.raw.info.transaction_status!=TransactionStatus.IDLE
    def _write_lock(self):
        if not self._locked:
            if self.raw.autocommit and not self.in_transaction:
                raise RuntimeError('PostgreSQL写入必须位于显式事务内')
            self.raw.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,%s))',(self.schema,LOCK_KEYS[self.namespace]))
            self._locked=True
    def execute(self, statement, parameters=None):
        if statement.strip().upper()=='BEGIN IMMEDIATE':
            cursor=self.raw.execute('BEGIN');self._write_lock();return cursor
        if statement.lstrip().split(None,1)[0].upper() in ('INSERT','UPDATE','DELETE'):
            self._write_lock()
        if parameters is None:return self.raw.execute(statement)
        return self.raw.execute(postgres_parameters(statement),parameters)
    def executemany(self,statement,parameters):
        self._write_lock()
        cursor=self.raw.cursor();cursor.executemany(postgres_parameters(statement),parameters);return cursor
    def executescript(self,script):
        # 本项目schema只包含简单DDL；不用于执行任意迁移函数或用户输入。
        for statement in script.split(';'):
            if statement.strip():self.execute(statement)
    def commit(self):
        self.raw.commit();self._locked=False
    def rollback(self):
        self.raw.rollback();self._locked=False
    def close(self):self.raw.close()
    def __enter__(self):return self
    def __exit__(self,kind,value,traceback):
        if kind is None:self.commit()
        else:self.rollback()
        return False


class StorageDatabase:
    def __init__(self,source: str|Path|StorageDatabase,namespace: str,*,timeout_seconds:float=5.0,schema_prefix:str|None=None):
        if namespace not in SCHEMAS:raise ValueError('未知数据库命名空间')
        if isinstance(source,StorageDatabase):
            schema_prefix=source.schema_prefix if schema_prefix is None else schema_prefix
            value=source._source
        else:value=str(source)
        if schema_prefix is not None and not re.fullmatch('[a-z][a-z0-9_]{0,31}',schema_prefix):
            raise ValueError('数据库schema前缀必须为32字符内小写标识符')
        self.is_postgres=value.startswith(('postgresql://','postgres://'))
        if '://' in value and not self.is_postgres:raise ValueError('不支持的数据库连接类型')
        self.path=None if self.is_postgres else Path(value)
        self.schema_prefix=schema_prefix
        self._source=value;self.namespace=namespace;self.schema=(schema_prefix+'_'+namespace if schema_prefix else SCHEMAS[namespace])
        self.timeout_seconds=float(timeout_seconds)
        if self.timeout_seconds<=0:raise ValueError('数据库等待时间必须大于0')
    def connect(self,*,autocommit:bool=True):
        if not self.is_postgres:
            connection=sqlite3.connect(self.path,timeout=self.timeout_seconds,isolation_level=None if autocommit else '')
            connection.row_factory=sqlite3.Row;connection.execute('PRAGMA foreign_keys=ON')
            return connection
        import psycopg
        from psycopg import sql
        raw=None
        try:
            raw=psycopg.connect(self._source,autocommit=True,row_factory=_row_factory,connect_timeout=10)
            raw.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(self.schema)))
            raw.execute("SELECT set_config('lock_timeout',%s,false)",(str(int(self.timeout_seconds*1000))+'ms',))
            raw.autocommit=autocommit
            return PostgresConnection(raw,self.namespace,self.schema)
        except Exception:
            if raw is not None:
                try:raw.close()
                except Exception:pass
            raise RuntimeError('数据库连接失败，请检查PostgreSQL配置与服务状态') from None
    def initialize(self,schema_sql):
        if self.path is not None:self.path.parent.mkdir(parents=True,exist_ok=True)
        conn=self.connect(autocommit=False)
        try:
            with conn:
                if self.is_postgres:
                    from psycopg import sql
                    self.lock(conn)
                    conn.raw.execute(sql.SQL('CREATE SCHEMA IF NOT EXISTS {}').format(sql.Identifier(self.schema)))
                    schema_sql=schema_sql.replace('INTEGER PRIMARY KEY AUTOINCREMENT','BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY')
                    schema_sql=re.sub(r'\bREAL\b','DOUBLE PRECISION',schema_sql)
                conn.executescript(schema_sql)
        finally:conn.close()
    def lock(self,conn):
        if self.is_postgres:
            conn.raw.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,%s))',(self.schema,LOCK_KEYS[self.namespace]))
    def columns(self,conn,table):
        if self.is_postgres:
            return {row['column_name'] for row in conn.execute('SELECT column_name FROM information_schema.columns WHERE table_schema=? AND table_name=?',(self.schema,table))}
        if not re.fullmatch('[a-z_]+',table):raise ValueError('无效表名')
        return {row['name'] for row in conn.execute('PRAGMA table_info('+table+')')}
