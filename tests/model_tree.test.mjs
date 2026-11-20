import test from 'node:test';
import assert from 'node:assert/strict';
import {parseHTML} from 'linkedom';
import {modelTree} from '../frontend/model_tree.js';

const table="Owner's P&L";
const context={tables:{[table]:[{name:'Code]1',dtype:'string'}],Raw:[{name:'Code]1',dtype:'string'}]},
  measures:[{table,name:'Payroll'}]};
const names=[{kind:'table',table,name:table,business_name:'费用明细',aliases:['台账']},
  {kind:'column',table,name:'Code]1',business_name:'分类<代码>',aliases:['费用编码']},
  {kind:'measure',table,name:'Payroll',business_name:'人员费用',aliases:['人力费用','LABOUR']}];
const render=query=>parseHTML('<body>'+modelTree(context,query,names)+'</body>').document;

test('field aliases show the confirmed name and escaped reference and retain value preview',()=>{
  const root=render('费用编码'),hit=root.querySelector('[data-ref]');
  assert.equal(root.querySelectorAll('[data-ref]').length,1);
  assert.equal(hit.querySelector('strong').textContent,'分类<代码>');
  assert.equal(hit.querySelector('small').textContent,"'Owner''s P&L'[Code]]1]");
  assert.equal(hit.dataset.ref,hit.querySelector('small').textContent);
  const preview=root.querySelector('[data-preview-column]');
  assert.deepEqual([preview.dataset.previewTable,preview.dataset.previewColumn],[table,'Code]1']);
});

test('business and technical measure names resolve to the same technical measure',()=>{
  for(const query of ['人员费用',' 人力费用 ','labour','payroll']){
    const hit=render(query).querySelector('[data-ref]');
    assert.equal(hit.dataset.ref,'[Payroll]');
    assert.equal(hit.querySelector('small').textContent,"'Owner''s P&L'[Payroll]");
  }
});

test('table aliases expose the table and its contents, and empty search returns the compact tree',()=>{
  const rows=[...render('台账').querySelectorAll('[data-ref]')];
  assert.deepEqual(rows.map(row=>row.dataset.ref),["'Owner''s P&L'","'Owner''s P&L'[Code]]1]",'[Payroll]']);
  assert.equal(rows[0].querySelector('strong').textContent,'费用明细');
  assert.equal(render('Code]1').querySelectorAll('[data-preview-column]').length,2);
  assert.equal(render('  ').querySelectorAll('details:not([open])').length,2);
  assert.equal(render('缺失字段').querySelector('[data-ref]'),null);
});
