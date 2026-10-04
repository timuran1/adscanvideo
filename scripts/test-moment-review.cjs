// Run: node scripts/test-moment-review.cjs (no browser or paid API calls).
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync(require('node:path').join(__dirname, '../index.html'), 'utf8');
class Element {
  constructor(){this.children=[];this.listeners={};this.attrs={};}
  append(item){this.children.push(item);}
  replaceChildren(){this.children=[];}
  setAttribute(name,value){this.attrs[name]=value;}
  addEventListener(name,handler){this.listeners[name]=handler;}
  get textContent(){return this._text ?? this.children.map(c=>c.textContent).join('');}
  set textContent(value){this._text=value;}
  scrollIntoView(){this.scrolled=true;}
  focus(){this.focused=true;}
}
const output=new Element(),player=new Element(),fileInput=new Element();
player.duration=5;
const notices=[];
const ctx=vm.createContext({document:{getElementById:id=>({'result-content':output,'original-video':player,'verification-file':fileInput}[id]),createElement:()=>new Element(),createTextNode:text=>({textContent:text})},verificationURL:null,message:t=>notices.push(t)});
// Read balanced closing brace via a non-greedy boundary known in this source.
const functionSource=html.slice(html.indexOf('function renderReport(text) {'),html.indexOf('\n}',html.indexOf('function renderReport(text) {'))+2);
vm.runInContext(functionSource,ctx);
const report='Candidate 00:01.125–00:02.875. Repeat at 00:03.250. <img src=x onerror=alert(1)>';
ctx.renderReport(report);
assert.equal(output.textContent,report,'Copy and export text must be preserved verbatim');
const buttons=output.children.filter(c=>c.listeners);
assert.equal(buttons.length,3,'Fractional timestamps and both range boundaries must be clickable');
buttons[0].listeners.click();
assert.equal(fileInput.focused,true,'No original file must request a file without seeking');
ctx.verificationURL='blob:test';
buttons[0].listeners.click();
assert.equal(player.currentTime,1.125,'Seek must retain fractional seconds');
ctx.renderReport('Hour-format 00:00:03.250.');
output.children.find(c=>c.listeners).listeners.click();
assert.equal(player.currentTime,3.25,'Hour-format timestamps must seek correctly');
ctx.renderReport('Outside 00:09.500');
output.children.find(c=>c.listeners).listeners.click();
assert.equal(player.currentTime,3.25,'Out-of-range timestamps must not seek');
assert.match(notices.at(-1),/outside/);
console.log('Moment review: safe text rendering, ranges, fractional seek and file/range guards passed.');
