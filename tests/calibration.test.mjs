import test from 'node:test';
import assert from 'node:assert/strict';
import {parseHTML} from 'linkedom';
import {mountCalibrationEditor} from '../frontend/calibration.js';

const context={tables:{客户:[{name:'地区',dtype:'string'},{name:'启用',dtype:'boolean'}],日期:[{name:'年份',dtype:'int64'}]}};
function fixture(initial={}) {
  const {document,window}=parseHTML('<html><body><form></form></body></html>');
  const form=document.querySelector('form');
  const editor=mountCalibrationEditor(form,context,initial);
  return {form,editor,window};
}
const filters=form=>[...form.querySelectorAll('[data-calibration-filter]')];
function choose(f,select,value) {
  const options=[...select.querySelectorAll('option')];
  for(const option of options)option.selected=false;
  options.find(option=>option.value===value).selected=true;
  select.dispatchEvent(new f.window.Event('change',{bubbles:true}));
}
function setTarget(form,expected='100',tolerance='0.01') {
  form.querySelector('[data-calibration-expected]').value=expected;
  form.querySelector('[data-calibration-tolerance]').value=tolerance;
}

test('multiple filters produce exactly one expected result and tolerance',()=>{
  const point={filters:[{table:'客户',column:'地区',value:'华东'},{table:'日期',column:'年份',value:2026}],expected:1200,tolerance:0.1};
  const f=fixture(point);
  assert.equal(filters(f.form).length,2);
  assert.equal(f.form.querySelectorAll('[data-calibration-expected]').length,1);
  assert.equal(f.form.querySelectorAll('[data-calibration-tolerance]').length,1);
  assert.deepEqual(f.editor.read(),point);
  assert.ok(f.form.textContent.includes('所有条件同时满足'));
});

test('adding and removing filters never duplicate or clear the single target',()=>{
  const f=fixture();setTarget(f.form,'42','0');
  f.form.querySelector('[data-add-filter]').click();
  f.form.querySelector('[data-add-filter]').click();
  assert.equal(filters(f.form).length,2);
  filters(f.form)[0].querySelector('[data-remove-filter]').click();
  assert.equal(filters(f.form).length,1);
  assert.equal(filters(f.form)[0].querySelector('[data-filter-label]').textContent,'条件 1');
  assert.equal(f.form.querySelectorAll('[data-calibration-expected]').length,1);
  assert.equal(f.form.querySelector('[data-calibration-expected]').value,'42');
  filters(f.form)[0].querySelector('[data-remove-filter]').click();
  assert.deepEqual(f.editor.read(),{filters:[],expected:42,tolerance:0});
  assert.equal(f.form.querySelector('[data-no-filters]').hidden,false);
});

test('changing tables updates only that filter and requires a valid column',()=>{
  const f=fixture({filters:[{table:'客户',column:'地区',value:'华东'}],expected:20,tolerance:0.01});
  const row=filters(f.form)[0];
  choose(f,row.querySelector('[data-filter-table]'),'日期');
  assert.equal(row.querySelector('[data-filter-column]').querySelectorAll('option').length,2);
  assert.throws(()=>f.editor.read(),/表和字段/);
  choose(f,row.querySelector('[data-filter-column]'),'年份');
  choose(f,row.querySelector('[data-filter-kind]'),'number');
  row.querySelector('[data-filter-value]').value='2026';
  assert.deepEqual(f.editor.read().filters,[{table:'日期',column:'年份',value:2026}]);
});

test('numeric zero, boolean false and blank filters retain their types',()=>{
  const point={filters:[{table:'日期',column:'年份',value:0},{table:'客户',column:'启用',value:false},{table:'客户',column:'地区',value:null}],expected:0,tolerance:0};
  const f=fixture(point);
  assert.deepEqual(f.editor.read(),point);
  assert.equal(filters(f.form)[2].querySelector('[data-filter-value]').disabled,true);
});

test('invalid expected values, negative tolerance and partial conditions are rejected',()=>{
  const f=fixture();
  assert.throws(()=>f.editor.read(),/已知正确结果/);
  setTarget(f.form,'100','-1');assert.throws(()=>f.editor.read(),/不能为负数/);
  setTarget(f.form);f.form.querySelector('[data-add-filter]').click();
  assert.throws(()=>f.editor.read(),/表和字段/);
});

test('filter growth stops at ten while result inputs remain singular',()=>{
  const f=fixture();
  for(let i=0;i<11;i++)f.form.querySelector('[data-add-filter]').click();
  assert.equal(filters(f.form).length,10);
  assert.equal(f.form.querySelector('[data-add-filter]').disabled,true);
  assert.equal(f.form.querySelectorAll('[data-calibration-expected]').length,1);
});
