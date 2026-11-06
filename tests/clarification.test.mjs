import test from 'node:test';
import assert from 'node:assert/strict';
import {parseHTML} from 'linkedom';
import {clarificationCard,clarificationReply} from '../frontend/clarification.js';
import {createMessageRenderer} from '../frontend/messages.js';

function message(extra={}) {
  return {id:'question-1',role:'assistant',status:'complete',content:'持久化问题及选项',clarification:{
    topic:'费用分类',question:'personnel 费用采用哪个分类字段？',context:'成本中心：0257',answered:false,
    options:[{id:'A',label:'按费用类别',value:'按 CE Category 筛选人员费用',detail:'Dim_Cost Element[CE Category]'},
             {id:'B',label:'按一级分类',value:'按 Class 1 筛选人员费用',detail:'Dim_Cost Element[Class 1]'}],
  },clarificationDraft:{},clarificationLocked:false,...extra};
}
const rootFor=html=>parseHTML('<html><body><main>'+html+'</main></body></html>').document.querySelector('main');

test('only one question is presented, with explicit choices, full meaning and a custom alternative',()=>{
  const root=rootFor(clarificationCard(message()));
  assert.equal(root.querySelectorAll('h3').length,1);
  assert.equal(root.querySelectorAll('[data-clarification-choice]').length,3);
  assert.equal(root.querySelectorAll('[checked]').length,0);
  assert.ok(root.textContent.includes('按 CE Category 筛选人员费用'));
  assert.ok(root.textContent.includes('自定义口径'));
  assert.equal(root.querySelector('textarea').getAttribute('maxlength'),'4000');
  assert.ok(root.querySelector('[data-confirm-clarification]'));
});

test('one option and supplemental text produce a reply tied to the exact question',()=>{
  const reply=clarificationReply(message(),{optionId:'B',supplement:'  取值 personnel，0257 保留前导零  '});
  assert.deepEqual(reply.clarification_reply,{message_id:'question-1',option_id:'B',supplement:'取值 personnel，0257 保留前导零'});
  assert.ok(reply.content.includes('按 Class 1 筛选人员费用'));
  assert.ok(!reply.content.includes('按 CE Category 筛选人员费用'));
});

test('custom and supplement-only answers are accepted but an empty confirmation is rejected',()=>{
  assert.throws(()=>clarificationReply(message(),{}),/选项|补充/);
  assert.throws(()=>clarificationReply(message(),{optionId:'custom'}),/口径|补充/);
  for(const optionId of [undefined,'custom']) {
    const reply=clarificationReply(message(),{optionId,supplement:'采用 Class 2 的人员费用'});
    assert.equal(reply.clarification_reply.option_id,null);
    assert.ok(reply.content.includes('采用 Class 2 的人员费用'));
  }
});

test('question content, choices and user supplements cannot create active HTML',()=>{
  const m=message();m.clarification.question='<img src=x onerror=1>?';
  m.clarification.options[0].label='<script>alert(1)</script>';
  m.clarification.options[0].value='<iframe src=evil>';
  m.clarificationDraft={optionId:'A',supplement:'</textarea><img src=x onerror=1>'};
  const root=rootFor(clarificationCard(m));
  assert.equal(root.querySelector('script,img,iframe'),null);
  assert.ok(clarificationCard(m).includes('&lt;/textarea&gt;&lt;img src=x onerror=1&gt;'));
  assert.ok(root.textContent.includes('<script>alert(1)</script>'));
});

test('already answered questions are read-only and cannot submit a stale answer',()=>{
  const m=message();m.clarification.answered=true;m.clarificationLocked=true;
  const root=rootFor(clarificationCard(m));
  assert.ok(root.querySelector('fieldset').hasAttribute('disabled'));
  assert.equal(root.querySelector('[data-confirm-clarification]'),null);
  assert.ok(root.textContent.includes('已回复'));
});

test('incremental rendering retains the question controls when the choice and supplement change',()=>{
  const {document}=parseHTML('<html><body><div id="scroll"><main></main></div></body></html>');
  const root=document.querySelector('main'),scroll=document.querySelector('#scroll');
  Object.assign(scroll,{scrollHeight:1000,scrollTop:600,clientHeight:400});scroll.scrollTo=()=>{};
  const renderer=createMessageRenderer({root,scroll,label:()=>'',content:()=>'',artifacts:clarificationCard,status:()=>''});
  const m=message();renderer.render([m],{conversation:'c'});
  const card=root.querySelector('[data-clarification]'),textarea=root.querySelector('textarea');
  m.clarificationDraft={optionId:'B',supplement:'保留补充说明'};renderer.render([m],{conversation:'c'});
  assert.equal(root.querySelector('[data-clarification]'),card);
  assert.equal(root.querySelector('textarea'),textarea);
  assert.equal(textarea.textContent,'保留补充说明');
  assert.ok(root.querySelector('input[value="B"]').hasAttribute('checked'));
  m.clarificationLocked=true;renderer.render([m],{conversation:'c'});
  assert.equal(root.querySelector('textarea'),textarea);
  assert.ok(root.querySelector('fieldset').hasAttribute('disabled'));
});
