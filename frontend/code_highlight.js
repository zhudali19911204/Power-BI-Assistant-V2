import {escapeHtml as esc} from './html.js';

const daxKeywords=new Set('VAR RETURN DEFINE EVALUATE MEASURE COLUMN TABLE ORDER BY ASC DESC START AT IN NOT AND OR TRUE FALSE'.split(' '));
const mKeywords=new Set('let in each if then else try otherwise catch error as is type meta section shared and or not true false null nullable optional'.split(' '));
const mTypes=new Set('any anynonnull binary date datetime datetimezone duration function list logical none number record table text time'.split(' '));

export function codeLanguage(language='') {
  const tag=String(language).trim().toLowerCase();
  if(['dax','dax-column','dax-measure','dax-table'].includes(tag))return 'dax';
  if(['m','powerquery','power-query','pq','mscript'].includes(tag))return 'm';
  return '';
}

// A presentation lexer, not a validator. Every source character is retained, including incomplete
// streamed strings/comments. M lexical rules: https://learn.microsoft.com/powerquery-m/m-spec-lexical-structure
export function highlightCode(value,language) {
  const source=String(value??''),mode=codeLanguage(language);
  if(!mode||source.length>100000)return esc(source);
  const word=/#?[\p{L}_][\p{L}\p{N}\p{M}_]*(?:\.[\p{L}_][\p{L}\p{N}\p{M}_]*)*/uy;
  const number=/(?:0[xX][\da-fA-F]+|\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?)/y;
  const parts=[];
  let i=0,tokens=0;
  const quotedEnd=(start,closing)=>{
    let end=start;
    while(end<source.length) {
      if(source[end]===closing) {
        if(source[end+1]===closing){end+=2;continue;}
        return end+1;
      }
      end++;
    }
    return end;
  };
  while(i<source.length) {
    // Bound DOM growth for unusually long output; the remaining text still displays and copies fully.
    if(tokens>=12000){parts.push(esc(source.slice(i)));break;}
    const start=i;
    let kind='',match;
    if(source.startsWith('//',i)||(mode==='dax'&&source.startsWith('--',i))) {
      i+=2;while(i<source.length&&!/[\r\n\u0085\u2028\u2029]/u.test(source[i]))i++;
      kind='comment';
    } else if(source.startsWith('/*',i)) {
      const end=source.indexOf('*/',i+2);i=end<0?source.length:end+2;kind='comment';
    } else if(mode==='m'&&source.startsWith('#"',i)) {
      i=quotedEnd(i+2,'"');kind='identifier';
    } else if(mode==='m'&&source.startsWith('#!"',i)) {
      i=quotedEnd(i+3,'"');kind='string';
    } else if(source[i]==='"') {
      i=quotedEnd(i+1,'"');kind='string';
    } else if(mode==='dax'&&(source[i]==="'"||source[i]==='[')) {
      i=quotedEnd(i+1,source[i]==='['?']':"'");kind='identifier';
    } else {
      word.lastIndex=i;number.lastIndex=i;
      if((match=word.exec(source))) {
        i=word.lastIndex;
        const text=match[0];
        if(mode==='dax'?daxKeywords.has(text.toUpperCase()):mKeywords.has(text))kind='keyword';
        else if(mode==='m'&&mTypes.has(text))kind='type';
        else {
          let next=i;while(next<source.length&&/\s/u.test(source[next]))next++;
          if(source[next]==='(')kind='function';
          else if(mode==='m'||source[next]==='[')kind='identifier';
        }
      } else if((match=number.exec(source))) {i=number.lastIndex;kind='number';}
      else i++;
    }
    const text=esc(source.slice(start,i));
    parts.push(kind?`<span class="code-token-${kind}">${text}</span>`:text);
    tokens++;
  }
  return parts.join('');
}
