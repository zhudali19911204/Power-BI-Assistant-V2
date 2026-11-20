import test from 'node:test';
import assert from 'node:assert/strict';
import {parseHTML} from 'linkedom';
import {createWorkspaceLayout} from '../frontend/layout.js';

const tick=()=>new Promise(resolve=>setImmediate(resolve));
function fixture(save,{withSidebar=false}={}) {
  const {window,document}=parseHTML(`<html><body><section>${withSidebar?'<aside id="sidebar"><div role="separator"></div></aside>':''}<main><aside><div role="separator"></div><button id="context-close"></button><input id="field-search"></aside></main></section><button id="toggle-context"></button></body></html>`);
  const root=document.querySelector('main'),panel=root.querySelector('aside'),handle=panel.querySelector('[role="separator"]'),toggle=document.querySelector('#toggle-context');
  const sidebarPanel=document.querySelector('#sidebar'),sidebarHandle=sidebarPanel?.firstElementChild;
  let viewport=1440,captured=null;const writes=[],notices=[];
  root.clientWidth=1192;
  for(const target of [handle,sidebarHandle].filter(Boolean)) {
    target.setPointerCapture=id=>{captured=id;};target.hasPointerCapture=id=>captured===id;
    target.releasePointerCapture=()=>{captured=null;target.dispatchEvent(new window.Event('lostpointercapture'));};
  }
  const layout=createWorkspaceLayout({root,panel,handle,toggle,win:window,viewportWidth:()=>viewport,
    sidebar:withSidebar?{panel:sidebarPanel,handle:sidebarHandle}:null,
    save:save|| (async value=>writes.push(value)),onNotice:message=>notices.push(message)});
  function event(type,extra={},target=handle) {
    const e=new window.Event(type,{cancelable:true});
    Object.assign(e,{pointerId:1,button:0,isPrimary:true,clientX:1000,...extra});target.dispatchEvent(e);return e;
  }
  return {root,panel,handle,toggle,layout,writes,notices,event,width:()=>Number(handle.getAttribute('aria-valuenow')),
    sidebarHandle,leftWidth:()=>Number(sidebarHandle.getAttribute('aria-valuenow')),leftEvent:(type,extra)=>event(type,extra,sidebarHandle),
    resize(size,space){viewport=size;root.clientWidth=space;window.dispatchEvent(new window.Event('resize'));},
    blur(){window.dispatchEvent(new window.Event('blur'));}};
}

test('dragging preserves chat space, limits width and saves only on release',async()=>{
  const f=fixture();f.layout.restore({context_width:320});
  f.event('pointerdown');f.event('pointermove',{clientX:850});
  assert.equal(f.width(),470);assert.equal(f.writes.length,0);
  assert.ok(f.root.classList.contains('resizing-context'));
  f.event('pointermove',{clientX:100});assert.equal(f.width(),520);
  f.event('pointerup');await tick();assert.deepEqual(f.writes,[{context_width:520}]);
  assert.equal(f.root.classList.contains('resizing-context'),false);
  f.resize(1100,800);assert.equal(f.width(),320);assert.equal(800-f.width(),480);
  f.resize(1440,1192);assert.equal(f.width(),520);assert.equal(f.writes.length,1);
  f.event('pointerdown');f.event('pointermove',{clientX:2000});f.event('pointerup');await tick();
  assert.equal(f.width(),240);assert.equal(f.writes.at(-1).context_width,240);
  const count=f.writes.length;f.event('pointerdown');f.event('pointerup');
  f.event('keydown',{key:'ArrowRight',repeat:true});await tick();assert.equal(f.writes.length,count);
});

