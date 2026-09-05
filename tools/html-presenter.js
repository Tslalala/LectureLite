/* HTML files execute in an opaque-origin sandbox. The host never runs their code. */
window.LectureHTML = (() => {
  function bridge(channel) {
    // A srcdoc frame cannot rewrite its URL to #slide with replaceState.
    for (const name of ['replaceState','pushState']) {
      const original = history[name].bind(history);
      history[name] = (...args) => { try { return original(...args); } catch (_) {} };
    }
    let nodes = [], baseline = [], playback = false, last = '', ready = false;
    const attrs = ['class','style','hidden','open'];
    const send = (kind, value) => parent.postMessage({lectureHTML:channel,kind,value}, '*');
    function snapshot() {
      return {slide:[...document.querySelectorAll('.slide')].findIndex(n=>n.classList.contains('active')),nodes:nodes.map((n,i) => {
        const a = attrs.map(k => n.getAttribute(k));
        const changed = a.some((v,j) => v !== baseline[i][j]);
        const scroll = n.scrollTop || n.scrollLeft;
        const text = n.id === 'counter' ? n.textContent : undefined;
        return changed || scroll || text ? {i,a,y:n.scrollTop,x:n.scrollLeft,text} : null;
      }).filter(Boolean), x:scrollX,y:scrollY};
    }
    function emit() {
      if (!ready || playback) return;
      const state = snapshot(), key = JSON.stringify(state);
      if (key !== last) { last = key; send('state', state); }
    }
    window.addEventListener('message', e => {
      if (e.source !== parent || e.data?.lectureHTML !== channel) return;
      const d = e.data;
      if(d.kind === 'capture' && ready) send('captured',snapshot());
      if (d.kind === 'mode') playback = !!d.value;
      if(d.kind === 'restore' && ready && Number.isInteger(d.value?.slide)){
        const index = d.value.slide;
        const slides = [...document.querySelectorAll('.slide')];
        if(index >= 0 && index < slides.length){
          playback = false;
          const link = document.querySelector('.toc-item[data-i="'+index+'"]');
          if(link) link.click();
          else {slides.forEach((s,i)=>s.classList.toggle('active',i===index));const counter=document.getElementById('counter');if(counter)counter.textContent=(index+1)+' / '+slides.length;}
          playback = true;
        }
      }
      if (d.kind === 'restore' && ready && d.value && Array.isArray(d.value.nodes)) {
        playback = true;
        const saved = new Map(d.value.nodes.filter(n=>n && Number.isInteger(n.i)).map(n => [n.i,n]));
        nodes.forEach((n,i) => {
          const v = saved.get(i), a = Array.isArray(v?.a) ? v.a : baseline[i];
          attrs.forEach((k,j) => { if (a[j] == null) n.removeAttribute(k); else n.setAttribute(k, String(a[j])); });
          n.scrollTop = v?.y || 0; n.scrollLeft = v?.x || 0;
          if (v?.text != null && n.id === 'counter') n.textContent = v.text;
        });
        scrollTo(d.value.x || 0,d.value.y || 0);
      }
    });
    window.addEventListener('load', () => {
      nodes = [document.documentElement,document.body,...document.body.querySelectorAll('*')]
        .filter(n => !['SCRIPT','STYLE','META','LINK'].includes(n.tagName)).slice(0,5000);
      baseline = nodes.map(n => attrs.map(k => n.getAttribute(k)));
      ready = true;
      const slides = [...document.querySelectorAll('.slide')].map(n=>n.textContent);
      const copy = document.body.cloneNode(true);
      copy.querySelectorAll('script,style').forEach(n=>n.remove());
      send('ready',{text:copy.textContent.slice(0,200000),slides});
      emit();
      setInterval(emit,200);
    });
    document.addEventListener('pointermove', e => { if(!playback) send('pointer',{x:e.clientX/innerWidth,y:e.clientY/innerHeight}); }, {passive:true});
    document.addEventListener('pointerdown', e => { if(!playback) send('click',{x:e.clientX/innerWidth,y:e.clientY/innerHeight}); }, {passive:true});
    // Playback is driven by the saved timeline; do not let embedded shortcuts diverge.
    for (const name of ['click','keydown','wheel','touchstart']) document.addEventListener(name,e => {
      if(playback){ e.preventDefault(); e.stopImmediatePropagation(); }
    },true);
  }
  async function mount(host, html, imageMap, onEvent) {
    const channel = crypto.randomUUID();
    const parsed = new DOMParser().parseFromString(html,'text/html');
    parsed.querySelectorAll('base,meta[http-equiv],iframe,object,embed').forEach(n=>n.remove());
    parsed.querySelectorAll('img').forEach(n=>{
      const src = n.getAttribute('src') || '';
      const key = src.replace(/^\.\//,'').toLowerCase();
      if(imageMap[key]) n.setAttribute('src', imageMap[key]);
    });
    const csp = parsed.createElement('meta');
    csp.httpEquiv = 'Content-Security-Policy';
    csp.content = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src blob: data:; media-src blob: data:; font-src data:; connect-src 'none'; form-action 'none'; base-uri 'none'";
    const script = parsed.createElement('script');
    script.textContent = '(' + bridge.toString() + ')(' + JSON.stringify(channel) + ');';
    parsed.head.prepend(csp,script);
    const frame = document.createElement('iframe');
    frame.title = 'HTML 演示文稿'; frame.setAttribute('sandbox','allow-scripts');
    frame.className = 'html-presentation'; frame.referrerPolicy = 'no-referrer';
    let resolveReady, resolveCapture;
    const pending = new Promise(r=>resolveReady=r);
    const listener = e=>{
      if(e.source !== frame.contentWindow || e.data?.lectureHTML !== channel) return;
      if(!['ready','state','pointer','click','captured'].includes(e.data.kind)) return;
      if(e.data.value == null || JSON.stringify(e.data.value).length > 500000) return;
      if(e.data.kind === 'captured'){resolveCapture?.(e.data.value);resolveCapture=null;return;}
      if(e.data.kind === 'ready') {
        if(typeof e.data.value.text !== 'string' || !Array.isArray(e.data.value.slides)) return;
        resolveReady(e.data.value);
      }
      onEvent(e.data.kind,e.data.value);
    };
    window.addEventListener('message',listener);
    frame.srcdoc = '<!doctype html>'+parsed.documentElement.outerHTML;
    host.replaceChildren(frame);
    const send = (kind,value)=>frame.contentWindow?.postMessage({lectureHTML:channel,kind,value},'*');
    let timeout;
    const info = await Promise.race([pending,new Promise(r=>timeout=setTimeout(()=>r({text:parsed.body.textContent,slides:0}),6000))]);
    clearTimeout(timeout);
    const capture = ()=>new Promise(resolve=>{const timer=setTimeout(()=>{resolveCapture=null;resolve(null);},1500);resolveCapture=value=>{clearTimeout(timer);resolve(value);};send('capture');});
    return {frame,info,send,capture,destroy(){resolveCapture?.(null);window.removeEventListener('message',listener);frame.remove();}};
  }
  return {mount};
})();
