import base64
import io
import json
import threading
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from PIL import Image

from backend.app import create_app
from backend.engine.llm.base import ChatImage, ChatMessage
from backend.engine.llm.openai_compat import OpenAICompatProvider
from backend.engine.llm.claude import ClaudeProvider
from backend.images import MAX_UPLOAD_BYTES, MAX_HISTORY_IMAGES
from backend.schemas import MessageInput
from backend.streaming import BackgroundStream
from tests.test_api import app, client, conversation, FakeProvider, send


def picture(size=(80,40),fmt='PNG',color='red'):
    output=io.BytesIO();Image.new('RGB',size,color).save(output,format=fmt);return output.getvalue()


def upload(client,name='截图.png',data=None,mime='image/png'):
    from urllib.parse import quote
    response=client.post('/api/images',content=picture() if data is None else data,
                         headers={'Content-Type':mime,'X-Image-Name':quote(name)})
    assert response.status_code==201,response.text
    return response.json()


def test_upload_stores_real_image_and_serves_it_without_exposing_base64(client,app,tmp_path):
    img=upload(client)
    assert img['name']=='截图.png' and img['width']==80 and img['height']==40
    assert 'data' not in img and img['url']=='/api/images/'+img['id']
    response=client.get(img['url']);assert response.status_code==200
    assert response.headers['content-type']=='image/png'
    assert Image.open(io.BytesIO(response.content)).size==(80,40)
    restarted=create_app(tmp_path)
    assert restarted.state.images.get(img['id'])['data']==response.content


@pytest.mark.parametrize('mime,data,status',[
    ('image/svg+xml',b'<svg><script>bad()</script></svg>',415),
    ('text/html',b'<html>bad</html>',415),
    ('image/png',b'not an image',422),
    ('image/png',b'x'*(MAX_UPLOAD_BYTES+1),413),
])
def test_invalid_image_uploads_do_not_enter_database(client,app,mime,data,status):
    response=client.post('/api/images',content=data,headers={'Content-Type':mime})
    assert response.status_code==status
    assert not app.state.db.all('SELECT id FROM message_images')


def test_large_image_is_resized_and_windows_filename_is_sanitized(client):
    img=upload(client,name=r'C:\private\capture.png',data=picture((3000,1000)))
    assert img['name']=='capture.png' and max(img['width'],img['height'])==2048


def test_same_origin_protection_also_covers_raw_uploads_and_screenshot_tool(client,monkeypatch):
    monkeypatch.setattr('backend.app.os.startfile',lambda *args:pytest.fail('must not launch'),raising=False)
    response=client.post('/api/images',content=picture(),headers={'Content-Type':'image/png','Origin':'https://other.example'})
    assert response.status_code==403
    assert client.post('/api/desktop/screenshot',json={},headers={'Origin':'https://other.example'}).status_code==403


def test_screenshot_button_only_launches_windows_snipping_uri(client,monkeypatch):
    calls=[];monkeypatch.setattr('backend.app.os.startfile',lambda uri:calls.append(uri),raising=False)
    response=client.post('/api/desktop/screenshot',json={})
    assert response.status_code==200 and calls==['ms-screenclip:']
    assert 'Ctrl+V' in response.json()['detail']


def test_image_only_message_and_bounded_image_references():
    assert MessageInput(image_ids=['a'*32]).content
    for body in ({'content':' '},{'image_ids':['../escape']},{'image_ids':['a'*32]*2},
                 {'content':'查看','image_ids':[str(i)*32 for i in range(5)]}):
        with pytest.raises(ValueError):MessageInput(**body)


@pytest.mark.parametrize('mode',['dax','mquery'])
def test_images_reach_model_and_followup_history_and_export(client,app,monkeypatch,mode):
    conv,_=conversation(client,mode)
    img=upload(client);provider=FakeProvider(['已查看图片。','继续结合上图。'])
    monkeypatch.setattr(app.state.settings,'provider',lambda:provider)
    events=send(client,conv['id'],'解释截图中的错误',image_ids=[img['id']])
    assert events[-1]['type']=='done'
    received=provider.calls[0][1][-1]
    assert received.images[0].mime_type=='image/png'
    assert Image.open(io.BytesIO(base64.b64decode(received.images[0].data))).size==(80,40)
    messages=client.get(f"/api/conversations/{conv['id']}/messages").json()
    assert messages[0]['images'][0]['id']==img['id'] and 'data' not in messages[0]['images'][0]
    send(client,conv['id'],'这张截图还说明了什么')
    assert provider.calls[1][1][0].images[0].data==received.images[0].data
    assert 'data:image/png;base64,' in client.get(f"/api/conversations/{conv['id']}/export").text
    assert client.delete(img['url']).status_code==409
    assert client.delete(f"/api/conversations/{conv['id']}").status_code==200
    assert client.get(img['url']).status_code==404


def test_image_only_send_and_pending_image_removal(client,app,monkeypatch):
    conv,_=conversation(client);img=upload(client)
    monkeypatch.setattr(app.state.settings,'provider',lambda:FakeProvider(['已查看图片']))
    response=client.post(f"/api/conversations/{conv['id']}/messages",json={'image_ids':[img['id']]})
    assert json.loads(response.text.splitlines()[-1])['type']=='done'
    pending=upload(client)
    assert client.delete(pending['url']).status_code==200
    assert client.get(pending['url']).status_code==404


