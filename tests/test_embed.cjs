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

test('embed forwards throttled user input activity from DOM events and RTCDataChannel send',()=>{
 const messages=[];
 let fakeNow=10000;
 const docListeners=new Map();
 const winListeners=new Map();
 class Peer extends EventTarget {constructor(){super();this.connectionState='connected';} setConfiguration(){} }
 const context={
  Date:{now:()=>fakeNow},
  Element:class{},
  document:{
   documentElement:{dataset:{}},
   createElement:()=>({}),
   head:{append(){}},
   addEventListener(type,fn){docListeners.set(type,(docListeners.get(type)||[]).concat(fn));},
   getElementById:()=>({readyState:4,videoWidth:1600})
  },
  location:{pathname:'/apps/blender/test/session/',origin:'https://studio.invalid'},
  parent:{postMessage:m=>messages.push(m)},
  innerWidth:1000,
  ResizeObserver:class{observe(){}},
  setInterval:()=>1,
  clearInterval(){},
  addEventListener(type,fn){winListeners.set(type,(winListeners.get(type)||[]).concat(fn));},
  RTCPeerConnection:Peer
 };
 context.window=context;
 vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../app/webclient/embed.js'),'utf8'),context);

 for(const fn of (docListeners.get('keydown')||[]))fn({});
 assert.equal(messages.filter(m=>m.type==='input').length,1);
 // Throttled inside 2000ms window
 fakeNow+=500;
 for(const fn of (docListeners.get('pointermove')||[]))fn({});
 assert.equal(messages.filter(m=>m.type==='input').length,1);

 // After >=2000ms, RTCDataChannel('input').send also emits input activity!
 fakeNow+=2100;
 const peer=new context.RTCPeerConnection({});
 const sentPayloads=[];
 const inputCh={label:'input',readyState:'open',send(p){sentPayloads.push(p);}};
 const dcEv=new Event('datachannel');dcEv.channel=inputCh;peer.dispatchEvent(dcEv);
 inputCh.send('m,10,20,0');
 assert.deepEqual(sentPayloads,['m,10,20,0']);
 assert.equal(messages.filter(m=>m.type==='input').length,2);

 // Non-shift wheel emits input; Shift+wheel forwards scroll to Studio without input false-positive
 fakeNow+=2500;
 for(const fn of (winListeners.get('wheel')||[]))fn({shiftKey:true,deltaY:120,preventDefault(){},stopImmediatePropagation(){}});
 assert.equal(messages.filter(m=>m.type==='wheel').length,1);
 assert.equal(messages.filter(m=>m.type==='input').length,2);
 for(const fn of (winListeners.get('wheel')||[]))fn({shiftKey:false,deltaY:60});
 assert.equal(messages.filter(m=>m.type==='input').length,3);
});
