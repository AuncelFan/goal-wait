// Real Codex scheduler + helper + tools, isolated home and local mock model.
// No account credentials or real model requests. Node 24+, Python 3.10+, codex.
import assert from 'node:assert/strict';
import {EventEmitter, once} from 'node:events';
import fs from 'node:fs';
import http from 'node:http';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {spawn} from 'node:child_process';
import {randomUUID} from 'node:crypto';

const repo = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const helper = path.join(repo, 'skills/goal-wait/scripts/goal_wait.py');
const root = fs.mkdtempSync(path.join(os.tmpdir(), 'goal-wait-integration-'));
const codexHome = path.join(root, 'codex');
const stateRoot = path.join(root, 'state');
fs.mkdirSync(codexHome);
fs.mkdirSync(stateRoot, {mode:0o700});
const bin = process.env.GOAL_WAIT_CODEX_BIN || 'codex';
const signals = new EventEmitter();
const pending = new Map();
const events = [];
const helpers = [];
const children = new Set();
let current, ws, server, serverErrors = '', sequence = 0;
const summaries = [];

function waitFor(predicate, label, timeout=12000) {
  return new Promise((resolve,reject) => {
    const timer = setTimeout(() => {
      signals.off('change', check);
      reject(new Error(`Timeout: ${label}; helper state=${current?.jobDir && fs.existsSync(path.join(current.jobDir,'state.json')) ? fs.readFileSync(path.join(current.jobDir,'state.json'),'utf8') : 'n/a'}`));
    },timeout);
    function check() {
      try {
        if (current?.error) throw current.error;
        const result = predicate();
        if (result) {clearTimeout(timer);signals.off('change',check);resolve(result);}
      } catch (error) {clearTimeout(timer);signals.off('change',check);reject(error);}
    }
    signals.on('change',check);
    check();
  });
}

function rpc(method,params) {
  return new Promise((resolve,reject) => {
    const id=++sequence;
    const timer=setTimeout(()=>{pending.delete(id);reject(new Error(`RPC timeout: ${method}`));},10000);
    pending.set(id,msg=>{clearTimeout(timer);msg.error ? reject(new Error(JSON.stringify(msg.error))) : resolve(msg.result);});
    ws.send(JSON.stringify({id,method,params}));
  });
}

async function run(args) {
  const proc=spawn('python3',[helper,...args],{env,stdio:['ignore','pipe','pipe']});
  let stdout='',stderr='';
  proc.stdout.on('data',chunk=>{stdout+=chunk;});
  proc.stderr.on('data',chunk=>{stderr+=chunk;});
  const [code]=await once(proc,'exit');
  assert.equal(code,0,`helper failed: ${stdout} ${stderr}`);
  return JSON.parse(stdout);
}

function response(res,item,index) {
  const payload={id:`resp_${index}`,object:'response',created_at:1,status:'completed',output:[item],usage:{input_tokens:1,output_tokens:1,total_tokens:2}};
  res.writeHead(200,{'Content-Type':'text/event-stream'});
  for (const event of [
    {type:'response.created',response:{...payload,status:'in_progress',output:[]}},
    {type:'response.output_item.added',output_index:0,item:{...item,status:'in_progress'}},
    {type:'response.output_item.done',output_index:0,item},
    {type:'response.completed',response:payload},
  ]) res.write(`event: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`);
  res.end();
}

function message(text,index) {
  return {type:'message',id:`msg_${index}`,role:'assistant',status:'completed',content:[{type:'output_text',text,annotations:[]}]};
}
function call(name,args,index) {
  return {type:'function_call',id:`fc_${index}`,call_id:`call_${index}`,name,arguments:JSON.stringify(args),status:'completed'};
}

const mock=http.createServer(async(req,res)=>{
  try {
    let body='';for await(const part of req) body+=part;
    const request=JSON.parse(body);
    const fixture=current;
    const index=++fixture.requests;
    signals.emit('change');
    if(index===1) {
      await fixture.releaseInitial;
      response(res,message('SETUP_DONE',index),index);
    } else if(index===2) {
      // Match real helper notification, not merely a hand-coded probe message.
      assert(JSON.stringify(request.input).includes('GOAL_WAIT_DONE'));
      response(res,message('GOAL_WAIT_ACK',index),index);
    } else if(!fs.existsSync(fixture.artifact)) {
      if(fixture.execRequested) {
        const output=request.input.filter(x=>x.type==='function_call_output').at(-1)?.output;
        fixture.error=new Error(`Fixture command did not create the artifact: ${JSON.stringify(output)}`);
        response(res,message('Fixture command failed; inspect its tool output.',index),index);
        signals.emit('change');
        return;
      }
      assert(request.tools.some(x=>x.name==='exec_command'));
      fixture.execRequested=true;
      const py=`import json,pathlib; r=json.loads(pathlib.Path(${JSON.stringify(path.join(fixture.jobDir,'result.json'))}).read_text()); pathlib.Path('resumed.txt').write_text(json.dumps(r['exit_code'])+'\\n')`;
      const quoted="'"+py.replaceAll("'","'\\''")+"'";
      response(res,call('exec_command',{cmd:`python3 -c ${quoted}`,workdir:fixture.directory,login:false,max_output_tokens:1000},index),index);
    } else if(!fixture.completionRequested) {
      fixture.completionRequested=true;
      response(res,call('update_goal',{status:'complete'},index),index);
    } else response(res,message('Verified result and completed original goal.',index),index);
  } catch(error) {
    console.error(error);
    res.writeHead(500);res.end(String(error));
  }
});

