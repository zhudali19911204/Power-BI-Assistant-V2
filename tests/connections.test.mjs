import test from 'node:test';
import assert from 'node:assert/strict';
import {createConnectionMonitor,connectionPresentation} from '../frontend/connections.js';

test('connection states distinguish recovery, changed schema, ambiguity and demo',()=>{
  const model={source:'desktop',live:false};
  assert.equal(connectionPresentation({...model,connection:{status:'reconnecting'}}).label,'自动重连中');
  assert.equal(connectionPresentation({...model,connection:{status:'changed'}}).label,'结构待同步');
  assert.equal(connectionPresentation({...model,connection:{status:'ambiguous'}}).label,'请选择报表');
  assert.equal(connectionPresentation({...model,connection:{status:'disconnected'}}).label,'等待 Desktop');
  assert.equal(connectionPresentation({...model,live:true}).kind,'connected');
  assert.equal(connectionPresentation({source:'demo'}).label,'示例模型');
});

test('checks deduplicate model IDs, skip demo and merge only validated response IDs',async()=>{
  const calls=[],updates=[];
  const monitor=createConnectionMonitor({getModels:()=>[{id:'A',source:'desktop'},{id:'A',source:'desktop'},{id:'demo',source:'demo'}],
    api:async path=>{calls.push(path);return {model_id:'A',live:true,port:60001,connection:{status:'connected'}};},onChange:value=>updates.push(value)});
  await monitor.refresh();assert.deepEqual(calls,['/models/A/connection']);assert.equal(updates[0].port,60001);
});

test('navigation during an in-flight check cannot apply its result to another model',async()=>{
  let selected={id:'A',source:'desktop'},resolve;
  const calls=[],updates=[];
  const monitor=createConnectionMonitor({getModels:()=>[selected],api:path=>{
    calls.push(path);return new Promise(done=>{resolve=done;});},onChange:value=>updates.push(value)});
  const pending=monitor.refresh();const duplicate=monitor.refresh();
  selected={id:'B',source:'desktop'};resolve({model_id:'A',live:true});
  await Promise.all([pending,duplicate]);assert.equal(calls.length,1);assert.equal(updates[0].model_id,'A');
});

test('local HTTP failure is not shown as proof of Desktop disconnection',async()=>{
  const updates=[],failures=[];
  const monitor=createConnectionMonitor({getModels:()=>[{id:'A',source:'desktop'}],api:async()=>{throw new Error('offline');},
    onChange:value=>updates.push(value),onUnavailable:error=>failures.push(error.message)});
  await monitor.refresh();assert.deepEqual(updates,[]);assert.deepEqual(failures,['offline']);
});
