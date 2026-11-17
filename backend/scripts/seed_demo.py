import os
from sqlalchemy import select, text
from src.config.database import Base,engine,Session
from src.models.entities import User,LogSet,CrashCase
from src.services.wal_engine import freeze_page_image
from src.utils.security import hash_password
RECORDS=[{'op':'begin','tx':'T1'},{'op':'write','tx':'T1','key':'sample_A','value':75},{'op':'commit','tx':'T1'},{'op':'checkpoint'},{'op':'begin','tx':'T2'},{'op':'write','tx':'T2','key':'sample_B','value':40},{'op':'begin','tx':'T3'},{'op':'write','tx':'T3','key':'sample_A','value':60},{'op':'commit','tx':'T3'},{'op':'write','tx':'T2','key':'sample_B','value':35},{'op':'commit','tx':'T2'}]
INITIAL={'sample_A':100,'sample_B':50}
# name -> scenario knobs; crash_after-only entries stay legacy (no-force pages).
CASES=[
    ('断电点10-撤销T2',{'crash_after':10}),
    ('完整提交点11',{'crash_after':11}),
    ('初始点0',{'crash_after':0}),
    ('未提交页已写回-撤销T2',{'executed_through':10,'log_flushed_through':10,'page_flushes':{'sample_B':10}}),
    ('提交日志未落盘-撤销T3',{'executed_through':9,'log_flushed_through':8,'page_flushes':{'sample_A':4}}),
    ('同键多次写入-页停在首个值',{'executed_through':10,'log_flushed_through':10,'page_flushes':{'sample_B':6}}),
]

def ensure_columns():
    """Additive columns for existing demo volumes; create_all only adds tables."""
    with engine.begin() as conn:
        for name,kind in [('executed_through','INTEGER'),('log_flushed_through','INTEGER'),('page_flushes','JSON'),('page_image','JSON')]:
            conn.execute(text(f'ALTER TABLE crash_cases ADD COLUMN IF NOT EXISTS {name} {kind}'))

def scenario_columns(knobs):
    if 'crash_after' in knobs:
        return {'crash_after':knobs['crash_after'],'executed_through':None,'log_flushed_through':None,'page_flushes':None,'page_image':None}
    executed=knobs['executed_through'];flushed=knobs.get('log_flushed_through',executed);flushes=knobs.get('page_flushes') or {}
    image=freeze_page_image(INITIAL,RECORDS,executed_through=executed,log_flushed_through=flushed,page_flushes=flushes)
    return {'crash_after':executed,'executed_through':executed,'log_flushed_through':flushed,'page_flushes':flushes,'page_image':image}

def seed():
    Base.metadata.create_all(engine)
    # Additive columns must exist before the API or worker touches the table,
    # even when demo data seeding is disabled on an existing volume.
    ensure_columns()
    if os.getenv('SEED_DEMO','true').lower()!='true':return
    with Session() as db:
        for role in ('admin','operator','viewer'):
            email=role+'@haili.local'
            if not db.scalar(select(User).where(User.email==email)):db.add(User(email=email,role=role,password_hash=hash_password('Recovery2026!')))
        logs=db.scalar(select(LogSet).where(LogSet.name=='海砾采样账本-晚班'))
        if logs is None:
            logs=LogSet(name='海砾采样账本-晚班',description='A初始100份、B初始50份，T1提交后检查点，T2未提交，T3已提交',initial=INITIAL,records=RECORDS);db.add(logs);db.flush()
        for name,knobs in CASES:
            if not db.scalar(select(CrashCase).where(CrashCase.name==name)):
                db.add(CrashCase(name=name,log_set_id=logs.id,**scenario_columns(knobs)))
        db.commit()
if __name__=='__main__':seed()
