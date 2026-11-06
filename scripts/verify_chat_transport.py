"""Temporary local server + real configured LLM + real frontend NDJSON decoder."""
import socket
import subprocess
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory

import uvicorn

from backend.app import create_app
from backend.powerbi import context_from_dict
from backend.schemas import SettingsInput


def main():
    source=create_app()
    cfg=source.state.settings.raw()
    row=source.state.db.one('SELECT id FROM models ORDER BY created_at DESC LIMIT 1')
    model=source.state.powerbi.get(row['id'])
    root=Path(__file__).resolve().parents[1]
    directory=root/'artifacts'
    directory.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix='transport-',dir=directory) as folder:
        app=create_app(Path(folder))
        app.state.settings.save(SettingsInput(provider=cfg['provider'],base_url=cfg['base_url'],model=cfg['model'],
            api_key=source.state.settings.key(cfg),enable_thinking=cfg['enable_thinking']))
        app.state.powerbi.save('传输测试模型快照','snapshot',None,context_from_dict(model['context']))
        sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen(128)
        url=f'http://127.0.0.1:{sock.getsockname()[1]}'
        server=uvicorn.Server(uvicorn.Config(app,log_level='error',access_log=False))
        worker=threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True)
        worker.start()
        try:
            deadline=time.monotonic()+10
            while not server.started:
                if not worker.is_alive() or time.monotonic()>deadline:raise RuntimeError('Test server failed')
                time.sleep(0.02)
            result=subprocess.run(['node','tests/live_stream.mjs',url],cwd=root,timeout=210)
            if result.returncode:raise RuntimeError('Frontend transport check failed')
        finally:
            server.should_exit=True
            worker.join(5)
            sock.close()


if __name__=='__main__':main()
