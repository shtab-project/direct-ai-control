"""Per-owner archive, durable revocation, and dispatch/disconnect serialization.

An operation lease spans the complete sync/poll operation, including commits.
Each HTTP dispatch additionally checks its credential under that same lease.
PostgreSQL session advisory locks work across API/worker processes.
"""
from contextlib import contextmanager,ExitStack
from contextvars import ContextVar
from functools import wraps
import hashlib
import threading

from fastapi import HTTPException
from sqlalchemy import select,text
from .db import Session,engine,AccountLifecycle,YandexConnection,ReportJob,Campaign

held=ContextVar('direct_connection_leases',default=frozenset())
local_locks={}
local_lock_guard=threading.Lock()


def archived(db,user_id,account_id):
    return bool(account_id and db.get(AccountLifecycle,(user_id,account_id)))


def archived_ids(db,user_id):
    return set(db.scalars(select(AccountLifecycle.account_id).where(AccountLifecycle.user_id==user_id)))


def analysis_accounts(db,run):
    if 'account_ids' in run.source:return set(run.source['account_ids'])
    campaign_ids={e.get('campaign_id') for e in run.source.get('evidence',{}).values() if e.get('campaign_id')}
    if campaign_ids:return set(db.scalars(select(Campaign.account_id).where(Campaign.id.in_(campaign_ids))))
    return set(db.scalars(select(Campaign.account_id).where(Campaign.project_id==run.project_id)))


def ensure_active_account(db,user_id,account_id):
    # Select fresh state, independent of the caller's identity map.
    if account_id and db.scalar(select(AccountLifecycle.account_id).where(AccountLifecycle.user_id==user_id,AccountLifecycle.account_id==account_id)):
        raise HTTPException(410,'Аккаунт отключён. Данные доступны только как архив.')


def active_connection(connection,user_id=None):
    with Session() as check:
        row=check.get(YandexConnection,connection)
        if not row or (user_id is not None and row.user_id!=user_id):
            raise HTTPException(404,'Подключение недоступно.')
        if row.status=='disconnected' or not row.encrypted_token:
            raise HTTPException(410,'OAuth-подключение отключено. Запросы в Директ запрещены.')
        ensure_active_account(check,row.user_id,row.account_id)
        return row


@contextmanager
def connection_lock(identifier):
    if identifier in held.get():
        yield; return
    token=held.set(held.get()|{identifier})
    try:
        if engine.dialect.name=='postgresql':
            key=int.from_bytes(hashlib.sha256(('direct:'+identifier).encode()).digest()[:8],'big',signed=True)
            with engine.connect() as lock_connection:
                lock_connection.execute(text("SET LOCAL statement_timeout = '120s'"))
                lock_connection.execute(text('SELECT pg_advisory_lock(:key)'),{'key':key})
                try: yield
                finally:
                    try: lock_connection.execute(text('SELECT pg_advisory_unlock(:key)'),{'key':key})
                    except Exception:
                        lock_connection.invalidate()
                        raise
        else:
            with local_lock_guard: lock=local_locks.setdefault(identifier,threading.RLock())
            with lock: yield
    finally:
        held.reset(token)


@contextmanager
def connection_lease(identifier,user_id=None):
    with connection_lock(identifier):
        row=active_connection(identifier,user_id)
        yield row


class Credential(str):
    def __new__(cls,value,connection_id,fingerprint):
        obj=super().__new__(cls,value)
        obj.connection_id=connection_id
        obj.fingerprint=fingerprint
        return obj


def credential(value,connection):
    return Credential(value,connection.id,hashlib.sha256(connection.encrypted_token.encode()).hexdigest())


@contextmanager
def credential_http(value):
    if not isinstance(value,Credential):
        raise HTTPException(403,'Для вызова Директа требуется активное серверное OAuth-подключение.')
    with connection_lease(value.connection_id) as row:
        if hashlib.sha256(row.encrypted_token.encode()).hexdigest()!=value.fingerprint:
            raise HTTPException(409,'OAuth-подключение изменилось. Повторите чтение с новым подключением.')
        yield


def guarded(kind):
    """Wrap entire connection operation; preserve FastAPI signature/ownership."""
    def decorator(fn):
        @wraps(fn)
        def wrapped(*args,**kwargs):
            import inspect
            params=inspect.signature(fn).bind(*args,**kwargs).arguments
            db=params['db'];user=params.get('user')
            if kind=='token' or kind=='sync': cid=params['connection'].id
            elif kind=='body': cid=params['body'].connection_id
            elif kind=='job':
                job=db.get(ReportJob,params['job_id'])
                if not job or job.user_id!=user.id:raise HTTPException(404,'Задание недоступно.')
                cid=job.connection_id
            else:
                c=db.get(Campaign,params['campaign_id'])
                if not c:raise HTTPException(404,'Кампания недоступна.')
                row=db.scalar(select(YandexConnection).where(YandexConnection.account_id==c.account_id,YandexConnection.user_id==user.id,YandexConnection.status!='disconnected').order_by(YandexConnection.updated_at.desc()))
                if not row:raise HTTPException(410,'Для кампании нет активного подключения.')
                cid=row.id
            with connection_lease(cid,user.id if user else None):
                return fn(*args,**kwargs)
        return wrapped
    return decorator
