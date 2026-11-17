import json

from tests.test_semantics import setup, ready
from tests.test_images import upload
from tests.test_analysis import Provider, plan, result
from tests.test_clarifications import clarify, reply


def test_semantic_clarification_keeps_user_image_for_the_next_turn(setup,monkeypatch):
    app,client,model,ctx,conv=setup;ready(app,model,ctx);img=upload(client)
    provider=Provider([clarify(),plan(),'实际总额125。[Q1]'])
    monkeypatch.setattr(app.state.settings,'provider',lambda:provider)
    monkeypatch.setattr(app.state.chat.analysis,'execute',lambda *a:result())
    response=client.post(f"/api/conversations/{conv['id']}/messages",json={'content':'分析2026年0257人员费用','image_ids':[img['id']]})
    assert response.status_code==200
    events=[json.loads(line) for line in response.text.splitlines()]
    assert any(event['type']=='clarification' for event in events)
    saved=client.get(f"/api/conversations/{conv['id']}/messages").json()
    assert saved[0]['images'][0]['id']==img['id']
    assert client.get(img['url']).status_code==200
    assert provider.calls[0][1][-1].images
    assert reply(client,conv,saved[-1]).status_code==200
    assert any(message.images for message in provider.calls[1][1] if message.role=='user')
