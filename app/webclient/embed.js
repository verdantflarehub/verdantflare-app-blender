// Studio owns navigation, identity, controls and scrolling around this viewport.
document.documentElement.dataset.embedded = 'true';
const style = document.createElement('style');
style.textContent = '#control-panel-app,#control-panel-loader,.fab-container,.v-navigation-drawer{display:none!important}html,body{margin:0;overflow:hidden!important}';
document.head.append(style);
const session = location.pathname.split('/')[4];
const send = (type, fields = {}) => parent.postMessage({channel:'vf-blender',type,session,...fields},location.origin);
const resize = () => send('resize',{height:Math.max(240,Math.round(innerWidth*9/16))});
new ResizeObserver(resize).observe(document.documentElement);
// Upstream blurs the native video element to suppress its media shortcuts. In an
// iframe that can return keyboard focus to Studio. Keep focus in this document.
document.addEventListener('pointerdown', event => {
 if(event.target instanceof Element && event.target.closest('#stream')) {
  setTimeout(()=>{
   window.focus();
   document.body.tabIndex=-1;
   document.body.focus({preventScroll:true});
  },0);
 }
},true);
let ready=false;
const peers = [];
const NativePeer = window.RTCPeerConnection;
window.RTCPeerConnection = class extends NativePeer {
 constructor(config, ...rest) {
  // Upstream local preferences must not enable direct/public fallback.
  super({...config,iceTransportPolicy:'relay'}, ...rest);
  peers.push(this);
  this.addEventListener('connectionstatechange',()=>{
   if(['failed','closed','disconnected'].includes(this.connectionState))send('error');
  });
 }
 setConfiguration(config) { return super.setConfiguration({...config,iceTransportPolicy:'relay'}); }
};
const probe=setInterval(()=>{
 const video=document.getElementById('stream');
 if(video?.readyState>=2 && video.videoWidth>0 && peers.some(p=>p.connectionState==='connected')){if(!ready){ready=true;send('ready')} }
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
