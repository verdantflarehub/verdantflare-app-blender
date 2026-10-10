"""Run with Linux Python; exercises an isolated real Blender child over HTTP.

Uses temporary project/config/socket and no GPU. Never targets the live desktop.
The input process is absent in this fixture; supervisor interaction is stubbed.
"""
import importlib.util
import json
import os
from pathlib import Path
import signal
import select
import subprocess
import tempfile
import threading
import time
import types
import urllib.error
import urllib.request
import uuid

runtime=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('drain_smoke_control',runtime/'instance-control/server.py')
control=importlib.util.module_from_spec(spec);spec.loader.exec_module(control)

with tempfile.TemporaryDirectory(prefix='blender-drain-smoke-') as temporary:
    root=Path(temporary)
    for field in ('INSTANCE_ID','BLENDER_PROJECT_ID','BLENDER_START_OPERATION_ID','BLENDER_POD_UID'):
        os.environ[field]=str(uuid.uuid4())
    os.environ.update(WORKSPACE_ROOT=str(root/'workspace'),BLENDER_MCP_ROOT=str(runtime),
        BLENDER_MCP_ADAPTER_SOCKET=str(root/'adapter.sock'),BLENDER_USER_CONFIG=str(root/'config'),
        CUDA_VISIBLE_DEVICES='',NVIDIA_VISIBLE_DEVICES='void',LIBGL_ALWAYS_SOFTWARE='true',
        GALLIUM_DRIVER='llvmpipe',__GLX_VENDOR_LIBRARY_NAME='mesa',XDG_SESSION_TYPE='x11')
    os.environ.pop('WAYLAND_DISPLAY',None)
    Path(os.environ['WORKSPACE_ROOT']).mkdir()
    child_script=root/'child.py'
    child_script.write_text('''import importlib.util,os,sys,time
from pathlib import Path
runtime=Path(os.environ['BLENDER_MCP_ROOT'])
spec=importlib.util.spec_from_file_location('drain_child_register',runtime/'blender-mcp-plugin/register.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
import bpy
bpy.context.preferences.view.show_splash=False
''')
    with (root/'child.log').open('w+') as log:
        readfd,writefd=os.pipe()
        display_env=dict(os.environ,LD_LIBRARY_PATH=str(Path(os.environ['BLENDER_TEST_XVFB']).parents[1]/'lib/x86_64-linux-gnu'))
        display=subprocess.Popen([os.environ['BLENDER_TEST_XVFB'],'-displayfd',str(writefd),'-screen','0','800x600x24','-nolisten','tcp','-noreset'],env=display_env,pass_fds=(writefd,),stdout=log,stderr=subprocess.STDOUT)
        os.close(writefd)
        child=None
        server=None
        try:
            assert select.select([readfd],[],[],10)[0],'virtual display did not start'
            number=os.read(readfd,32).decode().strip();os.close(readfd);readfd=None
            assert number.isdigit(),'virtual display allocation failed'
            os.environ['DISPLAY']=':'+number
            child=subprocess.Popen(['blender','--factory-startup','--disable-autoexec','--threads','1','--no-window-focus','--window-geometry','0','0','640','480','--python-exit-code','1','--python',str(child_script)],stdout=log,stderr=subprocess.STDOUT)
            adapter=control.bridge.AdapterClient(os.environ['BLENDER_MCP_ADAPTER_SOCKET'])
            adapter_errors=[]
            original_call=adapter.call
            def observed_call(*args,**kwargs):
                try:return original_call(*args,**kwargs)
                except control.bridge.BridgeError as error:
                    adapter_errors.append({'code':error.code,'message':error.message})
                    raise
            adapter.call=observed_call
            deadline=time.monotonic()+20
            while not adapter.ready() and child.poll() is None and time.monotonic()<deadline:time.sleep(0.05)
            assert child.poll() is None and adapter.ready(),'isolated Blender did not start'
            deadline=time.monotonic()+40
            while True:
                try:
                    generation=adapter.call('scene.get',{},request_id=str(uuid.uuid4()),scene_version=None,deadline_ms=2000)['generation']
                    break
                except control.bridge.BridgeError:
                    if child.poll() is not None or time.monotonic()>=deadline:
                        log.flush();log.seek(0)
                        raise AssertionError(('GUI loop unavailable',child.poll(),log.read()[-4000:])) from None
                    time.sleep(0.25)
            state=types.SimpleNamespace(instance_id=os.environ['INSTANCE_ID'],adapter=adapter,max_body_bytes=65536,authorized=lambda value:value=='Bearer fixture-drain-token')
            def start_server():
                value=control.Server(('127.0.0.1',0),state)
                value.gui.run=lambda action:None
                threading.Thread(target=value.serve_forever,daemon=True).start()
                return value
            server=start_server()
            def request(path,body=None,token='fixture-drain-token'):
                req=urllib.request.Request('http://127.0.0.1:'+str(server.server_port)+path,
                    data=None if body is None else json.dumps(body).encode(),headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
                try:response=urllib.request.urlopen(req,timeout=6)
                except urllib.error.HTTPError as error:response=error
                with response:return response.status,json.loads(response.read())
            command={'instance_id':state.instance_id,'start_operation_id':os.environ['BLENDER_START_OPERATION_ID'],
                'operation_id':str(uuid.uuid4()),'generation':generation,'action':'prepare'}
            assert request('/internal/drain',command,token='wrong')[0]==401
            server.gui.session='held-session'
            assert request('/internal/drain',command)[1]['code']=='GUI_EDIT_LEASE_HELD'
            assert server.drain.read() is None
            server.gui.session=None
            deadline=time.monotonic()+20
            while True:
                code,proof=request('/internal/drain',command)
                if proof.get('frozen') is True or time.monotonic()>=deadline:break
                if code!=200 and proof.get('code') not in {'WORKER_BUSY','DRAIN_BUSY'}:break
                time.sleep(0.25)
            if code!=200 or proof.get('frozen') is not True:
                log.flush();log.seek(0)
                raise AssertionError((proof,adapter_errors,log.read()[-4000:]))
            assert request('/readyz')[0]==200
            assert request('/internal/status')[1]['status']=='draining'
            assert request('/internal/drain',command)==(code,proof)
            rpc={'instance_id':state.instance_id,'generation':generation,'name':'object.create',
                'arguments':{'object_id':'DeniedCube','primitive':'cube','scene_version':proof['proof']['scene_version']},'request_id':str(uuid.uuid4())}
            assert request('/internal/rpc',rpc)[1]['code']=='INSTANCE_DRAINING'
            # Lose all controller memory; frozen evidence and fencing must survive.
            server.shutdown();server.server_close();server=start_server()
            assert request('/internal/drain',dict(command,action='status'))[1]==proof
            code,resumed=request('/internal/drain',dict(command,action='abort'))
            assert code==200 and resumed['phase']=='aborted' and not resumed['frozen'],resumed
            assert request('/internal/drain',dict(command,action='abort'))[1]['phase']=='aborted'
            assert request('/internal/drain',command)[1]['code']=='DRAIN_ABORTED'
            code,scene=request('/internal/rpc',dict(rpc,name='scene.get',arguments={}))
            assert code==200 and scene['generation']==generation,scene
            code,created=request('/internal/rpc',dict(rpc,arguments=dict(rpc['arguments'],object_id='AfterResume')))
            assert code==200 and created['ok'] is True,created
            command=dict(command,operation_id=str(uuid.uuid4()))
            code,proof=request('/internal/drain',command)
            assert code==200 and proof.get('frozen') is True,proof
            code,sealed=request('/internal/drain',dict(command,action='seal'))
            assert code==200 and sealed['phase']=='sealed' and sealed['frozen'],sealed
            assert request('/internal/drain',dict(command,action='seal'))==(code,sealed)
            server.shutdown();server.server_close();server=start_server()
            assert request('/internal/drain',dict(command,action='abort'))[1]['code']=='DRAIN_ALREADY_STOPPING'
            assert request('/internal/drain',dict(command,action='status'))[1]==sealed
            print(json.dumps({'real_blender':True,'authenticated_drain':True,'gui_held_rejected':True,
                'frozen_bytes':True,'controller_restart':True,'resume_same_generation':True,'old_operation_rejected':True,
                'seal_idempotent':True,'sealed_abort_rejected':True}))
        finally:
            if server:server.shutdown();server.server_close()
            if child and child.poll() is None:
                # Only the exact Popen child created by this isolated test.
                child.send_signal(signal.SIGCONT);child.terminate()
                try:child.wait(timeout=5)
                except subprocess.TimeoutExpired:child.kill();child.wait(timeout=5)
            if readfd is not None:os.close(readfd)
            if display.poll() is None:
                display.terminate()
                try:display.wait(timeout=5)
                except subprocess.TimeoutExpired:display.kill();display.wait(timeout=5)
