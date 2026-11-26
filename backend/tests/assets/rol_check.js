const fs=require('fs'), vm=require('vm');
const html=fs.readFileSync(process.argv[2],'utf8');
const script=html.split('<script>').slice(-1)[0].split('</script>')[0];
const mk=()=>new Proxy(function(){}, {get:(t,k)=>{ if(k==='classList') return {add(){},remove(){},toggle(){},contains(){return false}}; if(k==='style'||k==='dataset') return {}; if(k==='innerHTML'||k==='textContent'||k==='value') return t['__'+String(k)]||''; if(k==='querySelectorAll') return ()=>[]; if(k==='getAttribute') return ()=>null; return (...a)=>mk(); }, set:(t,k,v)=>{t['__'+String(k)]=v;return true}, apply:()=>mk()});
const docs={};
const document={getElementById:(id)=>docs[id]||(docs[id]=mk()), querySelectorAll:()=>[], documentElement:mk(), body:mk(), addEventListener(){}, createElement:()=>mk()};
const window={innerWidth:1200, scrollTo(){}, addEventListener(){}, matchMedia:()=>({matches:false}), print(){}};
const ctx={document,window,localStorage:{getItem(){return null},setItem(){}},console,requestAnimationFrame:f=>f(),setTimeout};
ctx.window=Object.assign(window,{});
vm.createContext(ctx);
vm.runInContext(script+'\n;this.__out={ROLES,PASOS,metaRol,actsDelRol,rolViewHTML,go,POR_ROL};',ctx);
const o=ctx.__out;
const res={poR:o.POR_ROL, roles:[]};
for(let i=0;i<o.ROLES.length;i++){
  const tag=o.ROLES[i].tag;
  const propias=o.actsDelRol(tag).reduce((t,x)=>t+x.idx.length,0);
  const html1=o.rolViewHTML(i);
  const filas=(html1.match(/class="drow/g)||[]).length;
  ctx.window.ROL_TODAS=true;
  const html2=o.rolViewHTML(i);
  const mias=(html2.match(/class="drow mia"/g)||[]).length, otras=(html2.match(/class="drow otra"/g)||[]).length;
  ctx.window.ROL_TODAS=false;
  const totalActs=o.PASOS.reduce((t,p)=>t+p.acts.length,0);
  res.roles.push({tag,propias,filas_modo_rol:filas,todas_mias:mias,todas_otras:otras,total:totalActs});
}
console.log(JSON.stringify(res));
