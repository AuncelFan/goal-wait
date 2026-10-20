// Verify notification delivery and goal continuation with a local mock model.
// Each scenario uses a separate fixture thread in an isolated Codex home.
import assert from 'node:assert/strict';
import {EventEmitter, once} from 'node:events';
import fs from 'node:fs';
import http from 'node:http';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import {spawn} from 'node:child_process';

const root = fs.mkdtempSync(path.join(os.tmpdir(), 'goal-resume-probe-'));
const codexHome = path.join(root, 'codex');
fs.mkdirSync(codexHome);
const codexBinary = process.env.GOAL_WAIT_CODEX_BIN || 'codex';
const notices = new EventEmitter();
const events = [];
const scenarios = [];
let current;
let ws;
let server;
let serverErrors = '';
let modelRequests = 0;
let sequence = 0;
const pending = new Map();
const overall = setTimeout(() => {
  console.error('Goal resume probe exceeded 55 seconds');
  cleanup();
  process.exit(1);
}, 55000);

function cleanup() {
  ws?.close();
  if (server) {try {process.kill(-server.pid,'SIGKILL');} catch {}}
  mock.closeAllConnections();
  mock.close();
}

function message(text, index) {
  return {type:'message', id:`msg_${index}`, role:'assistant', status:'completed', content:[{type:'output_text',text,annotations:[]}]};
}

function functionCall(name, args, index) {
  return {type:'function_call', id:`fc_${index}`, call_id:`call_${index}`, name, arguments:JSON.stringify(args), status:'completed'};
}

function respond(res, item, index) {
  const response = {id:`resp_${index}`, object:'response', created_at:1, status:'completed', output:[item], usage:{input_tokens:1,output_tokens:1,total_tokens:2}};
  res.writeHead(200, {'Content-Type':'text/event-stream'});
  const output = [
    {type:'response.created',response:{...response,status:'in_progress',output:[]}},
    {type:'response.output_item.added',output_index:0,item:{...item,status:'in_progress'}},
    {type:'response.output_item.done',output_index:0,item},
    {type:'response.completed',response},
  ];
  for (const event of output) res.write(`event: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`);
  res.end();
}

const mock = http.createServer(async (req,res) => {
  try {
    let body = '';
    for await (const chunk of req) body += chunk;
    const input = JSON.parse(body);
    const scenario = current;
    const index = ++modelRequests;
    const requestNumber = ++scenario.modelRequests;
    scenario.requests.push({index,requestNumber,input:input.input});
    notices.emit('change');
    if (requestNumber === 1) {
      if (scenario.mode === 'resume_during_notification') await scenario.releaseNotification;
      respond(res,message('ACK_ONLY',index),index);
      return;
    }
    if (!fs.existsSync(scenario.artifact)) {
      assert(input.tools.some(t => t.name === 'exec_command'));
      respond(res,functionCall('exec_command',{
        cmd:"printf 'PASS\\n' > resumed-goal.txt",
        workdir:scenario.directory,
        login:false,
        max_output_tokens:1000,
      },index),index);
    } else if (!scenario.completionRequested) {
      assert.equal(fs.readFileSync(scenario.artifact,'utf8'),'PASS\n');
      assert(input.tools.some(t => t.name === 'update_goal'));
      scenario.completionRequested = true;
      respond(res,functionCall('update_goal',{status:'complete'},index),index);
    } else {
      respond(res,message('Goal complete. Artifact verified.',index),index);
    }
  } catch (error) {
    current.error = String(error);
    res.writeHead(500);
    res.end(String(error));
    notices.emit('change');
  }
});

function waitFor(predicate, label, timeout = 15000) {
  return new Promise((resolve,reject) => {
    const timer = setTimeout(() => {
      notices.off('change',check);
      reject(new Error(`Timed out waiting for ${label}; fixture error=${current?.error || 'none'}`));
    },timeout);
    function check() {
      const result = predicate();
      if (result) {
        clearTimeout(timer);
        notices.off('change',check);
        resolve(result);
      }
    }
    notices.on('change',check);
    check();
  });
}

function rpc(method,params) {
  return new Promise((resolve,reject) => {
    const id = ++sequence;
    const timer = setTimeout(() => {pending.delete(id);reject(new Error(`RPC timed out: ${method}`));},10000);
    pending.set(id,msg => {
      clearTimeout(timer);
      if (msg.error) reject(new Error(`${method}: ${JSON.stringify(msg.error)}`));
      else resolve(msg.result);
    });
    ws.send(JSON.stringify({id,method,params}));
  });
}

