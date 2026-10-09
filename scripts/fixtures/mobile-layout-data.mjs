// Isolated API responses for browser layout checks; never contact the backend.
import { readFileSync } from 'node:fs'
import { runInNewContext } from 'node:vm'
import ts from 'typescript'

const adminData = {}
const adminSource = readFileSync(new URL('./admin-layout-data.ts', import.meta.url), 'utf8')
runInNewContext(ts.transpileModule(adminSource, {
  compilerOptions: { module: ts.ModuleKind.CommonJS },
}).outputText, { exports: adminData, module: { exports: adminData } })

const config = JSON.parse(readFileSync(new URL('../../setting.json', import.meta.url), 'utf8'));
config.paths.working_dir = '/fixture/project';
config.llm.api_key = 'fixture';
const name = '第001章_移动端长章节名称与边界检查';
const project = {id:'demo',name:'移动端展示检查项目名称',directory_key:'demo',created_at:'2026-10-06T00:00:00Z',updated_at:'2026-10-06T00:00:00Z'};
const chapters = Array.from({length:24},(_,i)=>({key:String(i),seq:i+1,num:i+1,numStr:String(i+1),title:'长章节标题用于测试移动端展示效果',chars:3000,orig_num:i+1,orig_numStr:String(i+1),final_num:i+1,actions:[],reasons:[],confidence:'high',pending:false,adjusted:false,matters:[]}));
const version = {flow_id:'flow',task_id:'task',mode:'smart',version_status:'current',created_at:project.created_at,total_chars:72000,chapters,files:chapters.map((c,i)=>({name:`第${String(i+1).padStart(3,'0')}章_长标题.txt`,chars:3000})),matters:[],report:{actions:[],warnings:[],removed:[]},baseline_chars:72000,original_count:24,length_target:3000,review_marks:[]};
const resourceEntry = {
  id: 'file', project_id: 'demo', project_name: project.name,
  name: name + '.txt', relative_path: '02_split_text/' + name + '.txt',
  module: '02_split_text', module_label: '章节文本', kind: 'file',
  size_bytes: 3000, modified_at: project.updated_at, extension: 'txt',
  preview_kind: 'text', snapshot_id: 'snapshot', can_preview: true,
  can_download: false, can_package: false, resource_role: 'production',
  delivery_label: null, completed_at: null, delivery_version: null, download_reason: null,
}

