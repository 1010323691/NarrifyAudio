"""Mix every registered task through real API/PG/Redis/workers with fake model boundaries."""
import argparse
import shutil
import socket
import concurrent.futures as cf
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import threading
import uuid
import wave

import httpx2 as httpx
import psycopg
from psycopg import sql

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--users', type=int, default=20)
parser.add_argument('--workers', type=int, nargs='+', default=[4])
parser.add_argument('--model-workers', type=int, default=4, help='Additional dedicated model workers; --workers then counts mechanical workers')
parser.add_argument('--model-delay', type=float, default=.2, help='Fallback delay for quick smoke tests')
parser.add_argument('--llm-delay', type=float)
parser.add_argument('--tts-min-delay', type=float)
parser.add_argument('--tts-max-delay', type=float)
parser.add_argument('--merge-delay', type=float, default=.2)
parser.add_argument('--postgres-admin-url', required=True, help='Local test PostgreSQL DSN with CREATE DATABASE permission')
parser.add_argument('--redis-server', default=shutil.which('redis-server'))
parser.add_argument('--output-dir', type=Path)
parser.add_argument('--deadline', type=int, default=300)
args = parser.parse_args()
if args.users < 2 or min(args.workers) < 1 or args.model_delay < 0 or args.model_workers < 1:
    parser.error('users >= 2, workers >= 1 and model-delay >= 0 required')
args.llm_delay = args.model_delay if args.llm_delay is None else args.llm_delay
args.tts_min_delay = args.model_delay if args.tts_min_delay is None else args.tts_min_delay
args.tts_max_delay = args.tts_min_delay if args.tts_max_delay is None else args.tts_max_delay
if args.llm_delay < 0 or args.merge_delay < 0 or not 0 <= args.tts_min_delay <= args.tts_max_delay:
    parser.error('delays must be nonnegative and tts-min-delay <= tts-max-delay')
if not args.redis_server:
    parser.error('redis-server executable required')
ROOT = Path(__file__).resolve().parents[2]
RUN = (args.output_dir or ROOT/'work') / ('mixed-'+uuid.uuid4().hex[:8])
RUN = RUN.resolve()
RUN.mkdir(parents=True)
DB = 'mixed_'+uuid.uuid4().hex[:12]
CONN = psycopg.conninfo.conninfo_to_dict(args.postgres_admin_url)
CONN['dbname'] = 'postgres'
from sqlalchemy.engine import URL
DATABASE_URL = URL.create('postgresql+psycopg', username=CONN.get('user'), password=CONN.get('password'),
    host=None if CONN.get('host','').startswith('/') else CONN.get('host'),
    port=int(CONN['port']) if CONN.get('port') else None, database=DB,
    query={'host':CONN['host']} if CONN.get('host','').startswith('/') else {}).render_as_string(hide_password=False)
def free_port():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1',0))
        return listener.getsockname()[1]
API_PORT, LLM_PORT, REDIS_PORT = free_port(), free_port(), free_port()
API_URL = f'http://127.0.0.1:{API_PORT}'
env = dict(os.environ, NARRIFY_DATABASE_URL=DATABASE_URL,
    NARRIFY_DB_ROLE='api', NARRIFY_STORAGE_ROOT=str(RUN/'storage'), NARRIFY_AUTO_CREATE_SCHEMA='true',
    NARRIFY_REDIS_URL=f'redis://127.0.0.1:{REDIS_PORT}/0', NARRIFY_BOOTSTRAP_ADMIN_EMAIL='',
    NARRIFY_BOOTSTRAP_ADMIN_PASSWORD='', NARRIFY_INITIAL_QUOTA_UNITS='10000')
if args.model_workers:
    env.update(NARRIFY_WORKER_DB_POOL_SIZE='3', NARRIFY_WORKER_DB_POOL_MAX_OVERFLOW='1',
               NARRIFY_WORKER_DB_LOCK_POOL_SIZE='1', NARRIFY_WORKER_DB_LOCK_POOL_MAX_OVERFLOW='0')