async function queue(threadId,text) {
  const proc = spawn(codexBinary,['queue','--remote',endpoint,'--thread',threadId,'--message',text],{env,stdio:['ignore','pipe','pipe']});
  let stdout='',stderr='';
  proc.stdout.on('data',data => {stdout+=data;});
  proc.stderr.on('data',data => {stderr+=data;});
  const [exitCode] = await once(proc,'exit');
  assert.equal(exitCode,0,stderr);
  return stdout.trim();
}

async function completionFile(scenario) {
  const requestsBefore = modelRequests;
  const notice = await new Promise((resolve,reject) => {
    const watcher = fs.watch(scenario.directory,(event,filename) => {
      if (filename === 'job-result.json' && fs.existsSync(path.join(scenario.directory,filename))) {
        watcher.close();
        resolve({event,filename,result:JSON.parse(fs.readFileSync(path.join(scenario.directory,filename),'utf8'))});
      }
    });
    const worker = spawn(process.execPath,['-e',"const fs=require('node:fs'),path=require('node:path');setTimeout(()=>{const dir=process.argv[1];fs.writeFileSync(path.join(dir,'result.tmp'),JSON.stringify({exitCode:0}));fs.renameSync(path.join(dir,'result.tmp'),path.join(dir,'job-result.json'));},300)",scenario.directory],{stdio:'ignore'});
    worker.once('error',error => {watcher.close();reject(error);});
  });
  assert.equal(modelRequests,requestsBefore,'Model ran during the paused external job');
  return {...notice,modelRequestsWhileWaiting:modelRequests-requestsBefore};
}

async function scenario(mode) {
  const directory = path.join(root,mode);
  fs.mkdirSync(directory);
  const fixture = {mode,directory,artifact:path.join(directory,'resumed-goal.txt'),modelRequests:0,requests:[],completionRequested:false};
  let release;
  fixture.releaseNotification = new Promise(resolve => {release=resolve;});
  current = fixture;
  const started = await rpc('thread/start',{cwd:directory,approvalPolicy:'never',sandbox:'workspace-write'});
  const threadId = started.thread.id;
  fixture.threadId = threadId;
  const objective = 'Integration fixture: after job-result.json is ready, write PASS followed by a newline to resumed-goal.txt in the current directory, verify it, then mark this goal complete.';
  const original = (await rpc('thread/goal/set',{threadId,objective,status:'paused',origin:'user'})).goal;
  const eventOffset = events.length;
  const completedFile = await completionFile(fixture);
  const notification = `GW_JOB_DONE ${mode}: job-result.json is ready. Acknowledge with ACK_ONLY and end this notification turn. The helper will explicitly resume the paused goal.`;
  const delivery = queue(threadId,notification);
  const notificationStarted = await waitFor(() => events.slice(eventOffset).find(e => e.method==='turn/started' && e.params.threadId===threadId),'notification turn');
  const notificationTurnId = notificationStarted.params.turn.id;
  let resumed;
  if (mode === 'resume_during_notification') {
    await waitFor(() => fixture.modelRequests===1,'held notification model request');
    resumed = (await rpc('thread/goal/set',{threadId,status:'active',origin:'automatic'})).goal;
    release();
  }
  await delivery;
  await waitFor(() => events.slice(eventOffset).find(e => e.method==='turn/completed' && e.params.turn.id===notificationTurnId),'notification turn completion');
  const afterNotification = (await rpc('thread/goal/get',{threadId})).goal;
  if (mode === 'queue_only') {
    // A bounded negative observation window, with no model-driven polling.
    await new Promise(resolve => setTimeout(resolve,500));
    assert.equal(fixture.modelRequests,1);
    assert.equal(afterNotification.status,'paused');
    assert(!fs.existsSync(fixture.artifact));
  } else {
    if (mode === 'resume_after_notification') {
      assert.equal(afterNotification.status,'paused');
      resumed = (await rpc('thread/goal/set',{threadId,status:'active',origin:'automatic'})).goal;
    }
    assert.equal(resumed.status,'active');
    assert.equal(resumed.objective,original.objective);
    assert.equal(resumed.createdAt,original.createdAt);
    await waitFor(() => events.slice(eventOffset).find(e => e.method==='thread/goal/updated' && e.params.threadId===threadId && e.params.goal.status==='complete'),'goal completion');
    await waitFor(() => events.slice(eventOffset).find(e => e.method==='turn/completed' && e.params.threadId===threadId && e.params.turn.id!==notificationTurnId),'automatic continuation completion');
    assert.equal(fs.readFileSync(fixture.artifact,'utf8'),'PASS\n');
  }
  const finalGoal = (await rpc('thread/goal/get',{threadId})).goal;
  const timeline = events.slice(eventOffset).filter(e => e.params?.threadId===threadId && ['turn/started','turn/completed','thread/goal/updated','item/started','item/completed'].includes(e.method)).map(e => ({
    method:e.method,
    turnId:e.params.turnId || e.params.turn?.id,
    goalStatus:e.params.goal?.status,
    itemType:e.params.item?.type,
    command:e.params.item?.command,
  }));
  const summary = {
    mode,threadId,completionFile:completedFile,
    initialGoalStatus:original.status,
    goalStatusAfterNotification:afterNotification.status,
    explicitResumeStatus:resumed?.status || null,
    finalGoalStatus:finalGoal.status,
    objectivePreserved:finalGoal.objective===original.objective,
    createdAtPreserved:finalGoal.createdAt===original.createdAt,
    modelRequests:fixture.modelRequests,
    turnsStarted:timeline.filter(e => e.method==='turn/started').length,
    artifact:fs.existsSync(fixture.artifact) ? fs.readFileSync(fixture.artifact,'utf8') : null,
    completionViaModelTool:fixture.completionRequested,
    timeline,
  };
  if (mode!=='queue_only') {
    assert.equal(finalGoal.status,'complete');
    assert(summary.turnsStarted>=2,'No distinct automatic goal continuation observed');
    assert(summary.completionViaModelTool);
  }
  scenarios.push(summary);
  console.log(JSON.stringify(summary));
}

