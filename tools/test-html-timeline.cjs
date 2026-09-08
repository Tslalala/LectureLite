const fs = require('node:fs');
const assert = require('node:assert/strict');
const path = require('node:path');
const root = path.join(__dirname, '..');
const page = fs.readFileSync(path.join(root, 'lecture-lite.html'), 'utf8');
for (const name of ['lecture-lite.html', 'home.html']) {
  for (const match of fs.readFileSync(path.join(root,name),'utf8').matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)) new Function(match[1]);
}
const start = page.indexOf('function buildOfflineStates(');
const end = page.indexOf('async function generateSpeech()',start);
const run = new Function('source','scriptTimeline','currentFileIndex',`let htmlTimeline=[],annotations=[];const annotationTools=null;function renderAnnotations(){}function emitCursorTimeline(cursor,pos,times){pos.forEach((p,i)=>{if(p)cursor.push({t:times[i][0],x:p.x,y:p.y});});}${page.slice(start,end)};const result=buildOfflineStates([1000,2000,1000],350);return {result,htmlTimeline,scriptTimeline,annotations};`);
const {result,htmlTimeline,scriptTimeline,annotations} = run({type:'html',slides:['第一页：开始','第二页：结束'],htmlTargets:[
  {slide:0,text:'第一页：开始',x:.1,y:.2,w:.4,h:.1},
  {slide:1,text:'第二页：结束',x:.4,y:.5,w:.3,h:.1}
]},[{text:'第一页：开始'},{text:'改写讲解',anchor:'第二页：结束',op:{kind:'highlight',target:'第二页：结束'}},{text:'不匹配时保持当前页'}],2);
assert.deepEqual(htmlTimeline.map(r=>r.state.slide),[0,1,1]);
assert.deepEqual(htmlTimeline.map(r=>r.t),[0,1350,3700]);
assert.equal(result.totalDurMs,4700);
assert.equal(scriptTimeline[2].endMs,4700);
assert.equal(htmlTimeline[0].fi,2);
assert.equal(result.cursor.length,3);
assert.equal(result.cursor[0].t,0);
assert.ok(Math.abs(result.cursor[0].x-.3)<1e-9);
assert.ok(Math.abs(result.cursor[0].y-.265)<1e-9);
assert.equal(result.newAnnotations.length,1);
assert.equal(annotations[0].coords[0].slide,1);
console.log('PASS: HTML timeline, automatic cursor, annotations, timing, file identity, and page script syntax');