processes = []
handles = []
report = {'run': str(RUN), 'users_per_stage': args.users, 'live_submission': True,
          'interval_seconds': .01, 'gpu_managed': False, 'stages': []}
monitor_stop = threading.Event()
monitor_stats = {'peak_connections': 0, 'samples': 0, 'errors': 0}
monitor_thread = None

def monitor_connections():
    with psycopg.connect(**CONN, autocommit=True) as db:
        while not monitor_stop.is_set():
            try:
                count = db.execute('SELECT count(*) FROM pg_stat_activity WHERE datname = %s', (DB,)).fetchone()[0]
                monitor_stats['peak_connections'] = max(monitor_stats['peak_connections'], count)
                monitor_stats['samples'] += 1
            except psycopg.Error:
                monitor_stats['errors'] += 1
            monitor_stop.wait(.1)

def launch(args, name):
    f = (RUN / (name + '.log')).open('w')
    handles.append(f)
    p = subprocess.Popen(args, cwd=ROOT, env=env, stdout=f, stderr=subprocess.STDOUT)
    processes.append(p)
    return p

def stop(p):
    if p.poll() is None:
        p.terminate()
        try:
            p.wait(timeout=12)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()

def checked(r, statuses=(200, 201)):
    assert r.status_code in statuses, (r.status_code, r.text[:1500])
    return r.json()

def percentile(xs, q):
    xs = sorted(xs)
    return round(xs[min(len(xs)-1, int((len(xs)-1)*q))], 3) if xs else None
import random
import zipfile
from collections import Counter
FIXTURES = ROOT/'backend/tests/load_fixtures'
env.update(NARRIFY_TEST_MUSIC_LIBRARY=str(RUN/'music'), NARRIFY_TEST_WAV=str(RUN/'template.wav'),
           NARRIFY_TEST_MP3=str(RUN/'template.mp3'), NARRIFY_TEST_TTS_MIN_DELAY=str(args.tts_min_delay), NARRIFY_TEST_TTS_MAX_DELAY=str(args.tts_max_delay),
           NARRIFY_TEST_MERGE_DELAY=str(args.merge_delay), NARRIFY_TEST_TTS_REPORT_DIR=str(RUN/'tts-calls'))