mock.listen(0,'127.0.0.1');
await once(mock,'listening');
fs.writeFileSync(path.join(codexHome,'config.toml'),`model = "goal-wait-mock"\nmodel_provider = "goal_wait_mock"\n[model_providers.goal_wait_mock]\nname = "Local protocol fixture"\nbase_url = "http://127.0.0.1:${mock.address().port}/v1"\nwire_api = "responses"\nrequires_openai_auth = false\n`);
const env = {...process.env,CODEX_HOME:codexHome};
delete env.OPENAI_API_KEY;
delete env.CODEX_API_KEY;
const reservation = net.createServer();
reservation.listen(0,'127.0.0.1');
await once(reservation,'listening');
const port = reservation.address().port;
await new Promise(resolve => reservation.close(resolve));
const endpoint = `ws://127.0.0.1:${port}`;
try {
  server = spawn(codexBinary,['app-server','--listen',endpoint],{env,detached:true,stdio:['ignore','ignore','pipe']});
  server.stderr.on('data',data => {serverErrors+=data;});
  await new Promise((resolve,reject) => {
    let attempts=0;
    const connect=() => {
      const candidate = new WebSocket(endpoint);
      candidate.onopen=() => {ws=candidate;resolve();};
      candidate.onerror=() => {if(++attempts<100)setTimeout(connect,30);else reject(new Error('app-server did not open'));};
    };
    connect();
  });
  ws.addEventListener('message',event => {
    const msg = JSON.parse(event.data);
    fs.appendFileSync(path.join(root,'wire.jsonl'),JSON.stringify(msg)+'\n');
    if ('id' in msg && pending.has(msg.id)) {
      pending.get(msg.id)(msg);
      pending.delete(msg.id);
    } else {
      events.push(msg);
      notices.emit('change');
    }
  });
  const initialized = await rpc('initialize',{clientInfo:{name:'goal_resume_research_probe',version:'0.1.0'},capabilities:{experimentalApi:true}});
  ws.send(JSON.stringify({method:'initialized',params:{}}));
  for (const mode of ['queue_only','resume_after_notification','resume_during_notification']) await scenario(mode);
  const output = process.env.GOAL_WAIT_OUTPUT || path.join(root,'results.json');
  fs.writeFileSync(output,JSON.stringify({validated:true,server:initialized.userAgent,root,scenarios},null,2)+'\n');
  console.log(JSON.stringify({validated:true,output}));
} catch(error) {
  console.error(error.stack);
  console.error(serverErrors.slice(-4000));
  process.exitCode=1;
} finally {
  clearTimeout(overall);
  cleanup();
  setTimeout(() => process.exit(process.exitCode??0),100);
}
