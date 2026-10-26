'use strict';
(()=>{
const host=document.getElementById('home-stories');if(!host)return;

const videos=[...host.querySelectorAll('video')];
const reduced=window.matchMedia('(prefers-reduced-motion:reduce)');
const visibility=new Map(videos.map(v=>[v,true]));
const scenes=videos.map(video=>({video,canvas:video.previousElementSibling,foreground:video.nextElementSibling,foregroundContext:video.nextElementSibling.getContext('2d',{alpha:false,desynchronized:true}),context:video.previousElementSibling.getContext('2d',{alpha:false,desynchronized:true}),pending:null,native:typeof video.requestVideoFrameCallback==='function'}));
function paintAmbient(scene){
 const v=scene.video,c=scene.context;if(!c||v.readyState<2||!v.videoWidth)return;
 if(scene.foregroundContext)scene.foregroundContext.drawImage(v,0,0,scene.foreground.width,scene.foreground.height);
 const w=v.videoWidth,h=v.videoHeight,strip=Math.max(1,Math.round(h*.07));
 // Stretch narrow edge strips outward; keep the central frame aligned.
 c.drawImage(v,0,0,w,strip,0,0,96,32);
 c.drawImage(v,0,0,w,h,0,32,96,96);
 c.drawImage(v,0,h-strip,w,strip,0,128,96,32);
}
function activeScene(scene){return !document.hidden&&!reduced.matches&&visibility.get(scene.video)&&!scene.video.paused;}
function stopAmbient(scene){
 if(scene.pending===null)return;
 if(scene.native)scene.video.cancelVideoFrameCallback(scene.pending);else cancelAnimationFrame(scene.pending);
 scene.pending=null;
}
function startAmbient(scene){
 if(scene.pending!==null||!activeScene(scene))return;
 const frame=()=>{
  scene.pending=null;
  if(!activeScene(scene))return;
  // Draw the same frame that the video is about to present, without a timer cap.
  paintAmbient(scene);
  startAmbient(scene);
 };
 scene.pending=scene.native?scene.video.requestVideoFrameCallback(frame):requestAnimationFrame(frame);
}
for(const scene of scenes){
 scene.video.addEventListener('loadeddata',()=>{paintAmbient(scene);startAmbient(scene);});
 scene.video.addEventListener('playing',()=>startAmbient(scene));
 scene.video.addEventListener('pause',()=>stopAmbient(scene));
 scene.video.addEventListener('seeked',()=>paintAmbient(scene));
}
function syncPlayback(){
 host.classList.toggle('home-stories-paused',reduced.matches||document.hidden);
 for(const scene of scenes){
  const video=scene.video,inactive=!visibility.get(video);
  video.closest('.home-story').classList.toggle('is-inactive',inactive);
  if(reduced.matches||document.hidden||inactive){stopAmbient(scene);video.pause();}
  else{const p=video.play();if(p)p.then(()=>startAmbient(scene)).catch(()=>{});}
 }
}

document.addEventListener('visibilitychange',syncPlayback);
reduced.addEventListener('change',syncPlayback);
const observer=new IntersectionObserver(entries=>{for(const e of entries)visibility.set(e.target.querySelector('video'),e.isIntersecting);syncPlayback();},{threshold:.1});
for(const video of videos)observer.observe(video.closest('.home-story'));
syncPlayback();

})();