mock.listen(0,'127.0.0.1');await once(mock,'listening');
fs.writeFileSync(path.join(codexHome,'config.toml'),`model="goal-wait-fixture"\nmodel_provider="fixture"\n[model_providers.fixture]\nname="Local fixture"\nbase_url="http://127.0.0.1:${mock.address().port}/v1"\nwire_api="responses"\nrequires_openai_auth=false\n`);
const env={...process.env,CODEX_HOME:codexHome};
delete env.OPENAI_API_KEY;delete env.CODEX_API_KEY;
const reservation=net.createServer();reservation.listen(0,'127.0.0.1');await once(reservation,'listening');
const port=reservation.address().port;await new Promise(resolve=>reservation.close(resolve));
const endpoint=`ws://127.0.0.1:${port}`;

async function scenario(mode) {
  const directory=path.join(root,mode);fs.mkdirSync(directory);
  const fixture={mode,directory,artifact:path.join(directory,'resumed.txt'),requests:0,jobId:randomUUID()};
  fixture.jobDir=path.join(stateRoot,fixture.jobId);
  fixture.releaseInitial=new Promise(resolve=>{fixture.release=resolve;});
  current=fixture;
  const offset=events.length;
  const started=await rpc('thread/start',{cwd:directory,approvalPolicy:'never',sandbox:'workspace-write'});
  const threadId=started.thread.id;
  const original=(await rpc('thread/goal/set',{threadId,objective:'Test fixture: after the external result, verify its exit code by writing it to resumed.txt, then complete this goal.',status:'active',origin:'user'})).goal;
  await waitFor(()=>fixture.requests===1,'initial turn held');
  const watcher=fs.watch(directory,()=>signals.emit('change'));
  const args=['start','--endpoint',endpoint,'--thread',threadId,'--state-dir',stateRoot,'--job-id',fixture.jobId,
    '--estimated-minutes','21','--authorize-auto-pause-resume','--continuation','Read result, verify exit code, continue original goal.','--cwd',directory];
  const fileMode=mode==='watch_preexisting' || mode==='watch_rename' || mode==='watch_invalid';
  const done=path.join(directory,'done.json');
  if(fileMode) {
    args.push('--completion-file',done);
    if(mode==='watch_preexisting') fs.writeFileSync(done,JSON.stringify({job_id:fixture.jobId,exit_code:0}));
  } else if(mode==='fast') args.push('--','python3','-c','pass');
  else if(mode==='spawn_error') args.push('--','/nonexistent/goal-wait-test-command');
  else {
    const script="import os,pathlib,signal,sys; signal.signal(signal.SIGUSR1,lambda *_:sys.exit("+(mode==='failure'?7:0)+")); pathlib.Path('child.pid').write_text(str(os.getpid())); signal.pause()";
    args.push('--','python3','-c',script);
  }
  let ready;
  try {
    ready=await run(args);
    helpers.push(ready.helper_pid);
    assert.equal(ready.goal_status,'paused');
    const jobWatch=fs.watch(fixture.jobDir,()=>signals.emit('change'));
    try {
      if(!fileMode && !['fast','spawn_error'].includes(mode)) {
        await waitFor(()=>fs.existsSync(path.join(directory,'child.pid')),'command ready');
        fixture.childPid=Number(fs.readFileSync(path.join(directory,'child.pid'),'utf8'));
        children.add(fixture.childPid);
      }
      if(['fast','spawn_error','watch_preexisting'].includes(mode)) {
        await waitFor(()=>fs.existsSync(path.join(fixture.jobDir,'result.json')),'early completion');
        assert.equal(fixture.requests,1,'completion must wait for the original turn to end');
      }
      if(mode==='cancel' || mode==='terminate') await run(['cancel',fixture.jobId,'--state-dir',stateRoot,...(mode==='terminate'?['--terminate']:[])]);
      if(mode==='changed') await rpc('thread/goal/set',{threadId,objective:'A new user objective must not be resumed by the old helper',status:'paused',origin:'user'});
      if(mode==='manual_pause') await rpc('thread/goal/set',{threadId,status:'paused',origin:'user'});
      fixture.release();
      await waitFor(()=>events.slice(offset).some(e=>e.method==='turn/completed' && e.params.threadId===threadId),'initial turn end');
      if(mode==='disconnect') {
        process.kill(-server.pid,'SIGKILL');await once(server,'exit');server=null;
      }
      // A short negative observation window; no model-driven polling.
      if(fixture.childPid && mode!=='terminate') {
        await new Promise(resolve=>setTimeout(resolve,120));
        assert.equal(fixture.requests,1,'Model was polled while the job was pending');
        process.kill(fixture.childPid,'SIGUSR1');
      }
      if(fileMode && mode!=='watch_preexisting') {
        fs.writeFileSync(done+'.tmp',JSON.stringify({job_id:mode==='watch_invalid'?randomUUID():fixture.jobId,exit_code:0}));
        fs.renameSync(done+'.tmp',done);
      }
      await waitFor(()=>fs.existsSync(path.join(fixture.jobDir,'result.json')),'persisted result');
      const suppressed=['cancel','terminate','changed','manual_pause','disconnect'].includes(mode);
      if(suppressed) {
        await waitFor(()=>JSON.parse(fs.readFileSync(path.join(fixture.jobDir,'state.json'))).phase==='suppressed','suppressed wake');
        await new Promise(resolve=>setTimeout(resolve,120));
        assert.equal(fixture.requests,1,'Cancelled/changed/disconnected job woke the model');
        assert(!fs.existsSync(fixture.artifact));
      } else {
        await waitFor(()=>events.slice(offset).some(e=>e.method==='thread/goal/updated' && e.params.threadId===threadId && e.params.goal.status==='complete'),'automatic goal completion');
        await waitFor(()=>events.slice(offset).filter(e=>e.method==='turn/completed' && e.params.threadId===threadId).length===3,'three completed turns');
        const goal=(await rpc('thread/goal/get',{threadId})).goal;
        assert.equal(goal.objective,original.objective);
        assert.equal(goal.createdAt,original.createdAt);
        assert.equal(goal.tokenBudget,original.tokenBudget);
        assert.equal(goal.status,'complete');
        const expected=mode==='failure'?7:mode==='spawn_error'||mode==='watch_invalid'?null:0;
        assert.equal(fs.readFileSync(fixture.artifact,'utf8'),JSON.stringify(expected)+'\n');
        assert(fixture.completionRequested,'Completion must pass through the actual model tool path');
        const notices=events.slice(offset).filter(e=>e.method==='item/started' && e.params.item.type==='userMessage');
        assert.equal(notices.length,1,'One completion notification only');
      }
      const state=JSON.parse(fs.readFileSync(path.join(fixture.jobDir,'state.json')));
      const result=JSON.parse(fs.readFileSync(path.join(fixture.jobDir,'result.json')));
      const summary={mode,phase:state.phase,exitCode:result.exit_code,requests:fixture.requests,
        turns:events.slice(offset).filter(e=>e.method==='turn/started'&&e.params.threadId===threadId).length};
      if(mode==='terminate') assert.equal(result.exit_code,-15);
      if(mode==='spawn_error'||mode==='watch_invalid') assert(result.error);
      summaries.push(summary);console.log(JSON.stringify(summary));
      if(mode!=='disconnect') await rpc('thread/archive',{threadId});
    } finally {jobWatch.close();}
  } finally {watcher.close();}
}

