// Local protocol feasibility probe. Uses a mock model endpoint and isolated CODEX_HOME.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import http from 'node:http';
import net from 'node:net';
import {spawn} from 'node:child_process';
import {once} from 'node:events';

const root = fs.mkdtempSync(path.join(os.tmpdir(), 'goal-wait-probe-'));
const codexBinary = process.env.GOAL_WAIT_CODEX_BIN || 'codex';
const goalWaitCodexHome = path.join(root, 'codex');
fs.mkdirSync(goalWaitCodexHome);
const observations = [];
let modelRequests = 0;
const mock = http.createServer((req, res) => {
  req.resume();
  modelRequests++;
  const message = {type:'message', id:`msg_${modelRequests}`, role:'assistant', status:'completed', content:[{type:'output_text', text:'Local protocol probe finished.', annotations:[]}]};
  const response = {id:`resp_${modelRequests}`, object:'response', created_at:1, status:'completed', output:[message], usage:{input_tokens:1, output_tokens:1, total_tokens:2}};
  res.writeHead(200, {'Content-Type':'text/event-stream'});
  for (const event of [
    {type:'response.created', response:{...response,status:'in_progress',output:[]}},
    {type:'response.output_item.added', output_index:0, item:{...message,status:'in_progress',content:[]}},
    {type:'response.content_part.added', item_id:message.id, output_index:0, content_index:0, part:{type:'output_text',text:'',annotations:[]}},
    {type:'response.output_text.delta', item_id:message.id, output_index:0, content_index:0, delta:message.content[0].text},
    {type:'response.output_item.done', output_index:0, item:message},
    {type:'response.completed', response},
  ]) res.write(`event: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`);
  res.end();
});
mock.listen(0, '127.0.0.1');
await once(mock, 'listening');
fs.writeFileSync(path.join(goalWaitCodexHome, 'config.toml'), `model = "goal-wait-mock"\nmodel_provider = "goal_wait_mock"\n[model_providers.goal_wait_mock]\nname = "Local protocol fixture"\nbase_url = "http://127.0.0.1:${mock.address().port}/v1"\nwire_api = "responses"\nrequires_openai_auth = false\n`);
const env = {...process.env, CODEX_HOME:goalWaitCodexHome};
// No account credentials are copied to the isolated Codex home.
delete env.OPENAI_API_KEY;
delete env.CODEX_API_KEY;
const reservation = net.createServer();
reservation.listen(0, '127.0.0.1');
await once(reservation, 'listening');
const port = reservation.address().port;
await new Promise(resolve => reservation.close(resolve));
const endpoint = `ws://127.0.0.1:${port}`;
let server = spawn(codexBinary, ['app-server','--listen',endpoint], {env, detached:true, stdio:['ignore','ignore','pipe']});
let errors = '';
server.stderr.on('data', d => { errors += d; });
let ws;
const cleanup = () => {ws?.close();try{process.kill(-server.pid,'SIGKILL');}catch{}mock.closeAllConnections();mock.close();};
const overall = setTimeout(() => {console.error('probe timed out');cleanup();process.exit(1);}, 45000);
try {
  await new Promise((resolve,reject) => {
    let attempts=0;
    const connect=()=>{
      const candidate=new WebSocket(endpoint);
      candidate.onopen=()=>{ws=candidate;resolve();};
      candidate.onerror=()=>{if(++attempts<100)setTimeout(connect,30);else reject(new Error('local server did not open'));};
    };
    connect();
  });
  const pending = new Map();
  const events = [];
  const onMessage = event => {
    let msg;try{msg=JSON.parse(event.data);}catch{return;}
    fs.appendFileSync(path.join(root,'wire.jsonl'),JSON.stringify(msg)+'\n');
    if ('id' in msg && pending.has(msg.id)) {pending.get(msg.id)(msg);pending.delete(msg.id);}
    else events.push(msg);
  };
  ws.addEventListener('message', onMessage);
  let seq = 0;
  const rpc = (method, params) => new Promise((resolve,reject) => {
    const id=++seq;
    const timer=setTimeout(()=>{pending.delete(id);reject(new Error(`RPC timed out: ${method}`));},10000);
    pending.set(id, msg=>{clearTimeout(timer);resolve(msg);});
    ws.send(JSON.stringify({id,method,params}));
  });
  const record = (name, response) => {
    const out={name,...response};observations.push(out);console.log(JSON.stringify(out));return response;
  };
  record('initialize', await rpc('initialize',{clientInfo:{name:'goal_wait_research_probe',version:'0.1.0'},capabilities:{experimentalApi:true}}));
  ws.send(JSON.stringify({method:'initialized',params:{}}));
  const created=await rpc('thread/start',{cwd:root,approvalPolicy:'never',sandbox:'read-only'});
  if(created.error)throw new Error(JSON.stringify(created.error));
  const threadId=created.result.thread.id;
  record('isolated_thread_created',{threadId});
  const goal=(params)=>rpc('thread/goal/set',{threadId,...params});
  record('set_active_as_fixture_user', await goal({objective:'Local protocol fixture: no real work',status:'active',origin:'user'}));
  record('automatic_pause', await goal({status:'paused',origin:'automatic'}));
  record('missing_origin_pause', await goal({status:'paused'}));
  record('fixture_user_pause', await goal({status:'paused',origin:'user'}));
  record('automatic_resume', await goal({status:'active',origin:'automatic'}));
  record('reset_paused_before_queue', await goal({status:'paused',origin:'user'}));
  await new Promise(resolve=>setTimeout(resolve,250));
  const beforeIdle=modelRequests;
  await new Promise(resolve=>setTimeout(resolve,500));
  record('paused_idle_interval',{milliseconds:500,modelRequestsBefore:beforeIdle,modelRequestsAfter:modelRequests});
  const jobStartRequests=modelRequests;
  const completion=await new Promise((resolve,reject)=>{
    const watcher=fs.watch(root,(event,filename)=>{
      if(filename==='job-result.json' && fs.existsSync(path.join(root,filename))) {
        watcher.close();resolve({event,filename,result:JSON.parse(fs.readFileSync(path.join(root,filename),'utf8'))});
      }
    });
    const job=spawn(process.execPath,['-e',"const fs=require('node:fs'),path=require('node:path');setTimeout(()=>{const p=process.argv[1];fs.writeFileSync(path.join(p,'job-result.tmp'),JSON.stringify({exitCode:0}));fs.renameSync(path.join(p,'job-result.tmp'),path.join(p,'job-result.json'));},500);",root],{stdio:'ignore'});
    job.once('error',err=>{watcher.close();reject(err);});
  });
  record('job_completion_event',{...completion,modelRequestsBefore:jobStartRequests,modelRequestsAtEvent:modelRequests});
  const queueStart=events.length;
  const queued = spawn('codex',['queue','--remote',endpoint,'--thread',threadId,'--message','Local protocol fixture completion event.'],{env,stdio:['ignore','pipe','pipe']});
  let queueOut='',queueErr='';
  queued.stdout.on('data',d=>{queueOut+=d;});queued.stderr.on('data',d=>{queueErr+=d;});
  const [queueCode]=await once(queued,'exit');
  record('codex_queue_result',{exitCode:queueCode,stdout:queueOut.trim(),stderr:queueErr.trim()});
  // A short observation interval in the local test; no agent/model polling loop.
  await new Promise(resolve=>setTimeout(resolve,700));
  record('after_queue',{goal:await rpc('thread/goal/get',{threadId}),queue:await rpc('thread/queue/list',{threadId}),events:events.slice(queueStart).filter(x=>/turn\/(started|completed)|thread\/goal|queue/.test(x.method)),modelRequests});
  const directStart=events.length;
  record('direct_queue_add',await rpc('thread/queue/add',{threadId,clientUserMessageId:'goal-wait-probe-direct',input:[{type:'text',text:'Second local fixture event.'}]}));
  await new Promise(resolve=>setTimeout(resolve,300));
  record('after_direct_add',{queue:await rpc('thread/queue/list',{threadId}),turnsStarted:events.slice(directStart).filter(x=>x.method==='turn/started').length,modelRequests});
  record('start_direct_submission',await rpc('thread/queue/start',{threadId}));
  await new Promise(resolve=>setTimeout(resolve,300));
  record('stop_fixture_continuations',await goal({status:'paused',origin:'automatic'}));
  const get = name => observations.find(x => x.name === name);
  assert.equal(get('automatic_pause').result.goal.status, 'paused');
  assert.equal(get('automatic_resume').result.goal.status, 'active');
  assert.equal(get('paused_idle_interval').modelRequestsBefore, get('paused_idle_interval').modelRequestsAfter);
  assert.equal(get('job_completion_event').modelRequestsBefore, get('job_completion_event').modelRequestsAtEvent);
  assert.equal(get('codex_queue_result').exitCode, 0);
  assert.equal(get('after_queue').goal.result.goal.status, 'paused');
  assert.equal(get('after_queue').events.filter(e => e.method === 'turn/started').length, 1);
  assert.equal(get('after_queue').modelRequests, get('job_completion_event').modelRequestsAtEvent + 1);
  const output = process.env.GOAL_WAIT_OUTPUT || path.join(root,'results.json');
  fs.writeFileSync(output,JSON.stringify({codexBinary,root,observations,modelRequests},null,2));
  console.log(JSON.stringify({validated:true,output}));
} catch (err) {console.error(String(err));console.error(errors.slice(-4000));process.exitCode=1;}
finally {clearTimeout(overall);cleanup();setTimeout(()=>process.exit(process.exitCode??0),100);}