export function layoutResponse(path, role) {
  if(path.startsWith('/api/v1/admin/')) {
    const result = adminData.adminLayoutResponse(path)
    if (result !== undefined) return result
  }
  if(path==='/api/auth/me' && role === 'guest') return {user:null};
  if(path==='/api/auth/me') return {user:{id:role,role,username:'mobile-test',display_name:'手机测试账户',email:'test@example.invalid',is_active:true}};
  if(path==='/api/config') return config;
  if(path==='/api/v1/projects/active')return {set:true,exists:true,is_default:false,project_id:'demo',project_name:project.name,path:'',dirs:{}};
  const taskCenterCounts = {task_count:24,succeeded_count:24,active_count:0,pausable_count:0,resumable_count:0};
  if(path==='/api/v1/tasks/center/summary')return {items:[{category:'script',task_count:24,project_count:1,active_count:0}]};
  if(path==='/api/v1/tasks/center/groups')return {items:[{...taskCenterCounts,project_id:'demo',project_name:project.name,latest:1791244800,latest_status:'succeeded'}],total:1,page:1,page_size:5};
  if(path==='/api/v1/tasks/center/items')return {items:Array.from({length:24},(_,i)=>({id:`task-${i}`,project_id:'demo',project_name:project.name,task_type:'script.parse',label:`第${i+1}章 ${name}`,status:'succeeded',progress:1,current:'',error:'',created:1791244800,created_at:project.created_at})),counts:taskCenterCounts,total:24,page:1,page_size:50};
  if(path==='/api/v1/tasks/overview')return {statuses:[],failures:[],failure_count:0};
  if(path==='/api/v1/tasks/history')return {items:[{id:'task',project_id:'demo',project_name:project.name,task_type:'script.parse',label:name,status:'succeeded',progress:1,current:'',error:'',created:1791244800,created_at:project.created_at}],next_cursor:null};
  if(path==='/api/v1/projects')return [project];
  if(path==='/api/v1/projects/trash')return [{...project,deleted_at:project.created_at,expires_at:'2026-11-01T00:00:00Z'}];
  if(path.endsWith('/summary'))return {project_id:'demo',name:project.name,updated_at:project.updated_at,file_count:24,size_bytes:72000,split_volume_count:24,stage_keys:[],stage_completion:{},categories:[],recent_files:[],recent_outputs:[],cleanup_candidates:{count:0,size_bytes:0,older_than_days:7,blocked_by_active_tasks:false}};
  if(path.endsWith('/text-format/state'))return {flow:{id:'flow',source_file_id:'input',source_file_name:'长篇小说.txt',config_snapshot:{},whole_book:false,force_by_length:false,status:'ready',manifest_count:24},version,next_task:null,active_tasks:[]};
  if(path.endsWith('/script-parse/summary'))return {total:125,done_count:0,active_task_ids:[]};
  if(path.endsWith('/script-parse/state'))return {source:{mode:'version',version},text_format_busy:false,files:version.files.map(f=>({name:f.name,input:{name:f.name,sha256:'hash',size:3000},latest_task:null,result:null,result_status:null}))};
  if(path.startsWith('/api/tts/preview/chapter/'))return {name:name+'.json',package:name,lines:Array.from({length:12},(_,index)=>({index,speaker:'长角色名称',text:'用于检查手机端台词编辑区域的长文本。'.repeat(4),instruct:'平静',audio:'',audio_mtime_ns:null,duration:null,ok:false,reason:'',staged:null,start_offset:null})),chapter_audio:null,timeline_exists:false,downstream:{merged:false,mixed:false,timeline:false,segment_stale:false}};
  if(path==='/api/tts/batch-status'||path==='/api/tts/batch-list')return {files:[{name:name+'.json',total:20,completed:10,complete:false,speakers:3,ready:3,missing:[]}]};
  if(path==='/api/tts/merge-status'||path==='/api/tts/merge-list')return {packages:[{name,total:20,completed:10,remaining:10,complete:false}]};
  if(path==='/api/tts/voices')return {has_script:true,script_path:'',voice_config_path:'',speakers:Array.from({length:24},(_,i)=>({name:'长角色名称'+i,line_count:30,status:'pending',foundation_status:'none',clone_status:'none',type:'',alias_of:'',gender:'male',description:'角色描述',preview:'',candidates:[],selected_audio_id:null}))};
  if(path==='/api/tts/status')return {implemented:true,ready:true,message:''};
  if(path==='/api/bgm/chapters')return {chapters:[{stem:name,narration_exists:true,mix_exists:false,assignment:null,music_missing:false,segment_analysis:null,timeline:null,segment_music_missing:false}],mode:'random'};
  if(path==='/api/music/library')return {version:1,tags:{scene:[],mood:[],emotion:[],custom:[]},tracks:{'长音乐名称测试.mp3':{duration:180,enabled:true,description:'长描述测试',tags:{scene:[],mood:[],emotion:[],custom:[]},added_at:project.created_at,folder:'',size_bytes:1000}},folders:{},suggestions:{}};
  if(path==='/api/v1/quota')return {available_units:1000,reserved_units:0,frozen_units:0,consumed_units:200};
  if(path==='/api/v1/resources')return {projects:[{project_id:'demo',name:project.name,delivery_count:24,delivery_bytes:72000,production_audio_count:0,production_categories:[],snapshot:{snapshot_id:'snapshot',scanned_at:project.updated_at,complete:true,errors:[],file_count:24,size_bytes:72000,categories:[],audio_count:0,latest_modified_at:project.updated_at},scan_task_id:null,scan_status:null,scan_error:null,stale:false}],categories:[],storage:{project_bytes:0,project_complete:true,trash_bytes:0,trash_complete:true,export_bytes:0},exports:[]};
  if(path.endsWith('/files/file/preview'))return {file:resourceEntry,content:'移动端长文本预览。'.repeat(500),truncated:false,warning:null};
  if(path.endsWith('/entries'))return {items:[resourceEntry],total:1,size_bytes:0,page:1,page_size:20,snapshots:[],complete:true,incomplete_projects:[]};
  if(path.includes('/tasks')||path.endsWith('/transactions')||path.endsWith('/files'))return [];
 return null;
}
