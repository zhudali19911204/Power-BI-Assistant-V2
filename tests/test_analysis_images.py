from tests.test_analysis import setup, Provider, plan, result
from tests.test_images import upload


def test_analysis_planner_and_summary_receive_images_and_actual_data_remains_evidence(setup,monkeypatch):
    app,client,conv=setup;img=upload(client)
    provider=Provider([plan(),'模型实际结果为125。[Q1]'])
    monkeypatch.setattr(app.state.settings,'provider',lambda:provider)
    monkeypatch.setattr(app.state.chat.analysis,'execute',lambda *args:result())
    response=client.post(f"/api/conversations/{conv['id']}/messages",json={'content':'分析截图中的指标','image_ids':[img['id']]})
    assert response.status_code==200 and '"type": "done"' in response.text
    assert provider.calls[0][1][-1].images
    assert provider.calls[1][1][0].images
    assert '截图' in provider.calls[0][0] and '不是本轮 Desktop 实际查询结果' in provider.calls[1][0]
    saved=client.get(f"/api/conversations/{conv['id']}/messages").json()
    assert saved[0]['images'][0]['id']==img['id']
    assert saved[-1]['analysis'][0]['rows']==[{'Value':125}]
