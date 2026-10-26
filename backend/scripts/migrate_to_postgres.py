"""显式迁移停写后的三个SQLite备份到PostgreSQL；凭据从环境读取，不接受命令行DSN。"""
import argparse,json,os,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from app.services.postgres_migration import migrate_sqlite_sources,inspect_sqlite_sources

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tasks',type=Path,required=True);parser.add_argument('--previews',type=Path,required=True)
    parser.add_argument('--candidates',type=Path,required=True)
    mode=parser.add_mutually_exclusive_group();mode.add_argument('--apply',action='store_true');mode.add_argument('--verify-only',action='store_true')
    parser.add_argument('--report',type=Path)
    args=parser.parse_args();sources={'observation':args.tasks,'preview':args.previews,'destination':args.candidates}
    try:
        if args.apply or args.verify_only:
            from dotenv import load_dotenv
            load_dotenv(ROOT/'.env',override=False)
            from app.services.relational_storage import StorageDatabase
            target=StorageDatabase(os.environ.get('DATABASE_URL',''),'observation',schema_prefix=os.environ.get('DATABASE_SCHEMA_PREFIX','trip'))
            result=migrate_sqlite_sources(sources,target,verify_only=args.verify_only)
        else:result={'mode':'source_only','tables':inspect_sqlite_sources(sources)}
        text=json.dumps(result,ensure_ascii=False,indent=2)
        if args.report:
            # 报告只含数量/哈希，但依旧限定本机权限，防止意外公开。
            with os.fdopen(os.open(args.report,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600),'w') as file:file.write(text+'\n')
        print(text);return 0
    except Exception as exc:
        print('迁移未完成：'+type(exc).__name__+'；请检查源备份、空目标与连接配置。未输出SQL或凭据。',file=sys.stderr)
        return 2
if __name__=='__main__':raise SystemExit(main())
