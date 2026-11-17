import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {parseHTML} from 'linkedom';
import {analysisEvidence} from '../frontend/analysis.js';
import {createTableTools} from '../frontend/table_reader.js';

const html=await readFile(new URL('../frontend/index.html',import.meta.url),'utf8');
const query=(extra={})=>({id:'saved-query',label:'Q1',title:'月度金额',columns:['月份','金额'],
  rows:Array.from({length:65},(_,i)=>({月份:i,金额:i*20})),row_count:65,ok:true,truncated:false,
  dax:'EVALUATE ...',created_at:'2026-11-17T10:00:00+08:00',warnings:[],...extra});
function fixture(data=query()) {
  const {window,document}=parseHTML(html),root=document.querySelector('#messages'),reader=document.querySelector('#table-reader');
  root.innerHTML=analysisEvidence([data]);root.scrollTop=240;
  const evidence=root.querySelector('details');evidence.setAttribute('open','');
  const opener=root.querySelector('[data-table-expand]');let focused=false;
  if(opener)opener.focus=()=>{focused=true;};
  reader.showModal=()=>{reader.open=true;};reader.close=()=>{reader.open=false;reader.dispatchEvent(new window.Event('close'));};
  const notices=[],tools=createTableTools(reader,{getQuery:id=>id===data.id?data:null,onNotice:message=>notices.push(message)});
  return {window,document,root,reader,evidence,opener,tools,notices,focused:()=>focused};
}

test('expanded result shows all saved rows while the message retains its 20-row preview and position',()=>{
  const f=fixture();assert.equal(f.root.querySelectorAll('tbody tr').length,20);
  assert.equal(f.tools.handleClick(f.opener),true);assert.equal(f.reader.open,true);
  assert.equal(f.reader.querySelectorAll('tbody tr').length,65);
  assert.ok(f.reader.textContent.includes('65 行 · 2 列'));
  assert.equal(f.reader.querySelector('a').getAttribute('href'),'/api/analysis/saved-query/csv');
  const cancel=new f.window.Event('cancel',{cancelable:true});f.reader.dispatchEvent(cancel);
  assert.equal(cancel.defaultPrevented,true);assert.equal(f.reader.open,false);assert.equal(f.focused(),true);
  assert.equal(f.root.querySelector('details'),f.evidence);assert.equal(f.evidence.hasAttribute('open'),true);
  assert.equal(f.root.scrollTop,240);assert.equal(f.reader.querySelector('table'),null);
});

test('expanded table preserves truncation, query time, warnings and scalar values without unsafe markup',()=>{
  const f=fixture(query({title:'<img src=x>',columns:['<script>','zero','false','nil','empty'],
    rows:[{'<script>':'<img src=x onerror=1>',zero:0,false:false,nil:null,empty:''}],truncated:true,
    warnings:['<script>warning</script>']}));
  f.tools.handleClick(f.opener);
  assert.equal(f.reader.querySelector('img,script,[onerror],[style]'),null);
  assert.ok(f.reader.textContent.includes('结果已截断，仅显示已保留部分'));
  assert.ok(f.reader.textContent.includes('2026-11-17T10:00:00+08:00'));
  assert.ok(f.reader.textContent.includes('<script>warning</script>'));
  assert.deepEqual([...f.reader.querySelectorAll('td')].map(td=>td.textContent),['<img src=x onerror=1>','0','false','空值','']);
  assert.ok(f.reader.querySelector('a').textContent.includes('已截断'));
});

test('empty and failed queries have no expand control, and stale controls fail safely',()=>{
  for(const data of [query({rows:[]}),query({ok:false,error:'invalid query'})])assert.equal(fixture(data).opener,null);
  const f=fixture();f.opener.dataset.tableExpand='deleted-query';f.tools.handleClick(f.opener);
  assert.ok(!f.reader.open);assert.equal(f.notices.length,1);
});

test('reopening uses the current saved result and resets only the readers scroll',()=>{
  const data=query(),f=fixture(data);f.tools.handleClick(f.opener);
  const body=f.reader.querySelector('[data-table-reader-body]');body.scrollTop=400;body.scrollLeft=80;
  f.tools.handleClick(f.reader.querySelector('[data-table-reader-close]'));
  data.rows=[{月份:'2026-12',金额:199}];f.tools.handleClick(f.opener);
  assert.equal(f.reader.querySelectorAll('tbody tr').length,1);assert.equal(body.scrollTop,0);assert.equal(body.scrollLeft,0);
  assert.equal(f.root.scrollTop,240);
});
