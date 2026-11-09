"""Test the selected model with a synthetic color image only. No Power BI data is sent."""
import base64
import io
import json

from PIL import Image, ImageDraw

from backend.app import create_app
from backend.engine.llm.base import ChatImage, ChatMessage
from backend.streaming import completion_events


def main():
    app=create_app();provider=app.state.settings.provider();result='';request_count=0
    canvas=Image.new('RGB',(120,60),'red');ImageDraw.Draw(canvas).rectangle((60,0,119,59),fill='blue')
    output=io.BytesIO();canvas.save(output,format='PNG')
    message=ChatMessage('user','只描述图片左半边和右半边的颜色，中文一句话。',
                        [ChatImage('image/png',base64.b64encode(output.getvalue()).decode('ascii'))])
    try:
        request_count=1
        for event in completion_events(provider,'请准确查看附图，不要猜测。',[message],max_tokens=256,timeout=120):
            if event['type']=='delta':result+=event['text']
        passed=('红' in result and '蓝' in result)
        print(json.dumps({'vision_test':'pass' if passed else 'unconfirmed','model':provider.model,
            'image':'synthetic-red-left-blue-right','reply':result[:160],'requests':request_count,
            'powerbi_data_sent':False,'model_writes':0},ensure_ascii=True))
    except Exception as exc:
        print(json.dumps({'vision_test':'failed','model':provider.model,'error_type':type(exc).__name__,
                          'requests':request_count,'powerbi_data_sent':False},ensure_ascii=True))
    finally:
        provider._client.close()


if __name__=='__main__':main()
