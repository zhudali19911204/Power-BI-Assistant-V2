"""Measure response channels without printing either private reasoning or answer data."""
import argparse
import time

from backend.app import create_app
from backend.engine.dax.prompts import build_chat_system_prompt
from backend.powerbi import context_from_dict


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--no-thinking',action='store_true')
    parser.add_argument('--context',action='store_true')
    args=parser.parse_args()
    app=create_app()
    provider=app.state.settings.provider()
    messages=[]
    if args.context:
        model=app.state.powerbi.get(app.state.db.one('SELECT id FROM models ORDER BY created_at DESC LIMIT 1')['id'])
        system=build_chat_system_prompt(context_from_dict(model['context']).serialize_for_prompt())
        messages.append({'role':'system','content':system})
    messages.append({'role':'user','content':'分析下这个模型' if args.context else '分析一个 Power BI 模型时应该先检查什么？请用一句话回答。'})
    extra={'chat_template_kwargs':{'enable_thinking':False}} if args.no_thinking else None
    started=time.monotonic()
    reasoning=content=0
    first=None
    with provider._client.chat.completions.create(model=provider.model,messages=messages,
            max_tokens=2048 if args.context else 256,stream=True,extra_body=extra) as stream:
        for chunk in stream:
            if not chunk.choices:continue
            delta=chunk.choices[0].delta
            reasoning+=len(getattr(delta,'reasoning_content','') or '')
            text=delta.content or ''
            if text and first is None:
                first=time.monotonic()-started
                print('FIRST_CONTENT_SECONDS',round(first,2),flush=True)
            content+=len(text)
    print({'disable_thinking':args.no_thinking,'with_context':args.context,
           'reasoning_chars':reasoning,'content_chars':content,'seconds':round(time.monotonic()-started,1)},flush=True)


if __name__=='__main__':main()
