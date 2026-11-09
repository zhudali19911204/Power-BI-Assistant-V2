import {escapeHtml as esc} from './markdown.js';
import {icon} from './icons.js';

export const MAX_IMAGES=4;
const MAX_BYTES=8*1024*1024;
const TYPES=new Set(['image/png','image/jpeg','image/webp']);

export function imageGallery(images=[]) {
  return images.length?`<div class="message-images">${images.map(img=>`<button type="button" class="message-image" data-open-image="${esc(img.id)}" aria-label="查看图片 ${esc(img.name)}"><img src="${esc('/api/images/'+img.id)}" alt="${esc(img.name)}" loading="lazy"><span>${esc(img.name)}</span></button>`).join('')}</div>`:'';
}

export function transferImages(transfer) {
  const items=[...(transfer?.items||[])].filter(item=>item.kind==='file'&&item.type?.startsWith('image/'));
  if(items.length)return items.map(item=>item.getAsFile()).filter(Boolean);
  return [...(transfer?.files||[])].filter(file=>file.type?.startsWith('image/'));
}

export async function uploadImage(file) {
  const response=await fetch('/api/images',{method:'POST',headers:{'Content-Type':file.type,
    'X-Image-Name':encodeURIComponent(file.name||'截图.png')},body:file});
  if(!response.ok){const error=await response.json();throw new Error(typeof error.detail==='string'?error.detail:'图片上传失败');}
  return response.json();
}

/** Pending uploads are pinned to the originating view even if the user switches assistants. */
export function mountComposerImages(root,{getView,onChange=()=>{},onNotice=()=>{},upload=uploadImage,
  remove=id=>fetch('/api/images/'+id,{method:'DELETE'}),
  screenshot=async()=>{
    const response=await fetch('/api/desktop/screenshot',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
    const result=await response.json();if(!response.ok)throw new Error(result.detail);return result;
  },createURL=file=>URL.createObjectURL(file),revokeURL=url=>URL.revokeObjectURL(url)}={}) {
  const tray=root.querySelector('[data-image-drafts]'),input=root.querySelector('[data-image-input]');
  let next=0;
  const changed=owner=>{if(getView()===owner)render();onChange(owner);};
  function render() {
    const images=getView().images||[];tray.hidden=!images.length;
    tray.innerHTML=images.map(img=>`<div class="image-draft ${img.status==='uploading'?'uploading':''}" data-image-key="${esc(img.key)}"><button type="button" class="image-draft-preview" ${img.id?`data-open-image="${esc(img.id)}"`:'disabled'}><img src="${esc(img.url)}" alt="${esc(img.name)}"><span>${esc(img.status==='uploading'?'正在添加…':img.name)}</span></button><button type="button" class="image-remove" data-remove-image="${esc(img.key)}" aria-label="移除图片 ${esc(img.name)}">${icon('close')}</button></div>`).join('');
  }
  async function add(files) {
    const owner=getView();owner.images??=[];
    const tasks=[];
    for(const file of files) {
      if(!TYPES.has(file.type)){onNotice('请选择 PNG、JPEG 或 WebP 图片',true);continue;}
      if(file.size>MAX_BYTES){onNotice('单张图片不能超过 8 MB，请缩小截图范围',true);continue;}
      if(owner.images.length>=MAX_IMAGES){onNotice('每条消息最多添加 4 张图片',true);break;}
      const draft={key:'image-'+(++next),name:file.name||'截图.png',url:createURL(file),status:'uploading'};
      owner.images.push(draft);changed(owner);
      tasks.push((async()=>{
        try {
          const result=await upload(file);
          if(!owner.images.includes(draft)){void Promise.resolve(remove(result.id)).catch(()=>{});return;}
          const oldURL=draft.url;Object.assign(draft,result,{status:'ready'});revokeURL(oldURL);
        } catch(error) {
          owner.images=owner.images.filter(img=>img!==draft);revokeURL(draft.url);onNotice(error.message,true);
        } finally {changed(owner);}
      })());
    }
    await Promise.all(tasks);
  }
  root.querySelector('[data-add-images]').onclick=()=>{input.value='';input.click();};
  root.querySelector('[data-screenshot]').onclick=async()=>{
    try {const result=await screenshot();onNotice(result.detail);}
    catch(error){onNotice(error.message,true);}
  };
  input.onchange=()=>{void add([...input.files]);input.value='';};
  root.addEventListener('paste',event=>{
    const files=transferImages(event.clipboardData);if(!files.length)return;
    event.preventDefault();void add(files);
  });
  root.addEventListener('dragover',event=>{if(event.dataTransfer?.types?.includes('Files')){event.preventDefault();root.classList.add('image-drop-target');}});
  root.addEventListener('dragleave',event=>{if(!root.contains(event.relatedTarget))root.classList.remove('image-drop-target');});
  root.addEventListener('drop',event=>{
    const files=transferImages(event.dataTransfer);root.classList.remove('image-drop-target');
    if(event.dataTransfer?.files?.length)event.preventDefault();
    if(files.length)void add(files);
  });
  tray.onclick=event=>{
    const button=event.target.closest('[data-remove-image]');if(!button)return;
    const owner=getView(),image=owner.images?.find(img=>img.key===button.dataset.removeImage);if(!image)return;
    owner.images=owner.images.filter(img=>img!==image);
    if(image.id)void Promise.resolve(remove(image.id)).catch(()=>{});else revokeURL(image.url);
    changed(owner);
  };
  render();return {render,add};
}
