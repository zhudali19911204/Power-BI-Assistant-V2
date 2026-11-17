import test from 'node:test';
import assert from 'node:assert/strict';
import {parseHTML} from 'linkedom';
import {highlightCode} from '../frontend/code_highlight.js';
import {codeBlock,codeSource,toggleCodeWrap,copyCodeText,createCodeTools} from '../frontend/code_blocks.js';
import {patchHtml} from '../frontend/messages.js';
import {markdown} from '../frontend/markdown.js';

const fixture=()=>parseHTML('<html><body><section></section></body></html>');
function render(source,language='dax') {
  const {document}=fixture(),root=document.querySelector('section');
  root.innerHTML=codeBlock(source,language);return root;
}

test('DAX keywords, functions, escaped table/field names and comments stay distinct',()=>{
  const source=`销售额 =
// 汇总当前筛选；SUM("不是函数")
VAR Revenue = SUM('Sales''FY'[Amount]])])
RETURN IF(Revenue > 1.25e2, "https://host/-- /* <x> ""值""", BLANK())`;
  const root=render(source);
  assert.equal(root.querySelector('code').textContent,source);
  assert.equal(root.querySelector('.code-token-comment').textContent,'// 汇总当前筛选；SUM("不是函数")');
  assert.deepEqual([...root.querySelectorAll('.code-token-keyword')].map(e=>e.textContent),['VAR','RETURN']);
  assert.ok([...root.querySelectorAll('.code-token-function')].some(e=>e.textContent==='SUM'));
  assert.ok([...root.querySelectorAll('.code-token-identifier')].some(e=>e.textContent==="'Sales''FY'"));
  assert.equal(root.querySelector('.code-token-string').textContent,'"https://host/-- /* <x> ""值"""');
  assert.equal(root.querySelector('.code-token-number').textContent,'1.25e2');
});

test('M supports quoted steps, literals, functions, comments and case-sensitive keywords',()=>{
  const source=`let
    // 保留输入步骤
    #"列""名称" = Table.TransformColumnTypes(Source, {{"金额", type number}}),
    Test = "a#(lf)//b" /* /* 不嵌套 */ + 0xFF,
    Let = #date(2026, 1, 1)
in #"列""名称"`;
  const root=render(source,'powerquery');
  assert.equal(root.querySelector('code').textContent,source);
  assert.deepEqual([...root.querySelectorAll('.code-token-keyword')].map(e=>e.textContent),['let','type','in']);
  assert.ok([...root.querySelectorAll('.code-token-function')].some(e=>e.textContent==='Table.TransformColumnTypes'));
  assert.ok([...root.querySelectorAll('.code-token-identifier')].some(e=>e.textContent==='#"列""名称"'));
  assert.ok([...root.querySelectorAll('.code-token-number')].some(e=>e.textContent==='0xFF'));
  assert.equal(root.querySelectorAll('.code-token-comment').length,2);
});

test('language aliases highlight while unknown languages remain safe plain text',()=>{
  for(const tag of ['dax','DAX-COLUMN','dax-measure','dax-table'])assert.ok(render('VAR x=1 RETURN x',tag).querySelector('.code-token-keyword'));
  for(const tag of ['m','powerquery','POWER-QUERY','pq','mscript'])assert.ok(render('let x=1 in x',tag).querySelector('.code-token-keyword'));
  const root=render('<script>alert(1)</script>','html');
  assert.equal(root.querySelector('script'),null);
  assert.equal(root.querySelector('code span'),null);
  assert.equal(codeSource(root.querySelector('.code-block')),'<script>alert(1)</script>');
});

test('highlighted code and metadata cannot inject markup, events or inline styles',()=>{
  const source='// </code><img src=x onerror=alert(1)>\nEVALUATE ROW("x", "</span><script>1</script>")';
  const {document}=fixture(),root=document.querySelector('section');
  root.innerHTML=codeBlock(source,'dax',{title:'<img src=x onerror=1>',copyLabel:'<script>bad</script>'});
  assert.equal(root.querySelector('script,img,[onerror],[style]'),null);
  assert.equal(root.querySelector('code').textContent,source);
  assert.equal(codeSource(root.querySelector('.code-block')),source);
  assert.ok(root.querySelector('[data-code-expand]'));
});

test('source survives HTML parsing with CRLF, tabs, quotes and trailing whitespace',()=>{
  const source='\r\n\t// 注释 & "\'"\r\nSUM(\'Sales\'[Value])  \r\n';
  const block=render(source).querySelector('.code-block');
  assert.equal(codeSource(block),source);
  assert.equal(JSON.parse(block.dataset.codeSource),source);
});

