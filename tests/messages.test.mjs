import test from 'node:test';
import assert from 'node:assert/strict';
import {parseHTML} from 'linkedom';
import {patchHtml,createMessageRenderer,createRenderScheduler} from '../frontend/messages.js';
import {markdown} from '../frontend/markdown.js';

function fixture() {
  const {document}=parseHTML('<html><body><div id="scroll"><div id="messages"></div></div></body></html>');
  const root=document.querySelector('#messages'),scroll=document.querySelector('#scroll');
  let scrolls=0;
  Object.assign(scroll,{scrollHeight:1000,scrollTop:600,clientHeight:400});
  scroll.scrollTo=({top,behavior})=>{assert.equal(behavior,'instant');scrolls++;scroll.scrollTop=top;};
  const view=createMessageRenderer({root,scroll,
    label:message=>`<span>${message.role}</span>`,
    content:message=>message.content,
    artifacts:message=>(message.artifacts||[]).map(a=>`<div class="card">${a.name}</div>`).join(''),
    status:message=>message.progress?.stage||'正在生成',
  });
  return {root,scroll,view,document,get scrolls(){return scrolls;}};
}

test('streamed updates preserve article, label, content block and existing text node',()=>{
  const f=fixture();
  const messages=[{id:'u',role:'user',content:'<p>问题</p>'},
    {role:'assistant',content:'<p>你</p>',status:'streaming'}];
  f.view.render(messages,{conversation:'c'});
  const user=f.root.firstChild,article=f.root.lastChild,heading=article.querySelector('.message-label');
  const body=article.querySelector('.message-content'),paragraph=body.firstChild,text=paragraph.firstChild;
  for(const token of ['好','，','这是','流式','输出']) {
    messages[1].content=messages[1].content.replace('</p>',`${token}</p>`);
    f.view.render(messages,{conversation:'c'});
    assert.equal(f.root.firstChild,user);
    assert.equal(f.root.lastChild,article);
    assert.equal(article.querySelector('.message-label'),heading);
    assert.equal(body.firstChild,paragraph);
    assert.equal(paragraph.firstChild,text);
  }
  assert.equal(text.data,'你好，这是流式输出');
  assert.ok(article.classList.contains('streaming'));
});

test('code blocks and copy controls stay mounted as code grows and answer completes',()=>{
  const f=fixture();
  const message={role:'assistant',status:'streaming',content:'<p>公式：</p><div class="code-block"><button class="copy-code">复制</button><pre><code>SUM(</code></pre></div>'};
  f.view.render([message],{conversation:'c'});
  const article=f.root.firstChild,code=article.querySelector('code'),button=article.querySelector('button'),text=code.firstChild;
  let clicks=0;button.addEventListener('click',()=>clicks++);
  message.content=message.content.replace('SUM(','SUM(Sales[Amount])');
  message.status='complete';message.id='saved-id';message.artifacts=[{name:'验证通过'}];
  f.view.render([message],{conversation:'c'});
  assert.equal(f.root.firstChild,article);
  assert.equal(article.querySelector('code'),code);
  assert.equal(code.firstChild,text);
  assert.equal(article.querySelector('button'),button);
  button.click();assert.equal(clicks,1);
  assert.equal(article.dataset.messageId,'saved-id');
  assert.equal(article.querySelector('.card').textContent,'验证通过');
  assert.equal(article.querySelector('.stream-status').hidden,true);
  assert.equal(article.classList.contains('streaming'),false);
});

test('status-only updates and subsequent tokens do not mutate prior answer markup',()=>{
  const f=fixture();
  const previous={role:'assistant',status:'complete',content:'<p>历史回答</p>'};
  const pending={role:'assistant',status:'streaming',content:'<p>新</p>',progress:{stage:'等待'}};
  f.view.render([previous,pending],{conversation:'c'});
  const history=f.root.firstChild.firstChild.nextSibling;
  // Detect any reparse of an already completed answer.
  Object.defineProperty(history,'innerHTML',{set(){throw new Error('History was rewritten');}});
  pending.progress.stage='正在生成';f.view.updateStatus([previous,pending]);
  assert.equal(f.root.lastChild.querySelector('.stream-status').textContent,'正在生成');
  pending.content='<p>新的回答</p>';f.view.render([previous,pending],{conversation:'c'});
  assert.equal(history.textContent,'历史回答');
});

