const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const path=require('node:path');

test('desktop ready waits for input, fails closed, and recovers after reconnect',()=>{
 const messages=[];let tick;
 class Peer extends EventTarget {constructor(){super();this.connectionState='new';} setConfiguration(){} }
 const video={readyState:0,videoWidth:0};
 const context={document:{documentElement:{dataset:{}},createElement:()=>({}),head:{append(){}},addEventListener(){},getElementById:()=>video},
  location:{pathname:'/apps/blender/test/session/',origin:'https://studio.invalid'},parent:{postMessage:m=>messages.push(m)},innerWidth:1000,
  ResizeObserver:class{observe(){}},setInterval:fn=>{tick=fn;return 1;},clearInterval(){},addEventListener(){},RTCPeerConnection:Peer};
 context.window=context;
 vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../app/webclient/embed.js'),'utf8'),context);
 const peer=new context.RTCPeerConnection({});peer.connectionState='connected';video.readyState=4;video.videoWidth=1600;
 tick();assert.equal(messages.filter(m=>m.type==='ready').length,0,'video alone exposed interactive desktop');
 const clipboard={label:'clipboard',readyState:'open'};
 function channel(value){const event=new Event('datachannel');event.channel=value;peer.dispatchEvent(event);}
 channel(clipboard);tick();assert.equal(messages.filter(m=>m.type==='ready').length,0,'clipboard is not keyboard/mouse');
 const input={label:'input',readyState:'connecting'};channel(input);tick();assert.equal(messages.filter(m=>m.type==='ready').length,0);
 input.readyState='open';tick();tick();assert.equal(messages.filter(m=>m.type==='ready').length,1);
 input.readyState='closed';tick();assert.equal(messages.at(-1).type,'error');
 const next=new context.RTCPeerConnection({});next.connectionState='connected';tick();assert.equal(messages.filter(m=>m.type==='ready').length,1,'old channel reused');
 const event=new Event('datachannel');event.channel={label:'input',readyState:'open'};next.dispatchEvent(event);tick();assert.equal(messages.filter(m=>m.type==='ready').length,2);
});
