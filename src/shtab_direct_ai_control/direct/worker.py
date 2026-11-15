import os
from celery import Celery

celery = Celery('direct_control', broker=os.getenv('SHTAB_DIRECT_REDIS_URL', 'redis://localhost:6379/0'))

@celery.task
def health_ping():
    return {'status': 'ok', 'mode': 'demo', 'external_requests': False}