test('unfinished streamed literals and comments retain all characters',()=>{
  for(const [tag,source] of [['dax',"VAR x=SUM('Sales''FY'[Amount]])]) // 注释\nRETURN x"],
    ['m','let #"文本" = "a""//b" /* 注释 */ in #"文本"']]) {
    const {document}=fixture(),root=document.querySelector('section');
    for(let i=1;i<=source.length;i++) {
      const prefix=source.slice(0,i);patchHtml(root,codeBlock(prefix,tag));
      assert.equal(root.querySelector('code').textContent,prefix);
      assert.equal(codeSource(root.querySelector('.code-block')),prefix);
    }
  }
});

test('code controls and the readers wrap choice survive Markdown streaming',()=>{
  const {document}=fixture(),root=document.querySelector('section');
  patchHtml(root,markdown('```dax\nTotal = SUM('));
  const block=root.querySelector('.code-block'),copy=block.querySelector('.copy-code'),wrap=block.querySelector('[data-code-toggle-wrap]');
  toggleCodeWrap(block);
  patchHtml(root,markdown('```dax\nTotal = SUM(Sales[Value])\n```'));
  assert.equal(root.querySelector('.code-block'),block);assert.equal(root.querySelector('.copy-code'),copy);
  assert.equal(root.querySelector('[data-code-toggle-wrap]'),wrap);
  assert.equal(block.dataset.codeWrapped,'false');assert.equal(wrap.getAttribute('aria-pressed'),'false');
  assert.equal(codeSource(block),'Total = SUM(Sales[Value])');
  toggleCodeWrap(block);assert.equal(block.dataset.codeWrapped,'true');
});

test('large output falls back to escaped text without truncating copy content',()=>{
  const source='// <tag>\n'.repeat(12000),root=render(source);
  assert.equal(root.querySelector('code span'),null);
  assert.equal(root.querySelector('tag'),null);
  assert.equal(codeSource(root.querySelector('.code-block')),source);
  assert.equal(root.querySelector('code').textContent,source);
});

test('expanded reader copies its snapshot and closes without altering the parent form',async()=>{
  const {window,document}=fixture();
  document.body.innerHTML='<dialog id="parent"><input value="未保存的表名"><section class="message streaming"></section></dialog><dialog id="reader"><button data-code-reader-close></button><p data-code-reader-note hidden></p><div data-code-reader-body></div></dialog>';
  const parent=document.querySelector('#parent'),reader=document.querySelector('#reader'),input=parent.querySelector('input');
  parent.open=true;parent.scrollTop=175;
  const source='// 保留注释\r\nSUM(Sales[Value])\r\n',section=parent.querySelector('section');
  section.innerHTML=codeBlock(source,'dax');
  const opener=section.querySelector('[data-code-expand]');let focused=false;opener.focus=()=>{focused=true;};
  reader.showModal=()=>{reader.open=true;};reader.close=()=>{reader.open=false;reader.dispatchEvent(new window.Event('close'));};
  const copied=[],notices=[],tools=createCodeTools(reader,{copy:async text=>copied.push(text),onNotice:text=>notices.push(text)});
  await tools.handleClick(section.querySelector('[data-code-toggle-wrap]'));
  assert.equal(await tools.handleClick(opener),true);assert.equal(reader.open,true);
  assert.equal(reader.querySelector('[data-code-reader-note]').hidden,false);
  assert.equal(reader.querySelector('.code-block').dataset.codeWrapped,'false');
  assert.equal(reader.querySelector('[data-code-expand]'),null);
  patchHtml(section,codeBlock(source+' + 1','dax'));
  await tools.handleClick(reader.querySelector('.copy-code'));
  assert.deepEqual(copied,[source]);assert.deepEqual(notices,['代码已复制']);
  const cancel=new window.Event('cancel',{cancelable:true});reader.dispatchEvent(cancel);
  assert.equal(cancel.defaultPrevented,true);assert.equal(reader.open,false);assert.equal(parent.open,true);
  assert.equal(parent.querySelector('input'),input);assert.equal(input.value,'未保存的表名');assert.equal(parent.scrollTop,175);
  assert.equal(focused,true);assert.equal(reader.querySelector('code'),null);
});

test('clipboard fallback uses the active dialog and reports failure honestly',async()=>{
  const {document}=fixture();document.body.innerHTML='<dialog>'+codeBlock('x','dax')+'</dialog>';
  const dialog=document.querySelector('dialog'),block=dialog.querySelector('.code-block'),source='// 注释\r\nx\t';
  const create=document.createElement.bind(document);
  document.createElement=tag=>{const el=create(tag);if(tag==='textarea')el.select=()=>{};return el;};
  document.execCommand=command=>{
    assert.equal(command,'copy');assert.equal(dialog.querySelector('textarea').value,source);return true;
  };
  const clipboard={writeText:async()=>{throw new Error('unavailable');}};
  await copyCodeText(source,block,clipboard);assert.equal(document.querySelector('textarea'),null);
  document.execCommand=()=>false;
  await assert.rejects(()=>copyCodeText(source,block,clipboard),/复制失败/);
  assert.equal(document.querySelector('textarea'),null);
});
