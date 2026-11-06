// Decode NDJSON independently of network chunk boundaries, including split UTF-8 characters.
export async function readEvents(response, onEvent, {idleTimeout=90000}={}) {
  const reader=response.body.getReader(), decoder=new TextDecoder();
  let buffer='',terminal=false;
  const process=line=>{
    if(!line.trim())return;
    const event=JSON.parse(line);
    if(event.type==='done'||event.type==='error'||event.type==='background')terminal=true;
    onEvent(event);
  };
  try {
    while(true) {
      let timer;
      const timeout=new Promise((_,reject)=>{timer=setTimeout(()=>reject(new Error('长时间未收到服务状态，请检查本地服务后重新发送')),idleTimeout);});
      let next;
      try {next=await Promise.race([reader.read(),timeout]);} finally {clearTimeout(timer);}
      if(next.done)break;
      buffer+=decoder.decode(next.value,{stream:true});
      const lines=buffer.split('\n');buffer=lines.pop();lines.forEach(process);
    }
    buffer+=decoder.decode();if(buffer.trim())process(buffer);
    if(!terminal)throw new Error('响应已中断，请重新发送；已收到的内容保存在会话中');
  } finally {
    // Closing the reader also closes an unfinished fetch when decoding or timeout fails.
    await reader.cancel().catch(()=>{});
    reader.releaseLock();
  }
}