test('cancelled or interrupted drags restore the original width without saving',async()=>{
  for(const cancel of ['pointercancel','lostpointercapture','blur','resize','Escape']) {
    const f=fixture();f.layout.restore({context_width:350});
    f.event('pointerdown');f.event('pointermove',{clientX:900});assert.equal(f.width(),450);
    if(cancel==='blur')f.blur();else if(cancel==='resize')f.resize(1440,1192);
    else if(cancel==='Escape')f.event('keydown',{key:'Escape'});else f.event(cancel);
    f.event('pointerup');await tick();
    assert.equal(f.width(),350,cancel);assert.deepEqual(f.writes,[],cancel);
    assert.equal(f.root.classList.contains('resizing-context'),false);
  }
});

test('keyboard, reset and late saved preferences honor the most recent user choice',async()=>{
  const f=fixture();
  assert.equal(f.event('keydown',{key:'ArrowLeft'}).defaultPrevented,true);assert.equal(f.width(),294);
  f.layout.restore({context_width:480});assert.equal(f.width(),294);
  f.event('keydown',{key:'ArrowLeft',shiftKey:true});assert.equal(f.width(),358);
  f.event('keydown',{key:'ArrowRight'});assert.equal(f.width(),342);
  f.event('keydown',{key:'Home'});assert.equal(f.width(),240);
  f.event('keydown',{key:'End'});assert.equal(f.width(),520);
  f.event('keydown',{key:'Home',isComposing:true});assert.equal(f.width(),520);
  f.event('dblclick');await tick();assert.equal(f.width(),278);assert.equal(f.writes.at(-1).context_width,null);
  f.resize(1700,1435);assert.equal(f.width(),300);
});

test('collapsed and mobile panels disable resizing and preserve the desktop preference',async()=>{
  const f=fixture();f.layout.restore({context_width:420});
  f.toggle.click();assert.equal(f.toggle.getAttribute('aria-expanded'),'false');assert.equal(f.handle.hidden,true);
  f.event('pointerdown');f.event('pointermove',{clientX:100});f.event('pointerup');
  f.event('keydown',{key:'End'});assert.equal(f.width(),420);
  f.resize(1000,785);f.toggle.click();
  assert.ok(f.panel.classList.contains('mobile-open'));assert.equal(f.toggle.getAttribute('aria-expanded'),'true');
  assert.equal(f.handle.hidden,true);f.event('dblclick');await tick();assert.deepEqual(f.writes,[]);
  f.resize(1440,1192);f.toggle.click();assert.equal(f.width(),420);assert.equal(f.handle.hidden,false);
});

test('the narrow model panel focuses search and returns to its trigger on close or Escape',()=>{
  const f=fixture(),search=f.panel.querySelector('#field-search'),close=f.panel.querySelector('#context-close');
  let focused;
  for(const el of [search,close,f.toggle])el.focus=()=>{focused=el;};
  f.resize(1000,785);
  f.toggle.click();assert.equal(focused,close);
  close.click();assert.equal(focused,f.toggle);assert.equal(f.toggle.getAttribute('aria-expanded'),'false');
  f.panel.classList.add('has-model');
  f.toggle.click();assert.equal(focused,search);
  f.event('keydown',{key:'Escape',isComposing:true},f.panel);
  assert.equal(f.toggle.getAttribute('aria-expanded'),'true');
  f.event('keydown',{key:'Escape'},f.panel);
  assert.equal(focused,f.toggle);assert.equal(f.toggle.title,'展开模型字段面板');
  f.resize(1440,1192);f.layout.dismissOverlay();
  assert.equal(f.toggle.getAttribute('aria-expanded'),'true');
});

test('slow preference writes remain ordered and a failed save does not disable adjustment',async()=>{
  const writes=[];let release;
  const f=fixture(async value=>{
    writes.push(value);
    if(writes.length===1)await new Promise(resolve=>{release=resolve;});
    if(writes.length===2)throw new Error('offline');
  });
  f.event('keydown',{key:'ArrowLeft'});f.event('keydown',{key:'ArrowLeft'});f.event('keydown',{key:'ArrowLeft'});
  await tick();assert.equal(writes.length,1);release();await tick();
  assert.deepEqual(writes.map(w=>w.context_width),[294,310,326]);assert.equal(f.width(),326);assert.equal(f.notices.length,1);
});

