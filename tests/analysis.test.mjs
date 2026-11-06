import test from 'node:test';
import assert from 'node:assert/strict';
import {parseHTML} from 'linkedom';
import {analysisEvidence} from '../frontend/analysis.js';
import {MODES} from '../frontend/modes.js';
import {createMessageRenderer} from '../frontend/messages.js';

function query(extra={}) {
  return {id:'test-id',label:'Q1',title:'按月销售额',dax:'EVALUATE ROW("Amount",125)',
    columns:['月份','金额'],rows:[{'月份':'2026-01','金额':125}],row_count:1,
    ok:true,truncated:false,warnings:[],seconds:0.2,attempt:1,created_at:'2026-11-05T08:00:00Z',...extra};
}
const element=html=>parseHTML('<html><body><section>'+html+'</section></body></html>').document.querySelector('section');

test('analysis is a distinct mode with data overview, trend and contribution prompts',()=>{
  assert.equal(MODES.analysis.title,'数据分析助手');
  assert.equal(MODES.analysis.cards.length,3);
  assert.ok(MODES.analysis.cards.every(card=>card[3].length>10));
  assert.notEqual(MODES.analysis.placeholder,MODES.dax.placeholder);
});

test('query evidence displays actual cells, executable query and CSV download',()=>{
  const root=element(analysisEvidence([query()]));
  assert.equal(root.querySelectorAll('tbody tr').length,1);
  assert.ok(root.textContent.includes('125'));
  assert.equal(root.querySelector('code').textContent,'EVALUATE ROW("Amount",125)');
  assert.equal(root.querySelector('a').getAttribute('href'),'/api/analysis/test-id/csv');
  assert.equal(root.querySelector('[data-apply]'),null);
});

test('truncated and empty results are explicit, not shown as zero or full data',()=>{
  const partial=element(analysisEvidence([query({truncated:true,warnings:['结果已截断']})]));
  assert.ok(partial.textContent.includes('已截断'));
  const empty=element(analysisEvidence([query({columns:[],rows:[],row_count:0})]));
  assert.ok(empty.textContent.includes('不能据此将业务指标认定为零'));
});

test('model-provided text is escaped in table cells, column names and queries',()=>{
  const root=element(analysisEvidence([query({title:'<img src=x>',columns:['<script>'],rows:[{'<script>':'<img src=x onerror=1>'}]})]));
  assert.equal(root.querySelector('img'),null);assert.equal(root.querySelector('script'),null);
  assert.ok(root.textContent.includes('<img src=x onerror=1>'));
});

test('evidence changes update the message independently of prose or generated measure artifacts',()=>{
  const {document}=parseHTML('<html><body><div id="scroll"><section></section></div></body></html>');
  const root=document.querySelector('section'),scroll=document.querySelector('#scroll');
  Object.assign(scroll,{scrollHeight:1000,scrollTop:600,clientHeight:400});scroll.scrollTo=()=>{};
  const renderer=createMessageRenderer({root,scroll,label:()=>'',content:m=>m.content,
    artifacts:m=>analysisEvidence(m.analysis||[]),status:()=>''});
  const message={role:'assistant',status:'streaming',content:'<p>分析口径</p>',artifacts:[],analysis:[]};
  renderer.render([message],{conversation:'c'});
  const article=root.firstChild,paragraph=root.querySelector('p');
  message.analysis=[query()];renderer.render([message],{conversation:'c'});
  assert.ok(root.querySelector('.analysis-evidence'));
  const expanded=root.querySelector('.analysis-evidence');expanded.setAttribute('open','');
  message.analysis.push(query({id:'second',label:'Q2'}));
  renderer.render([message],{conversation:'c'});
  assert.equal(root.querySelector('.analysis-evidence'),expanded);
  assert.ok(expanded.hasAttribute('open'));
  message.content+='<p>实际总额125。</p>';renderer.render([message],{conversation:'c'});
  assert.equal(root.firstChild,article);assert.equal(root.querySelector('p'),paragraph);
});
