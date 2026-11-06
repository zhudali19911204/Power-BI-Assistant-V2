import test from 'node:test';
import assert from 'node:assert/strict';
import {parseHTML} from 'linkedom';
import {markdown} from '../frontend/markdown.js';
import {patchHtml} from '../frontend/messages.js';

const section=()=>parseHTML('<html><body><section></section></body></html>').document.querySelector('section');
const render=source=>{const element=section();patchHtml(element,markdown(source));return element;};

// Match the failure in the screenshot: headings immediately followed by numbered and nested lists.
const screenshotReply=`### 2. 模型结构优缺点分析

#### ✅ 优点
1.  **关系清晰**：事实表通过 @@Std Price Key@@ 关联标准价格。
2.  **度量值逻辑严密**：标准价值和实际金额构成计算差异的基础。
3.  **加权平均逻辑**：处理多物料单价不可直接求和的问题。

#### ⚠️ 潜在风险与优化建议
1.  **单价逻辑的复杂性**：
    *   现有代码在单一值和非单一值分支使用不同逻辑。
    *   **建议**：直接基于事实表计算加权平均单价。
2.  **标准价格表的粒度**：
    *   检查每个期间是否只有一个标准价。
3.  **迭代计算的性能**：
    *   在大型数据集中，对每个月份计算复杂度量可能较慢。
4.  **当前期间的定义**：
    *   明确切片器和默认时间范围。

### 3. 关键度量值解释与优化建议

这一节是普通正文，不能被当成标题。`.replaceAll('@@','`');

test('screenshot regression: headings, numbered items and child suggestions remain distinct blocks',()=>{
  const element=render(screenshotReply);
  assert.equal(element.querySelectorAll('h3').length,2);
  assert.deepEqual([...element.querySelectorAll('h4')].map(h=>h.textContent),['✅ 优点','⚠️ 潜在风险与优化建议']);
  const lists=[...element.children].filter(e=>e.tagName==='OL');
  assert.equal(lists.length,2);
  assert.equal(lists[0].children.length,3);
  assert.equal(lists[1].children.length,4);
  assert.equal(lists[1].children[0].querySelectorAll('ul > li').length,2);
  assert.equal(lists[1].children[1].querySelectorAll('ul > li').length,1);
  assert.equal(element.querySelector('h4').querySelector('ol'),null);
  assert.equal(element.lastElementChild.tagName,'P');
  assert.equal(lists[0].firstElementChild.querySelector('strong').textContent,'关系清晰');
  assert.equal(lists[0].firstElementChild.querySelector('code').textContent,'Std Price Key');
});

test('all heading levels retain semantic hierarchy instead of collapsing into one heading',()=>{
  const element=render('# 大标题\n说明正文\n\n## 第一节\n### 子节\n#### 小标题\n正文\n\n##### 注释\n###### 细节');
  for(let level=1;level<=6;level++)assert.equal(element.querySelectorAll('h'+level).length,1);
  assert.equal(element.querySelector('h1').textContent,'大标题');
  assert.equal(element.querySelectorAll('p').length,2);
});

test('ordered list numbering, multiline items and nested bullets are preserved',()=>{
  const element=render('3. **第一条**：\n   补充说明。\n   - 子建议一\n   - 子建议二\n4. **第二条**：独立内容。');
  const ordered=element.querySelector('ol');
  assert.equal(ordered.getAttribute('start'),'3');
  assert.equal(ordered.children.length,2);
  assert.equal(ordered.firstElementChild.querySelectorAll('ul > li').length,2);
  assert.ok(ordered.firstElementChild.textContent.includes('补充说明'));
});

test('tables, quotes and emphasis render readable blocks with CSP-compatible alignment',()=>{
  const element=render('> **注意**：此处是建议。\n\n| 指标 | 结果 |\n| :--- | ---: |\n| 销售额 | 100 |\n\n*补充说明*\n\n---');
  assert.equal(element.querySelector('blockquote strong').textContent,'注意');
  assert.equal(element.querySelectorAll('.markdown-table th').length,2);
  assert.equal(element.querySelector('td.md-align-right').textContent,'100');
  assert.equal(element.querySelector('[style]'),null);
  assert.equal(element.querySelector('em').textContent,'补充说明');
  assert.ok(element.querySelector('hr'));
});

test('unfinished streamed code fences keep their code container and copy button',()=>{
  const element=render('### 公式\n\n```dax\n总额 = SUM(');
  const heading=element.querySelector('h3'),code=element.querySelector('code'),button=element.querySelector('.copy-code');
  patchHtml(element,markdown('### 公式\n\n```dax\n总额 = SUM(\'销售\'[金额])\n```\n\n说明正文。'));
  assert.equal(element.querySelector('h3'),heading);
  assert.equal(element.querySelector('code'),code);
  assert.equal(element.querySelector('.copy-code'),button);
  assert.equal(element.lastElementChild.tagName,'P');
});

test('streaming a structured analysis does not replace already displayed headings and list items',()=>{
  const element=render('### 分析\n\n1. **关系清晰**：说明。\n2. **下一条**：');
  const heading=element.querySelector('h3'),list=element.querySelector('ol'),first=list.firstElementChild;
  const tail='建议。\n   - 检查键值\n   - 确认筛选方向\n3. **最后一条**：完成。';
  let source='### 分析\n\n1. **关系清晰**：说明。\n2. **下一条**：';
  for(const token of tail) {
    source+=token;patchHtml(element,markdown(source));
    assert.equal(element.querySelector('h3'),heading);
    assert.equal(element.querySelector('ol'),list);
    assert.equal(list.firstElementChild,first);
  }
  assert.equal(list.children.length,3);
});

test('raw HTML, executable URLs and remote images cannot create active model-supplied content',()=>{
  const element=render('<script>alert(1)</script>\n\n<img src=x onerror=alert(1)>\n\n[bad](javascript:alert(1))\n\n![说明](https://remote.example/pixel)\n\n[参考](https://example.com)');
  assert.equal(element.querySelector('script'),null);
  assert.equal(element.querySelector('img'),null);
  assert.equal(element.querySelector('[onerror]'),null);
  const links=[...element.querySelectorAll('a')];
  assert.equal(links.length,1);
  assert.equal(links[0].getAttribute('href'),'https://example.com');
  assert.equal(links[0].getAttribute('target'),'_blank');
  assert.equal(links[0].getAttribute('rel'),'noopener noreferrer');
});