test('invalid persisted values fall back to a safe default',()=>{
  for(const context_width of [-1,239,521,'350',null,NaN]) {
    const f=fixture();f.layout.restore({context_width});assert.equal(f.width(),278);
  }
});

test('left sidebar drag and keyboard follow the boundary direction and persist independently',async()=>{
  const f=fixture(undefined,{withSidebar:true});f.layout.restore({sidebar_width:280,context_width:390});
  f.leftEvent('pointerdown');f.leftEvent('pointermove',{clientX:1060});
  assert.equal(f.leftWidth(),340);assert.equal(f.width(),390);assert.equal(f.writes.length,0);
  f.leftEvent('pointerup');await tick();assert.deepEqual(f.writes,[{sidebar_width:340}]);
  f.leftEvent('keydown',{key:'ArrowLeft'});assert.equal(f.leftWidth(),324);
  f.leftEvent('keydown',{key:'ArrowRight'});assert.equal(f.leftWidth(),340);
  f.leftEvent('keydown',{key:'Home'});assert.equal(f.leftWidth(),215);
  f.leftEvent('keydown',{key:'End'});assert.equal(f.leftWidth(),400);
  f.leftEvent('dblclick');await tick();assert.equal(f.leftWidth(),248);
  assert.equal(f.width(),390);assert.deepEqual(f.writes.at(-1),{sidebar_width:null});
});

test('both panels leave room for chat and recover preferred widths after viewport and visibility changes',async()=>{
  const f=fixture(undefined,{withSidebar:true});f.layout.restore({sidebar_width:400,context_width:520});
  f.resize(1100,700);assert.equal(f.leftWidth(),380);assert.equal(f.width(),240);
  assert.equal(1100-f.leftWidth()-f.width(),480);
  f.toggle.click();assert.equal(f.leftWidth(),400);
  f.toggle.click();assert.equal(f.leftWidth(),380);assert.equal(f.width(),240);
  f.resize(1440,1040);assert.equal(f.leftWidth(),400);assert.equal(f.width(),520);
  f.resize(1200,800);assert.equal(f.width(),320);
  f.leftEvent('pointerdown');f.leftEvent('pointermove',{clientX:900});
  assert.equal(f.leftWidth(),300);assert.equal(f.width(),420);
  f.leftEvent('pointercancel');assert.equal(f.leftWidth(),400);assert.equal(f.width(),320);
  await tick();assert.deepEqual(f.writes,[]);
});

test('sidebar remains resizable with a mobile model overlay but disables resizing in icon-only navigation',async()=>{
  const f=fixture(undefined,{withSidebar:true});f.layout.restore({sidebar_width:350,context_width:480});
  f.resize(1000,650);assert.equal(f.sidebarHandle.hidden,false);assert.equal(f.handle.hidden,true);
  f.leftEvent('keydown',{key:'ArrowRight'});assert.equal(f.leftWidth(),366);
  f.resize(700,635);assert.equal(f.sidebarHandle.hidden,true);
  f.leftEvent('pointerdown');f.leftEvent('pointermove',{clientX:1100});f.leftEvent('pointerup');
  f.leftEvent('dblclick');f.leftEvent('keydown',{key:'Home'});await tick();
  assert.deepEqual(f.writes,[{sidebar_width:366}]);
  f.resize(1440,1074);assert.equal(f.leftWidth(),366);assert.equal(f.width(),480);
});

test('a late configuration load does not replace a dragged sidebar but still restores the untouched model panel',()=>{
  const f=fixture(undefined,{withSidebar:true});
  f.leftEvent('pointerdown');f.leftEvent('pointermove',{clientX:1050});f.leftEvent('pointerup');
  f.layout.restore({sidebar_width:400,context_width:430});
  assert.equal(f.leftWidth(),298);assert.equal(f.width(),430);
});