try:
    with psycopg.connect(**CONN, autocommit=True) as db:
        db.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(DB)))
    launch([args.redis_server,'--bind','127.0.0.1','--port',str(REDIS_PORT),'--save','','--appendonly','no'], 'redis')
    launch([sys.executable,str(FIXTURES/'model_server.py'),'--port',str(LLM_PORT),'--report',str(RUN/'llm.json'),'--delay',str(args.llm_delay)], 'llm')
    launch([sys.executable,'-m','uvicorn','backend.main:app','--host','127.0.0.1','--port',str(API_PORT),'--no-access-log'], 'api')
    for _ in range(100):
        try:
            if httpx.get(API_URL+'/api/health').status_code==200:
                break
        except httpx.HTTPError:
            pass
        time.sleep(.1)
    else:
        raise RuntimeError('API startup failed')
    os.environ.update(env)
    sys.path.insert(0,str(ROOT))
    from backend.platform.database import SessionLocal, engine, lock_engine
    from backend.platform.models import Task, TaskAttempt, TaskResult, ProjectFile, User, UserQuotaAccount, QuotaHold, QuotaTransaction, SystemConfig, GPURequest
    from backend.platform.task_registry import TASK_TYPES
    from backend.platform.storage import project_workspace_path
    from backend.platform.resource_inventory import build_index, internal_path, source_version
    from backend.tests.resource_delivery_helpers import record_delivery
    from backend.platform.platform_settings import settings as platform_settings
    from backend.core.config import AppConfig
    from backend.core import paths as core_paths
    from backend.engines import music, tts_batch
    from sqlalchemy import select,func
    core_paths.MUSIC_LIBRARY_DIR=RUN/'music'
    core_paths.MUSIC_LIBRARY_DIR.mkdir()
    with wave.open(str(RUN/'template.wav'),'wb') as f:
        f.setnchannels(1); f.setsampwidth(2); f.setframerate(16000)
        f.writeframes(b'\0\0'*16000*2)
    subprocess.run(['ffmpeg','-v','error','-i',str(RUN/'template.wav'),'-c:a','libmp3lame',str(RUN/'template.mp3')],check=True)
    config=AppConfig().model_dump()
    config['llm'].update(base_url=f'http://127.0.0.1:{LLM_PORT}/v1',model_name='fixture',api_key='local',stream=False)
    config['prompts'].update(system_prompt='FAKE_SCRIPT',user_prompt='{chunk}')
    config['persona_prompts'].update(system_prompt='FAKE_PERSONA',user_prompt='{speaker}')
    for key in ('spot_check_enabled','revalidate_splits','check_boundary_speakers','validate_instructs','check_chunk_alignment','check_long_paragraphs'):
        config['generation'][key]=False
    # Recovery probes use platform configuration, not an individual task snapshot.
    with SessionLocal.begin() as db:
        db.merge(SystemConfig(key='application.features', value=config))
    index=music._default_index()
    index['tags']['scene']=['测试场景']
    monitor_thread=threading.Thread(target=monitor_connections,daemon=True); monitor_thread.start()
    for stage,worker_count in enumerate(args.workers):
        sessions=[]
        for i in range(args.users):
            client=httpx.Client(base_url=API_URL,timeout=120)
            name=f'mix{stage}_{i}_{uuid.uuid4().hex[:6]}'
            reg=checked(client.post('/api/auth/register',json={'email':name+'@example.invalid','username':name,'password':'load-test-pass-1234'}))
            client.headers['X-CSRF-Token']=reg['csrf_token']
            owner=reg['user']['id']
            project=checked(client.post('/api/v1/projects',json={'name':name}))['id']
            checked(client.put('/api/v1/projects/active',json={'project_id':project}))
            txt=checked(client.post('/api/files/upload',files={'file':('source.txt',f'第一章 测试\n用户{name}走进房间。\n他说今天开始并发测试。'.encode(),'text/plain')}))['id']
            resource_project=checked(client.post('/api/v1/projects',json={'name':name+'-resources'}))['id']
            cleanup_project=checked(client.post('/api/v1/projects',json={'name':name+'-cleanup'}))['id']
            with SessionLocal.begin() as db:
                db.get(User,owner).role='admin'
                root=project_workspace_path(db,name,project)
                rr=project_workspace_path(db,name,resource_project)
                cr=project_workspace_path(db,name,cleanup_project)
            def write(root,relative,data):
                p=root/relative; p.parent.mkdir(parents=True,exist_ok=True)
                p.write_bytes(data if isinstance(data,bytes) else json.dumps(data,ensure_ascii=False).encode())
                return p
            voice={'type':'foundation','description':'清晰男声','ref_text':'这是用于并发测试的参考语音。','gender':'male','foundation_status':'done'}
            vc={'旁白':dict(voice),'克隆角色':dict(voice),'基础角色':dict(voice)}
            write(root,'04_voice_profiles/voice_config.json',vc)
            for stem in ('batch','preview','segment','match','reset','merge','foundation','clone'):
                speaker={'foundation':'基础角色','clone':'克隆角色'}.get(stem,'旁白')
                lines=[{'type':'NARRATOR','speaker':speaker,'text':f'用户{name}的{stem}测试第一段。','instruct':'平静'},
                       {'type':'NARRATOR','speaker':speaker,'text':f'用户{name}的{stem}测试第二段。','instruct':'平静'}]
                write(root,f'03_parsed_json/{stem}.json',lines)
            for stem in ('merge','reset','segment'):
                manifest=[]
                for n in range(2):
                    relative=f'05_audio_chunk/{stem}/{n+1:04d}.mp3'
                    write(root,relative,(RUN/'template.mp3').read_bytes())
                    manifest.append({'index':n,'speaker':'旁白','text':f'用户{name}的{stem}测试第'+('一' if n==0 else '二')+'段。','ok':True,'path':relative,'voice_used':tts_batch.voice_params('旁白',vc)})
                write(root,f'05_audio_chunk/{stem}/manifest.json',manifest)
            for relative,kind in [('06_audio_merge/mix.mp3','tts.merge'),('06_audio_merge/match.mp3','tts.merge'),('06_audio_merge/segment.mp3','tts.merge'),('08_bgm/package.mp3','bgm.mix')]:
                write(root,relative,(RUN/'template.mp3').read_bytes()); record_delivery(owner,project,relative,kind)
            write(root,'08_bgm/bgm_assignments.json',{'version':1,'chapters':{'mix':{'music':'base.mp3','mode':'random'}}})
            write(rr,'08_bgm/seed.mp3',(RUN/'template.mp3').read_bytes()); record_delivery(owner,resource_project,'08_bgm/seed.mp3')
            stale=write(cr,'00_temp/stale.bin',b'expired'); os.utime(stale,(time.time()-9*86400,)*2)
            write(cr,'00_temp/fresh.bin',b'keep')
            refs=[]
            for pid,r in ((resource_project,rr),(cleanup_project,cr)):
                with SessionLocal() as db:
                    path=internal_path(db,owner,pid,'prepare.sqlite')
                    summary=build_index(r,path,project_id=pid,version=source_version(db,owner,pid),check=lambda:None,progress=lambda n:None)
                    final=internal_path(db,owner,pid,summary['snapshot_id']+'.sqlite'); path.rename(final)
                    internal_path(db,owner,pid,'current.json').write_text(json.dumps({'snapshot_id':summary['snapshot_id']}))
                    refs.append({'project_id':pid,'snapshot_id':summary['snapshot_id']})
            track=f'{name}.mp3'; shutil.copyfile(RUN/'template.mp3',RUN/'music'/track)
            index['tracks'][track]={'duration':2,'enabled':True,'description':'测试场景音乐','tags':{}}
            payloads={
                'text.format':{'input_file_id':txt,'output_name':'formatted.txt'},
                'book.analyze':{'input_file_id':txt}, 'book.split':{'input_file_id':txt,'whole_book':True,'base':'book'},
                'script.parse':{'input_file_id':txt,'output_name':'parsed.json'},
                'resources.scan':{'project_ids':[project]},
                'resources.package':{'scope':{'snapshots':[refs[0]],'category':'deliverables'}},
                'resources.cleanup':{'snapshots':[refs[1]]},
                'voices.foundation':{'script':'foundation.json','speakers':['基础角色']},
                'voices.clone':{'script':'clone.json','speakers':['克隆角色'],'candidate_count':1},
                'tts.batch':{'scripts':['batch.json'],'concurrency':2}, 'tts.merge':{'package':'merge'},
                'tts.preview_render':{'script':'preview.json','render':[{'index':0}]},
                'bgm.segment':{'stem':'segment'}, 'bgm.mix':{'stem':'mix'},
                'bgm.match':{'chapters':['match'],'mode':'random'}, 'bgm.package':{'chapters':['package'],'base':'bgm'},
                'music.suggest_tags':{'name':track},
                'tts.reset':{'scripts':['reset.json']}}
            assert set(payloads)==set(TASK_TYPES)
            sessions.append((client,owner,project,payloads,resource_project,cleanup_project,root,cr,track))
        shutil.copyfile(RUN/'template.mp3',RUN/'music/base.mp3')
        index['tracks']['base.mp3']={'duration':2,'enabled':True,'description':'base','tags':{'scene':['测试场景']}}
        music.save_index(index)
        monitor_stats.update(peak_connections=0,samples=0,errors=0)
        workers=[]
        for lane,count in (('mechanical',worker_count),('model',args.model_workers)):
            for i in range(count):
                wid=f'mixed-{stage}-{lane}-{i}'
                workers.append(launch([sys.executable,str(FIXTURES/'worker_launcher.py'),'--worker-id',wid,'--interval','.01','--task-lane',lane],f'worker-{stage}-{lane}-{i}'))
        def submit_user(s):
            c,owner,project,payloads,rp,cp,*_=s
            order=list(payloads); random.Random(int(s[6].parent.name.split('_')[1])).shuffle(order)
            jobs=[];latency=[]
            for n,kind in enumerate(order):
                p={**payloads[kind],'config':config}
                target=rp if kind=='resources.package' else cp if kind=='resources.cleanup' else project
                body={'project_id':target,'task_type':kind,'payload':p,'idempotency_key':uuid.uuid4().hex}
                start=time.monotonic(); r=checked(c.post('/api/v1/tasks',json=body));latency.append(time.monotonic()-start)
                jobs.append((r['id'],owner,target,kind))
                if n==0: assert checked(c.post('/api/v1/tasks',json=body))['id']==r['id']
                if n%4==0: checked(c.get('/api/v1/tasks'))
            return jobs,latency
        llm_before=len(json.loads((RUN/'llm.json').read_text())['calls']) if (RUN/'llm.json').exists() else 0
        start=time.monotonic()
        with cf.ThreadPoolExecutor(max_workers=args.users) as pool:
            batches=list(pool.map(submit_user,sessions))
        jobs=[j for batch,_ in batches for j in batch];ids=[j[0] for j in jobs]
        latency=[x for _,v in batches for x in v]
        submit_seconds=time.monotonic()-start
        if len(sessions)>1: assert sessions[1][0].get('/api/v1/tasks/'+jobs[0][0]).status_code==404
        peak=0; renewed=set(); api_latency=[]; api_errors=[]; probe_next=time.monotonic(); probe_index=0
        while time.monotonic()-start < args.deadline:
            with SessionLocal() as db:
                active_attempts=db.execute(select(TaskAttempt.id,TaskAttempt.started_at,TaskAttempt.lease_expires_at).where(TaskAttempt.task_id.in_(ids),TaskAttempt.status=='running')).all()
                for aid,begin,expires in active_attempts:
                    if expires and (expires-begin).total_seconds()>platform_settings.task_lease_seconds+1:renewed.add(aid)
                counts=dict(db.execute(select(Task.status,func.count()).where(Task.id.in_(ids)).group_by(Task.status)).all())
            peak=max(peak,counts.get('running',0))
            if sum(counts.get(x,0) for x in ('succeeded','failed','cancelled','timeout'))==len(jobs):break
            if time.monotonic() >= probe_next:
                probe_start=time.monotonic()
                try:
                    checked(sessions[probe_index % len(sessions)][0].get('/api/v1/tasks',timeout=5))
                    api_latency.append(time.monotonic()-probe_start)
                except Exception as exc:
                    api_errors.append({'type':type(exc).__name__,'message':str(exc)[:300]})
                probe_index+=1;probe_next=time.monotonic()+2
            time.sleep(.1)
        elapsed=time.monotonic()-start
        errors=[]; durations={}; integrity=0; retries=[];quota=[];validation=[]; waits=[]; first={}; timeline=[]; lane_waits={}; lane_durations={}
        with SessionLocal() as db:
            for tid,owner,project,kind in jobs:
                task=db.get(Task,tid)
                attempts=db.scalars(select(TaskAttempt).where(TaskAttempt.task_id==tid)).all()
                if args.model_workers:
                    expected_lane='model' if TASK_TYPES[kind].gpu_initial else 'mechanical'
                    if any(f'-{expected_lane}-' not in a.worker_id for a in attempts):validation.append('wrong worker lane '+kind)
                if len(attempts)!=1: retries.append({'kind':kind,'attempts':len(attempts)})
                if task.status!='succeeded':
                    errors.append({'id':tid,'kind':kind,'status':task.status,'code':task.error_code,'message':task.error_message});continue
                result=db.get(TaskResult,tid).result
                assert task.owner_id==owner and task.project_id==project
                durations.setdefault(kind,[]).append((task.finished_at-task.started_at).total_seconds())
                waits.append((task.started_at-task.created_at).total_seconds())
                lane='model' if TASK_TYPES[kind].gpu_initial else 'mechanical'
                lane_waits.setdefault(lane,[]).append((task.started_at-task.created_at).total_seconds())
                lane_durations.setdefault(lane,[]).append((task.finished_at-task.started_at).total_seconds())
                first[owner]=min(first.get(owner,task.started_at),task.started_at)
                timeline.append({'id':tid,'kind':kind,'user':owner,'created':task.created_at.isoformat(),'started':task.started_at.isoformat(),'finished':task.finished_at.isoformat()})
                for fid in [result.get('file_id')]:
                    if fid:
                        output=db.get(ProjectFile,fid);assert output.owner_id==owner and output.project_id==project
                        if output.deleted_at is None:
                            path=RUN/'storage'/output.object_key
                            assert hashlib.sha256(path.read_bytes()).hexdigest()==output.sha256
                            integrity+=1
                if kind=='script.parse':
                    parsed=json.loads(path.read_text())
                    marker=next(s[6].parent.name for s in sessions if s[1]==owner)
                    assert marker in ''.join(line['text'] for line in parsed),kind
                if kind=='resources.cleanup': assert result['projects'][0]['deleted_count']==1,result
                if kind=='resources.package':
                    assert result['file_count']==1,result
                    archive=internal_path(db,owner,'exports',tid,'files.zip')
                    with zipfile.ZipFile(archive) as zipped:
                        assert len(zipped.namelist())==1
                        assert zipped.read(zipped.namelist()[0])==(RUN/'template.mp3').read_bytes()
                if kind=='resources.scan': assert result['snapshots'][0]['complete'] and not result['scan_errors'],result
                if kind=='voices.foundation': assert result['results'][0]['ok'],result
                if kind=='voices.clone': assert result['ok']==1 and not result['failed'],result
                if kind=='tts.batch': assert result['completed']==2 and not result['failed'],result
                if kind=='bgm.segment': assert len(result['blocks'])==1 and result['timeline'],result
                if kind=='bgm.match': assert result['matched']==1 and result['no_bgm']==0,result
                if kind=='tts.reset': assert result['removed']==['reset'],result
                if TASK_TYPES[kind].billable:
                    charges=db.scalars(select(QuotaTransaction).where(QuotaTransaction.task_id==tid, QuotaTransaction.kind=='consume')).all()
                    assert charges and all(c.char_count>0 for c in charges), (kind, charges)
                    expected_resource='LLM' if kind in ('script.parse','voices.foundation','bgm.segment','music.suggest_tags') else 'TTS'
                    assert all(c.resource_type==expected_resource for c in charges),kind
                    assert sum(c.char_count for c in charges)==sum(c.amount for c in charges),kind
                if kind=='tts.preview_render': assert result['completed']==1 and not result['failed'],result
            for s in sessions:
                account=db.get(UserQuotaAccount,s[1]);quota.append({'user':s[1],'available':account.available_units,'reserved':account.reserved_units,'frozen':account.frozen_units,'consumed':account.consumed_units})
                if account.reserved_units or account.frozen_units:validation.append('quota hold leak '+s[1])
                if (s[7]/'00_temp/stale.bin').exists() or not (s[7]/'00_temp/fresh.bin').exists():validation.append('cleanup wrong '+s[1])
                tracks=music.load_index()['tracks']
                if tracks[s[8]]['tags']['scene']!=['测试场景']:validation.append('shared music update lost '+s[8])
                voice=json.loads((s[6]/'04_voice_profiles/voice_config.json').read_text())
                if voice['基础角色']['description']!='温暖清晰的成年男声' or voice['克隆角色']['type']!='clone':validation.append('voice update lost '+s[1])
                ledger=db.scalar(select(func.coalesce(func.sum(QuotaTransaction.amount),0)).where(QuotaTransaction.user_id==s[1],QuotaTransaction.kind=='consume'))
                if ledger!=account.consumed_units or account.available_units+account.consumed_units!=10000:validation.append('quota ledger mismatch '+s[1])
                with wave.open(str(s[6]/voice['克隆角色']['ref_audio'])) as clone:
                    assert clone.getnframes()>0
                if (s[6]/'05_audio_chunk/reset').exists():validation.append('reset package remains '+s[1])
            permits=db.scalar(select(func.count()).select_from(GPURequest))
            if permits:validation.append('GPU admission request leak '+str(permits))
            held=db.scalar(select(func.count()).select_from(QuotaHold).where(QuotaHold.status=='held'))
        events=sorted((stamp,delta) for t in timeline for stamp,delta in ((t['started'],1),(t['finished'],-1)))
        active=peak_intervals=0
        for _,delta in events:
            active+=delta
            peak_intervals=max(peak_intervals,active)
        llm_report=json.loads((RUN/'llm.json').read_text())
        llm_counts=dict(Counter(c['kind'] for c in llm_report['calls'][llm_before:]))
        if llm_counts!={k:args.users for k in ('script.parse','voices.foundation','bgm.segment','music.suggest_tags')}:validation.append('unexpected model call count '+str(llm_counts))
        tts_calls=[json.loads(p.read_text()) for p in (RUN/'tts-calls').glob('*.json')]
        stage_tts=[c for c in tts_calls if c['stage']==stage]
        modes=dict(Counter(c['mode'] for c in stage_tts))
        if modes!={'batch':args.users*2,'design-batch':args.users,'merge':args.users}:validation.append('unexpected TTS invocation count '+str(modes))
        if api_errors:validation.append('API probe failed')
        exits=[p.returncode for p in workers if p.poll() is not None]
        for p in workers:stop(p)
        metrics={'users':args.users,'workers':worker_count+args.model_workers,'mechanical_workers':worker_count if args.model_workers else None,'model_workers':args.model_workers,'tasks':len(jobs),'status_counts':counts,'elapsed_seconds':round(elapsed,3),
                 'tasks_per_second':round(counts.get('succeeded',0)/elapsed,3),'submit_seconds':round(submit_seconds,3),
                 'submit_p95_seconds':percentile(latency,.95),'queue_wait_p95_seconds':percentile(waits,.95),
                 'all_users_first_start_spread_seconds':round((max(first.values())-min(first.values())).total_seconds(),3) if first else None,
                 'by_lane':{lane:{'tasks':len(values),'queue_wait_p95_seconds':percentile(values,.95),'execute_p95_seconds':percentile(lane_durations[lane],.95)} for lane,values in lane_waits.items()},
                 'llm_calls_by_kind':llm_counts,'peak_running_sampled_after_submission':peak,'peak_running_from_task_intervals':peak_intervals,'database_connections':dict(monitor_stats),
                 'errors':errors,'retries':retries,'unexpected_worker_exits':exits,'integrity_checks':integrity,'held_quota_rows':held,'remaining_gpu_requests':permits,
                 'quota':quota,'validation_errors':validation,'by_kind':{k:{'count':len(v),'p50':percentile(v,.5),'p95':percentile(v,.95)} for k,v in durations.items()},
                 'fake_model_delay':args.model_delay,'llm_delay_seconds':args.llm_delay,
                 'tts_delay_range_seconds':[args.tts_min_delay,args.tts_max_delay],'merge_delay_seconds':args.merge_delay,
                 'lease_renewed_attempts_observed':len(renewed),'api_probe_count':len(api_latency),'api_probe_p95_seconds':percentile(api_latency,.95),'api_probe_errors':api_errors,
                 'tts_calls_by_mode':modes,'tts_calls':stage_tts,'llm':json.loads((RUN/'llm.json').read_text()) if (RUN/'llm.json').exists() else None}
        (RUN/f'timeline-{stage}.json').write_text(json.dumps(timeline,indent=2))
        report['stages'].append(metrics);(RUN/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
        print(json.dumps(metrics,ensure_ascii=False),flush=True)
        for s in sessions:s[0].close()
        assert not errors and not retries and not exits and not held and not validation,metrics
    engine.dispose();lock_engine.dispose()
except Exception as exc:
    report['fatal']={'type':type(exc).__name__,'message':str(exc)[:2500]}
    (RUN/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));raise
finally:
    monitor_stop.set()
    if monitor_thread:monitor_thread.join(timeout=2)
    for p in reversed(processes):stop(p)
    for f in handles:f.close()
    with psycopg.connect(**CONN,autocommit=True) as db:
        db.execute(sql.SQL('DROP DATABASE IF EXISTS {} WITH (FORCE)').format(sql.Identifier(DB)))
    print('REPORT '+str(RUN/'report.json'),flush=True)
