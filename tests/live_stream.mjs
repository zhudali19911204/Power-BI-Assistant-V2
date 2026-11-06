// Actual HTTP transport + the same decoder imported by the desktop frontend.
import assert from 'node:assert/strict';
import {readEvents} from '../frontend/stream.js';
import {parseHTML} from 'linkedom';
import {createMessageRenderer} from '../frontend/messages.js';
import {markdown} from '../frontend/markdown.js';

const {document}=parseHTML('<html><body><div id="scroll"><div id="messages"></div></div></body></html>');
const root=document.querySelector('#messages'),scroll=document.querySelector('#scroll');
Object.assign(scroll,{scrollHeight:1000,scrollTop:600,clientHeight:400});scroll.scrollTo=()=>{};
const message={role:'assistant',status:'streaming',content:'',artifacts:[]};
const renderer=createMessageRenderer({root,scroll,label:()=>'<span>助手</span>',content:m=>markdown(m.content),
  artifacts:()=>'',status:()=>''});
renderer.render([message],{conversation:'live-stream'});
const article=root.firstChild,body=article.querySelector('.message-content');let deltaUpdates=0;

const base=process.argv[2];
const model=(await (await fetch(base+'/api/models')).json())[0];
const conv=await (await fetch(base+'/api/conversations',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({model_id:model.id,title:'流式接口验证'})})).json();
const started=Date.now();
let firstStatus=null,firstDelta=null,characters=0,done=false,heartbeats=0;
const response=await fetch(`${base}/api/conversations/${conv.id}/messages`,{method:'POST',
  headers:{'Content-Type':'application/json'},body:JSON.stringify({content:'分析下这个模型，请给出简洁的结构分析和改进建议。不要生成代码。'})});
assert.equal(response.status,200);
await readEvents(response,event=>{
  if(event.type==='status'){
    firstStatus??=Date.now()-started;
    console.log(`${((Date.now()-started)/1000).toFixed(2)}s STATUS ${event.text}`);
  }
  if(event.type==='heartbeat')heartbeats++;
  if(event.type==='delta'){
    if(firstDelta===null){firstDelta=Date.now()-started;console.log('FIRST_CONTENT_SECONDS',firstDelta/1000);}
    characters+=event.text.length;
    message.content+=event.text;
    renderer.render([message],{conversation:'live-stream'});
    assert.equal(root.firstChild,article,'Streaming must retain the original message article');
    assert.equal(article.querySelector('.message-content'),body,'Streaming must retain the content container');
    deltaUpdates++;
  }
  if(event.type==='error')throw new Error(event.text);
  if(event.type==='done'){done=true;message.status='complete';renderer.render([message],{conversation:'live-stream'});}
});
assert.ok(done&&characters>0);
assert.ok(firstStatus<5000,'Initial progress must arrive before waiting for the model');
assert.equal(root.firstChild,article);assert.ok(body.textContent.length>0);
console.log(JSON.stringify({result:'PASS',firstStatusMs:firstStatus,firstDeltaMs:firstDelta,
  answerCharacters:characters,deltaUpdates,messageNodeRetained:true,heartbeats,totalSeconds:(Date.now()-started)/1000}));
