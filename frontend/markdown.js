import {icon} from './icons.js';
import MarkdownIt from './vendor/markdown-it.mjs';

export const escapeHtml=value=>String(value??'').replace(/[&<>"']/g,c=>({
  '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;',
}[c]));

// Use the locally bundled browser build so desktop use never needs npm or a CDN.
// Model-provided HTML stays literal; only the parser's own elements can enter the DOM.
const parser=new MarkdownIt({html:false,breaks:true,linkify:false,typographer:false});

function codeBlock(code,language='') {
  return `<div class="code-block"><div class="code-heading"><span>${escapeHtml(language||'CODE').toUpperCase()}</span><button class="copy-code">${icon('copy')}复制</button></div><pre><code>${escapeHtml(code.replace(/\n$/,''))}</code></pre></div>\n`;
}
parser.renderer.rules.fence=(tokens,index)=>codeBlock(tokens[index].content,tokens[index].info.trim().split(/\s+/)[0]);
parser.renderer.rules.code_block=(tokens,index)=>codeBlock(tokens[index].content);

// Keep references readable without loading model-supplied remote images into the local app.
parser.renderer.rules.image=(tokens,index,options,env,renderer)=>
  escapeHtml(renderer.renderInlineAsText(tokens[index].children,options,env));
parser.renderer.rules.link_open=(tokens,index,options,env,renderer)=>{
  tokens[index].attrSet('target','_blank');
  tokens[index].attrSet('rel','noopener noreferrer');
  return renderer.renderToken(tokens,index,options);
};

// Table alignment uses classes rather than inline styles, in line with the desktop CSP.
for(const rule of ['th_open','td_open']) {
  parser.renderer.rules[rule]=(tokens,index,options,env,renderer)=>{
    const token=tokens[index],style=token.attrIndex('style');
    if(style>=0) {
      const align=/^text-align:(left|center|right)$/.exec(token.attrs[style][1]);
      token.attrs.splice(style,1);
      if(align)token.attrJoin('class','md-align-'+align[1]);
    }
    return renderer.renderToken(tokens,index,options);
  };
}
parser.renderer.rules.table_open=()=>'<div class="markdown-table"><table>\n';
parser.renderer.rules.table_close=()=>'</table></div>\n';

export function markdown(text) {
  return parser.render(String(text??''));
}
