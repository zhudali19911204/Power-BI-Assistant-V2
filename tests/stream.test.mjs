import test from 'node:test';
import assert from 'node:assert/strict';
import {readEvents} from '../frontend/stream.js';

test('status and Chinese text survive byte-by-byte delivery',async()=>{
  const bytes=new TextEncoder().encode('{"type":"status","text":"模型正在分析"}\n{"type":"delta","text":"你好"}\n{"type":"done"}');
  const response=new Response(new ReadableStream({start(controller){for(const b of bytes)controller.enqueue(new Uint8Array([b]));controller.close();}}));
  const events=[];await readEvents(response,event=>events.push(event));
  assert.deepEqual(events.map(e=>e.type),['status','delta','done']);
  assert.equal(events[1].text,'你好');
});

test('closed stream without completion is an interruption, not eternal loading',async()=>{
  const response=new Response('{"type":"start"}\n');
  await assert.rejects(readEvents(response,()=>{}),/响应已中断/);
});

test('a stalled reader has an idle timeout and is cancelled',async()=>{
  let cancelled=false;
  const response=new Response(new ReadableStream({cancel(){cancelled=true;}}));
  await assert.rejects(readEvents(response,()=>{},{idleTimeout:20}),/长时间未收到/);
  assert.equal(cancelled,true);
});

test('error events finish normally so the caller can display the server error',async()=>{
  const events=[];
  await readEvents(new Response('{"type":"error","text":"服务器超时"}\n'),event=>events.push(event));
  assert.equal(events[0].text,'服务器超时');
});
