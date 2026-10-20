// Short smoke test using the configured real model and its existing login.
// Uses a fixture thread archived on cleanup, a private app-server, and a temporary cwd.
import assert from 'node:assert/strict';
import {EventEmitter,once} from 'node:events';
import fs from 'node:fs';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import {spawn} from 'node:child_process';

const root=fs.mkdtempSync(path.join(os.tmpdir(),'goal-resume-live-'));
const codexBinary=process.env.GOAL_WAIT_CODEX_BIN || 'codex';
const notices=new EventEmitter();
const events=[];
const pending=new Map();
let seq=0;
let ws,server,threadId,currentTurn;
const startedAt=Date.now();
const reservation=net.createServer();
reservation.listen(0,'127.0.0.1');
await once(reservation,'listening');
const port=reservation.address().port;
await new Promise(resolve=>reservation.close(resolve));
const endpoint=`ws://127.0.0.1:${port}`;

function rpc(method,params) {
  return new Promise((resolve,reject)=>{
    const id=++seq;
    const timer=setTimeout(()=>{pending.delete(id);reject(new Error(`RPC timed out: ${method}`));},15000);
    pending.set(id,msg=>{clearTimeout(timer);if(msg.error)reject(new Error(`${method}: ${JSON.stringify(msg.error)}`));else resolve(msg.result);});
    ws.send(JSON.stringify({id,method,params}));
  });
}

function waitFor(predicate,label,timeout=120000) {
  return new Promise((resolve,reject)=>{
    const timer=setTimeout(()=>{notices.off('change',check);reject(new Error(`Timed out waiting for ${label}`));},timeout);
    function check(){const value=predicate();if(value){clearTimeout(timer);notices.off('change',check);resolve(value);}}
    notices.on('change',check);check();
  });
}

async function job() {
  return new Promise((resolve,reject)=>{
    const watcher=fs.watch(root,(event,filename)=>{
      if(filename==='job-result.json' && fs.existsSync(path.join(root,filename))){watcher.close();resolve({event,filename});}
    });
    const child=spawn(process.execPath,['-e',"const fs=require('node:fs'),path=require('node:path');setTimeout(()=>{const dir=process.argv[1];fs.writeFileSync(path.join(dir,'result.tmp'),JSON.stringify({exitCode:0}));fs.renameSync(path.join(dir,'result.tmp'),path.join(dir,'job-result.json'));},2000)",root],{stdio:'ignore'});
    child.once('error',error=>{watcher.close();reject(error);});
  });
}

