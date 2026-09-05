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
const run = new Function('source','scriptTimeline','currentFileIndex',`let htmlTimeline=[];${page.slice(start,end)};const result=buildOfflineStates([1000,2000,1000],350);return {result,htmlTimeline,scriptTimeline};`);
const {result,htmlTimeline,scriptTimeline} = run({type:'html',slides:['第一页：开始','第二页：结束']},[{text:'第一页：开始'},{text:'改写讲解',anchor:'第二页：结束'},{text:'不匹配时保持当前页'}],2);
assert.deepEqual(htmlTimeline.map(r=>r.state.slide),[0,1,1]);
assert.deepEqual(htmlTimeline.map(r=>r.t),[0,1350,3700]);
assert.equal(result.totalDurMs,4700);
assert.equal(scriptTimeline[2].endMs,4700);
assert.equal(htmlTimeline[0].fi,2);
assert.deepEqual(result.cursor,[]);
console.log('PASS: HTML timeline matching, timing, file identity, and page script syntax');
