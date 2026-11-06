// Read-only layout check for a real saved response; never print its business content.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {parseHTML} from 'linkedom';
import {markdown} from '../frontend/markdown.js';
import {patchHtml} from '../frontend/messages.js';

const source=readFileSync(0,'utf8');
const {document}=parseHTML('<html><body><section></section></body></html>');
const element=document.querySelector('section');
patchHtml(element,markdown(source));
const headings=[...element.querySelectorAll('h1,h2,h3,h4,h5,h6')];
assert.ok(headings.length>0);
for(const heading of headings) {
  assert.equal(heading.querySelector('p,ol,ul,pre'),null);
  assert.ok(!heading.textContent.includes('\n'),'Heading must not swallow the next paragraph or list');
}
const numbered=[...element.querySelectorAll('ol')];
assert.ok(numbered.length>0);
const nested=[...element.querySelectorAll('ol ul')];
assert.ok(nested.length>0);
const copy=element.querySelectorAll('.copy-code');
assert.equal(copy.length,element.querySelectorAll('.code-block').length);
console.log(JSON.stringify({result:'PASS',headings:headings.length,numberedLists:numbered.length,
  nestedSuggestionLists:nested.length,listItems:element.querySelectorAll('li').length,codeBlocks:copy.length}));