try {
  server=spawn(codexBinary,['app-server','--listen',endpoint,'-c','mcp_servers.zotero-mcp.enabled=false','-c','features.hooks=false'],{detached:true,stdio:['ignore','ignore','ignore']});
  await new Promise((resolve,reject)=>{
    let attempts=0;
    const connect=()=>{
      const candidate=new WebSocket(endpoint);
      candidate.onopen=()=>{ws=candidate;resolve();};
      candidate.onerror=()=>{if(++attempts<150)setTimeout(connect,50);else reject(new Error('Private app-server did not open'));};
    };
    connect();
  });
  ws.addEventListener('message',event=>{
    const msg=JSON.parse(event.data);
    if('id' in msg && pending.has(msg.id)){pending.get(msg.id)(msg);pending.delete(msg.id);return;}
    if(msg.method==='turn/started')currentTurn=msg.params.turn.id;
    if(msg.method==='turn/completed')currentTurn=null;
    events.push(msg);
    if(['turn/started','turn/completed','thread/goal/updated','error'].includes(msg.method)){
      console.log(JSON.stringify({event:msg.method,turnId:msg.params.turnId || msg.params.turn?.id,status:msg.params.goal?.status || msg.params.turn?.status,elapsedMs:Date.now()-startedAt}));
    }
    notices.emit('change');
  });
  const initialized=await rpc('initialize',{clientInfo:{name:'goal_resume_live_probe',version:'0.1.0'},capabilities:{experimentalApi:true}});
  ws.send(JSON.stringify({method:'initialized',params:{}}));
  const fixture=await rpc('thread/start',{cwd:root,approvalPolicy:'never',sandbox:'workspace-write'});
  threadId=fixture.thread.id;
  const objective='Integration test in this temporary directory only: when job-result.json reports exitCode 0, write exactly PASS followed by a newline to resumed-goal.txt, verify the result, and call update_goal with status complete. Do not delegate or spawn other agents. No other work is required.';
  const original=(await rpc('thread/goal/set',{threadId,objective,status:'paused',origin:'user'})).goal;
  const completion=await job();
  assert.equal(events.filter(e=>e.method==='turn/started' && e.params.threadId===threadId).length,0);
  console.log(JSON.stringify({event:'external_job_complete',completion,turnsWhileWaiting:0}));
  const notification='GW_JOB_DONE: the external job finished and job-result.json is ready. For this notification turn, reply exactly ACK_ONLY and take no tools or other actions. The test helper will explicitly activate the paused goal after this turn completes.';
  const added=await rpc('thread/queue/add',{threadId,clientUserMessageId:`goal-resume-live-${Date.now()}`,input:[{type:'text',text:notification}]});
  const notificationStarted=await waitFor(()=>events.find(e=>e.method==='turn/started' && e.params.threadId===threadId),'notification turn');
  const notificationTurnId=notificationStarted.params.turn.id;
  const notificationCompleted=await waitFor(()=>events.find(e=>e.method==='turn/completed' && e.params.turn.id===notificationTurnId),'notification completion');
  assert.equal(notificationCompleted.params.turn.status,'completed');
  const afterNotification=(await rpc('thread/goal/get',{threadId})).goal;
  assert.equal(afterNotification.status,'paused');
  assert(!fs.existsSync(path.join(root,'resumed-goal.txt')));
  const resumed=(await rpc('thread/goal/set',{threadId,status:'active',origin:'automatic'})).goal;
  assert.equal(resumed.status,'active');
  assert.equal(resumed.createdAt,original.createdAt);
  await waitFor(()=>events.find(e=>e.method==='thread/goal/updated' && e.params.threadId===threadId && e.params.goal.status==='complete'),'goal complete');
  await waitFor(()=>events.find(e=>e.method==='turn/completed' && e.params.threadId===threadId && e.params.turn.id!==notificationTurnId),'automatic continuation completion');
  const finalGoal=(await rpc('thread/goal/get',{threadId})).goal;
  const artifact=fs.readFileSync(path.join(root,'resumed-goal.txt'),'utf8');
  assert.equal(artifact,'PASS\n');
  assert.equal(finalGoal.status,'complete');
  assert.equal(finalGoal.objective,original.objective);
  const scoped=events.filter(e=>e.params?.threadId===threadId);
  const summary={
    validated:true,mode:'real_model',server:initialized.userAgent,
    model:fixture.model || fixture.thread.model,modelProvider:fixture.modelProvider || fixture.thread.modelProvider,
    threadId,ephemeral:fixture.thread.ephemeral,root,
    goalStatusAfterNotification:afterNotification.status,
    explicitResumeStatus:resumed.status,finalGoalStatus:finalGoal.status,
    objectivePreserved:finalGoal.objective===original.objective,
    createdAtPreserved:finalGoal.createdAt===original.createdAt,
    turnsWhileWaiting:0,turnsStarted:scoped.filter(e=>e.method==='turn/started').length,
    artifact,elapsedMs:Date.now()-startedAt,
    tokenUsage:scoped.filter(e=>e.method==='thread/tokenUsage/updated').at(-1)?.params.tokenUsage || null,
    goalTokensUsed:finalGoal.tokensUsed,
    timeline:scoped.filter(e=>['turn/started','turn/completed','thread/goal/updated','item/completed'].includes(e.method)).map(e=>({
      method:e.method,turnId:e.params.turnId || e.params.turn?.id,
      goalStatus:e.params.goal?.status,itemType:e.params.item?.type,
      text:e.params.item?.type==='agentMessage' ? e.params.item.text : undefined,
      command:e.params.item?.command,
    })),
  };
  assert(summary.turnsStarted>=2);
  const output=process.env.GOAL_WAIT_OUTPUT || path.join(root,'results.json');
  fs.writeFileSync(output,JSON.stringify(summary,null,2)+'\n');
  console.log(JSON.stringify({validated:true,output,summary}));
} catch(error) {
  console.error(error.stack);
  if(threadId && ws?.readyState===WebSocket.OPEN){
    if(currentTurn)await rpc('turn/interrupt',{threadId,turnId:currentTurn}).catch(()=>{});
    await rpc('thread/goal/set',{threadId,status:'paused',origin:'automatic'}).catch(()=>{});
  }
  process.exitCode=1;
} finally {
  if(threadId && ws?.readyState===WebSocket.OPEN)await rpc('thread/archive',{threadId}).catch(()=>{});
  ws?.close();
  if(server){try{process.kill(-server.pid,'SIGKILL');}catch{}}
  setTimeout(()=>process.exit(process.exitCode??0),100);
}
