// Studio owns navigation, identity, controls and scrolling around this viewport.
document.documentElement.dataset.embedded = 'true';
const style = document.createElement('style');
style.textContent = '#control-panel-app,.fab-container,.v-navigation-drawer{display:none!important}html,body{margin:0;overflow:hidden!important}';
document.head.append(style);
const send = (type, fields = {}) => parent.postMessage({channel:'vf-blender',type,...fields},location.origin);
const resize = () => send('resize',{height:Math.max(240,Math.round(innerWidth*9/16))});
new ResizeObserver(resize).observe(document.documentElement);
let ready=false;
const probe=setInterval(()=>{
 const video=document.getElementById('stream');
 if(video?.readyState>=2 && video.videoWidth>0){if(!ready){ready=true;send('ready')} }
 else if(ready){ready=false;send('error',{message:'画面连接已中断，请重新连接。'})}
},500);
addEventListener('message',event=>{
 if(event.source!==parent||event.origin!==location.origin||event.data?.channel!=='vf-studio')return;
 if(event.data.type==='theme')document.documentElement.dataset.theme=event.data.theme==='light'?'light':'dark';
 if(event.data.type==='route'){send('route',{path:'/desktop'});resize()}
});
// Wheel over the Blender viewport manipulates Blender; Shift+wheel scrolls Studio.
addEventListener('wheel',event=>{if(event.shiftKey){event.preventDefault();event.stopImmediatePropagation();send('wheel',{deltaY:event.deltaY})}}, {capture:true,passive:false});
addEventListener('pagehide',()=>clearInterval(probe));
send('loading');resize();