test('reader scrolling up is respected; only a pinned reader follows new content',()=>{
  const f=fixture(),message={role:'assistant',status:'streaming',content:'<p>一</p>'};
  f.view.render([message],{conversation:'c'});
  assert.equal(f.scrolls,1);
  f.scroll.scrollTop=100;
  message.content='<p>一二</p>';f.view.render([message],{conversation:'c'});
  assert.equal(f.scroll.scrollTop,100);assert.equal(f.scrolls,1);
  f.scroll.scrollTop=600;
  message.content='<p>一二三</p>';f.view.render([message],{conversation:'c'});
  assert.equal(f.scrolls,2);
});

test('server repair reset and persisted messages reuse their article',()=>{
  const f=fixture(),message={role:'assistant',status:'streaming',content:'<p>原版本</p>'};
  f.view.render([message],{conversation:'c'});const article=f.root.firstChild;
  message.content='';f.view.render([message],{conversation:'c'});
  message.content='<p>修复版本</p>';f.view.render([message],{conversation:'c'});
  f.view.render([{...message,id:'server-id',status:'complete'}],{conversation:'c'});
  assert.equal(f.root.firstChild,article);
  assert.equal(article.querySelector('.message-content').textContent,'修复版本');
  f.view.render([{...message,id:'other'}],{conversation:'other-conversation'});
  assert.notEqual(f.root.firstChild,article);
});

test('hundreds of tokens share one scheduled paint; flush cancels a trailing redraw',()=>{
  const callbacks=new Map();let id=0,paints=0;
  const scheduler=createRenderScheduler(()=>paints++,{
    request:callback=>{callbacks.set(++id,callback);return id;},cancel:key=>callbacks.delete(key),
  });
  for(let n=0;n<500;n++)scheduler.schedule();
  assert.equal(callbacks.size,1);assert.equal(paints,0);
  const callback=callbacks.values().next().value;callbacks.clear();callback();
  assert.equal(paints,1);
  scheduler.schedule();scheduler.flush();
  assert.equal(paints,2);assert.equal(callbacks.size,0);
});

test('markdown structural changes remove obsolete nodes without replacing the container',()=>{
  const f=fixture(),element=f.document.createElement('div');
  patchHtml(element,'<p>第一段</p><p>第二段</p>');const first=element.firstChild;
  patchHtml(element,'<p>第一段继续</p><pre><code>新代码</code></pre>');
  assert.equal(element.firstChild,first);
  assert.equal(element.lastChild.nodeName,'PRE');
  patchHtml(element,'<p>修订后</p>');
  assert.equal(element.childNodes.length,1);assert.equal(element.firstChild,first);
});

test('real Markdown streaming preserves completed paragraphs and a growing code block',()=>{
  const f=fixture(),element=f.document.createElement('div');
  let source='这是计算说明。\n\n';
  patchHtml(element,markdown(source));const intro=element.firstChild;
  source+='```dax\n合计 = SUM(';
  patchHtml(element,markdown(source));
  const block=element.querySelector('.code-block'),code=element.querySelector('code'),button=element.querySelector('.copy-code');
  for(const token of ["'销售明细'",'[销售额]',')','\n```','\n\n这个公式汇总销售额。']) {
    source+=token;patchHtml(element,markdown(source));
    assert.equal(element.firstChild,intro);
    assert.equal(element.querySelector('.code-block'),block);
    assert.equal(element.querySelector('code'),code);
    assert.equal(element.querySelector('.copy-code'),button);
  }
  assert.equal(code.textContent,"合计 = SUM('销售明细'[销售额])");
  assert.equal(element.lastElementChild.textContent,'这个公式汇总销售额。');
});

test('model-provided HTML is escaped during streaming and never creates executable nodes',()=>{
  const f=fixture(),element=f.document.createElement('div');
  patchHtml(element,markdown('<img src=x onerror=alert(1)>\n\n```html\n<script>bad()</script>\n```'));
  assert.equal(element.querySelector('img'),null);assert.equal(element.querySelector('script'),null);
  assert.ok(element.textContent.includes('<script>bad()</script>'));
});

test('appending a repair retains the entire first version and its DOM nodes',()=>{
  const f=fixture(),message={role:'assistant',status:'streaming',content:'<p>这是第一版模型评价。</p><div class="code-block"><pre><code>Original = 1</code></pre></div>'};
  f.view.render([message],{conversation:'repair'});
  const article=f.root.firstChild,body=article.querySelector('.message-content'),paragraph=body.firstChild,code=body.querySelector('code');
  message.content+='<hr><h3>验证修订 · 第 1 轮</h3><p>以下追加修订内容。</p>';
  f.view.render([message],{conversation:'repair'});
  assert.equal(f.root.firstChild,article);
  assert.equal(body.firstChild,paragraph);
  assert.equal(body.querySelector('code'),code);
  assert.equal(paragraph.textContent,'这是第一版模型评价。');
  assert.ok(body.textContent.includes('以下追加修订内容'));
});