const overall=setTimeout(()=>{console.error('Integration test exceeded 110s');cleanup();process.exit(1);},110000);
function cleanup() {
  for(const pid of children) {try{process.kill(pid,'SIGKILL');}catch{}}
  for(const pid of helpers) {try{process.kill(pid,'SIGTERM');}catch{}}
  ws?.close();
  if(server) {try{process.kill(-server.pid,'SIGKILL');}catch{}}
  mock.closeAllConnections();mock.close();
}
try {
  // The codex launcher may spawn a native child. Own and clean up the whole group.
  server=spawn(bin,['app-server','--listen',endpoint],{env,detached:true,stdio:['ignore','ignore','pipe']});
  server.stderr.on('data',x=>{serverErrors+=x;});
  await new Promise((resolve,reject)=>{
    let attempts=0;
    function connect(){const candidate=new WebSocket(endpoint);candidate.onopen=()=>{ws=candidate;resolve();};candidate.onerror=()=>{if(++attempts<100)setTimeout(connect,30);else reject(new Error('No app-server'));};}
    connect();
  });
  ws.addEventListener('message',event=>{
    const msg=JSON.parse(event.data);
    if(['thread/goal/updated','thread/goal/cleared','turn/started','turn/completed'].includes(msg.method))
      fs.appendFileSync(path.join(root,'lifecycle.jsonl'),JSON.stringify(msg)+'\n');
    if('id' in msg && pending.has(msg.id)){pending.get(msg.id)(msg);pending.delete(msg.id);}
    else {events.push(msg);signals.emit('change');}
  });
  const info=await rpc('initialize',{clientInfo:{name:'goal_wait_integration',version:'0.1.0'},capabilities:{experimentalApi:true}});
  ws.send(JSON.stringify({method:'initialized',params:{}}));
  for(const mode of ['success','failure','fast','spawn_error','watch_preexisting','watch_rename','watch_invalid','cancel','terminate','changed','manual_pause','disconnect']) await scenario(mode);
  console.log(JSON.stringify({validated:true,server:info.userAgent,scenarios:summaries.length}));
} catch(error) {
  console.error(error.stack);console.error(serverErrors.slice(-2500));
  console.error(`Fixture retained for diagnosis: ${root}`);
  process.exitCode=1;
} finally {
  clearTimeout(overall);cleanup();
  setTimeout(()=>process.exit(process.exitCode??0),100);
}
