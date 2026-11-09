import test from 'node:test';
import assert from 'node:assert/strict';
import {parseHTML} from 'linkedom';
import {mountComposerImages,imageGallery,transferImages} from '../frontend/images.js';
import {createWorkspaces} from '../frontend/workspaces.js';

const tick=()=>new Promise(resolve=>setImmediate(resolve));
const file=(name='截图.png',type='image/png')=>Object.assign(new Blob(['bytes'],{type}),{name});

function fixture({upload}={}) {
  const {window,document}=parseHTML('<form><button data-add-images type="button">图片</button><button data-screenshot type="button">截图</button><input data-image-input type="file"><div data-image-drafts></div><textarea></textarea></form>');
  const root=document.querySelector('form'),workspace=createWorkspaces(),calls=[],removed=[],revoked=[],notices=[],changed=[];
  let next=0,snips=0;
  const controller=mountComposerImages(root,{getView:()=>workspace.current,
    upload:upload||(async image=>{calls.push(image);const id=String(++next).repeat(32);return {id,name:image.name,url:'/api/images/'+id,mime_type:image.type};}),
    createURL:()=>`blob:local-${next++}`,revokeURL:url=>revoked.push(url),remove:async id=>removed.push(id),
    onChange:owner=>changed.push(owner),onNotice:(message,error)=>notices.push({message,error}),
    screenshot:async()=>{snips++;return {detail:'截图后 Ctrl+V'};}});
  return {window,root,workspace,controller,calls,removed,revoked,notices,changed,get snips(){return snips;}};
}

test('selected images upload and preview without sending a model request',async()=>{
  const f=fixture();await f.controller.add([file()]);
  assert.equal(f.workspace.current.images.length,1);
  assert.equal(f.workspace.current.images[0].status,'ready');
  assert.equal(f.calls.length,1);assert.equal(f.root.querySelector('[data-image-drafts]').hidden,false);
  assert.equal(f.root.querySelectorAll('.image-draft').length,1);assert.equal(f.revoked.length,1);
});

test('clipboard image is consumed while ordinary pasted text stays native',async()=>{
  const f=fixture(),image=file(),event=new f.window.Event('paste',{bubbles:true,cancelable:true});
  Object.defineProperty(event,'clipboardData',{value:{items:[{kind:'file',type:image.type,getAsFile:()=>image}]}});
  f.root.querySelector('textarea').dispatchEvent(event);await tick();
  assert.equal(event.defaultPrevented,true);assert.equal(f.calls.length,1);
  const textEvent=new f.window.Event('paste',{bubbles:true,cancelable:true});
  Object.defineProperty(textEvent,'clipboardData',{value:{items:[{kind:'string',type:'text/plain'}]}});
  f.root.dispatchEvent(textEvent);assert.equal(textEvent.defaultPrevented,false);
});

test('an upload finishing after navigation attaches only to its original draft',async()=>{
  let resume;const f=fixture({upload:()=>new Promise(resolve=>{resume=resolve;})});
  const owner=f.workspace.current,pending=f.controller.add([file()]);
  assert.equal(owner.images[0].status,'uploading');
  f.workspace.switchMode('analysis');f.controller.render();
  assert.equal(f.root.querySelector('[data-image-drafts]').hidden,true);
  resume({id:'a'.repeat(32),url:'/api/images/'+'a'.repeat(32),name:'截图.png'});await pending;
  assert.equal(owner.images[0].status,'ready');assert.equal(f.workspace.current.images.length,0);
  f.workspace.switchMode('dax');f.controller.render();assert.equal(f.root.querySelectorAll('.image-draft').length,1);
});

test('removing an in-flight image deletes its late upload without restoring the thumbnail',async()=>{
  let resume;const f=fixture({upload:()=>new Promise(resolve=>{resume=resolve;})});
  const pending=f.controller.add([file()]);f.root.querySelector('[data-remove-image]').click();
  assert.equal(f.workspace.current.images.length,0);
  resume({id:'a'.repeat(32),url:'/api/images/'+'a'.repeat(32),name:'截图.png'});await pending;
  assert.deepEqual(f.removed,['a'.repeat(32)]);assert.equal(f.workspace.current.images.length,0);
});

test('image count, format and byte limits are enforced before upload',async()=>{
  const f=fixture();await f.controller.add([file('bad.svg','image/svg+xml'),{name:'big.png',type:'image/png',size:9*1024*1024}]);
  assert.equal(f.calls.length,0);assert.equal(f.notices.length,2);
  await f.controller.add(Array.from({length:5},(_,i)=>file(`${i}.png`)));
  assert.equal(f.calls.length,4);assert.equal(f.workspace.current.images.length,4);
  assert.ok(f.notices.at(-1).message.includes('4 张'));
});

test('failed upload leaves text and other workspace drafts intact',async()=>{
  const f=fixture({upload:async()=>{throw new Error('图片上传失败');}});
  f.workspace.current.draft='未发送的文字';await f.controller.add([file()]);
  assert.equal(f.workspace.current.draft,'未发送的文字');assert.equal(f.workspace.current.images.length,0);
  assert.equal(f.notices[0].message,'图片上传失败');
});

test('Windows screenshot action displays paste guidance without capturing automatically',async()=>{
  const f=fixture();f.root.querySelector('[data-screenshot]').click();await tick();
  assert.equal(f.snips,1);assert.ok(f.notices.at(-1).message.includes('Ctrl+V'));assert.equal(f.calls.length,0);
});

test('image gallery and filename labels cannot create active markup',()=>{
  const {document}=parseHTML('<section></section>'),root=document.querySelector('section');
  root.innerHTML=imageGallery([{id:'a'.repeat(32),name:'<script>alert(1)</script>'}]);
  assert.equal(root.querySelector('script'),null);assert.ok(root.textContent.includes('<script>'));
  assert.equal(root.querySelector('img').getAttribute('src'),'/api/images/'+'a'.repeat(32));
});

test('dragged clipboard items keep image files and ignore text content',()=>{
  const picture=file();
  assert.deepEqual(transferImages({files:[picture,{type:'text/plain'}]}),[picture]);
});