def test_sent_image_cannot_be_rebound_to_another_conversation(client,app,monkeypatch):
    conv,_=conversation(client);other,_=conversation(client);img=upload(client)
    monkeypatch.setattr(app.state.settings,'provider',lambda:FakeProvider(['已查看']))
    send(client,conv['id'],'看图',image_ids=[img['id']])
    response=client.post(f"/api/conversations/{other['id']}/messages",json={'content':'reuse','image_ids':[img['id']]})
    assert response.status_code==409
    assert not app.state.db.all('SELECT id FROM messages WHERE conversation_id=?',(other['id'],))


def test_unknown_image_fails_before_saving_user_message(client,app,monkeypatch):
    conv,_=conversation(client)
    monkeypatch.setattr(app.state.settings,'provider',lambda:pytest.fail('no model call'))
    response=client.post(f"/api/conversations/{conv['id']}/messages",json={'content':'看图','image_ids':['a'*32]})
    assert response.status_code==404 and not app.state.db.all('SELECT id FROM messages')


def test_binding_failure_rolls_back_user_message_atomically(client,app):
    conv,_=conversation(client);img=upload(client)
    with pytest.raises(HTTPException):app.state.images.save_user(conv['id'],'test',[img['id'],'b'*32])
    assert not app.state.db.all('SELECT id FROM messages')
    assert app.state.images.get(img['id'])['message_id'] is None


def test_message_and_model_image_order_follow_selection_instead_of_upload_completion(client,app):
    conv,_=conversation(client)
    second=upload(client,name='第二张.png',data=picture(color='blue'))
    first=upload(client,name='第一张.png',data=picture(color='red'))
    mid=app.state.images.save_user(conv['id'],'按第一张和第二张分析',[first['id'],second['id']])
    assert [img['name'] for img in app.state.images.metadata(mid)]==['第一张.png','第二张.png']
    messages=app.state.images.llm_messages([],mid,'查看')
    colors=[Image.open(io.BytesIO(base64.b64decode(img.data))).getpixel((0,0)) for img in messages[0].images]
    assert colors==[(255,0,0),(0,0,255)]


def test_multimodal_provider_rejection_preserves_images_and_gives_actionable_error(client,app,monkeypatch):
    conv,_=conversation(client);img=upload(client)
    BadRequestError=type('BadRequestError',(Exception,),{})
    class Provider:
        def stream(self,*args,**kwargs):
            raise BadRequestError('private endpoint error');yield
    monkeypatch.setattr(app.state.settings,'provider',lambda:Provider())
    events=send(client,conv['id'],'看图',image_ids=[img['id']])
    assert events[-1]['type']=='error' and '支持图片输入' in events[-1]['text']
    assert 'private endpoint' not in events[-1]['text']
    assert client.get(img['url']).status_code==200


def test_openai_and_anthropic_request_shapes_contain_actual_image_bytes():
    message=ChatMessage('user','分析截图',[ChatImage('image/png','encoded-image')])
    openai=OpenAICompatProvider._to_messages('system',[message])
    assert openai[1]['content'][1]['image_url']['url']=='data:image/png;base64,encoded-image'
    anthropic=ClaudeProvider._to_messages([message])
    assert anthropic[0]['content'][0]['source']=={'type':'base64','media_type':'image/png','data':'encoded-image'}
    assert anthropic[0]['content'][1]['text']=='分析截图'
    assert OpenAICompatProvider._to_messages('',[ChatMessage('user','text')])[0]['content']=='text'


def test_history_has_image_budget_without_claiming_omitted_images_were_seen(client,app):
    conv,_=conversation(client);rows=[]
    for i in range(3):
        images=[upload(client)['id'] for _ in range(4)]
        mid=app.state.images.save_user(conv['id'],str(i),images)
        rows.append({'id':mid,'role':'user','content':str(i)})
    messages=app.state.images.llm_messages(rows[:-1],rows[-1]['id'],'latest')
    assert sum(len(m.images) for m in messages)==MAX_HISTORY_IMAGES
    assert '未重新发送' in messages[0].content and not messages[0].images
    assert len(messages[-1].images)==4


def test_stream_disconnect_keeps_image_message_and_background_result(client,app,monkeypatch):
    conv,_=conversation(client);img=upload(client);resume=threading.Event()
    class Provider:
        def stream(self,system,messages,**kwargs):
            assert messages[-1].images
            yield '已看到图片。'
            assert resume.wait(3)
            yield '后台完成。'
    monkeypatch.setattr(app.state.settings,'provider',lambda:Provider())
    task=BackgroundStream(app.state.chat.begin(conv['id'],MessageInput(content='看图',image_ids=[img['id']]),app.state.settings))
    reader=task.events()
    try:
        while json.loads(next(reader))['type']!='delta':pass
        reader.close();resume.set();assert task.finished.wait(3)
        saved=app.state.chat.messages(conv['id'])
        assert saved[0]['images'][0]['id']==img['id']
        assert saved[-1]['content']=='已看到图片。后台完成。'
    finally:resume.set()


def test_upload_strips_metadata_and_rejects_oversized_decoded_dimensions(client,app,monkeypatch):
    from PIL.PngImagePlugin import PngInfo
    info=PngInfo();info.add_text('private','do not keep metadata')
    output=io.BytesIO();Image.new('RGB',(20,20),'white').save(output,format='PNG',pnginfo=info)
    img=upload(client,data=output.getvalue())
    with Image.open(io.BytesIO(client.get(img['url']).content)) as normalized:
        assert 'private' not in normalized.info
    monkeypatch.setattr('backend.images.MAX_IMAGE_PIXELS',100)
    response=client.post('/api/images',content=picture((20,20)),headers={'Content-Type':'image/png'})
    assert response.status_code==422
