const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync('extension_src/background.js', 'utf8');
const style = {display:'block', visibility:'visible', opacity:'1', fontFamily:'"Academy Sans", sans-serif',
  fontWeight:'400', color:'rgb(20, 30, 40)', backgroundColor:'rgb(255, 255, 255)', fontSize:'16px', letterSpacing:'normal'};
const element = (text, overrides={}) => ({textContent:text, getBoundingClientRect:()=>({width:120,height:60}),
  parentElement:null, style:{...style}, ...overrides});
const heading = element('Laima Academy', {style:{...style, fontFamily:'"Academy Display", serif', fontWeight:'700'}});
const body = element('A paragraph from the website.');
const logo = element('', {alt:'Laima Academy logo', id:'logo', className:'logo', currentSrc:'https://laima.example/logo.svg',
  naturalWidth:300, naturalHeight:100, closest:selector => selector.startsWith('header') ? {} : {href:'https://laima.example/'}});
const document = {fonts:{ready:Promise.resolve(),status:'loaded'}, title:'Laima Academy', images:[logo],
  querySelector:()=>({content:'Laima Academy'}), querySelectorAll:selector=>{
    if(selector==='h1, h2'||selector==='h1') return [heading];
    if(selector==='main p, article p') return [body];
    return [];
  }};
const page = vm.runInNewContext(source.slice(source.indexOf('const PAGE ='))+'\nPAGE;', {
  document, location:{href:'https://laima.example/'}, getComputedStyle:el=>el.style, URL, Date,
  setTimeout:(fn,ms)=>{ const timer=setTimeout(fn,ms); timer.unref(); return timer; }
});
(async()=>{
  const data=await page.brand_snapshot();
  assert.equal(data.styles.find(s=>s.role==='heading').font_family, '"Academy Display", serif');
  assert.equal(data.styles.find(s=>s.role==='body').font_family, '"Academy Sans", sans-serif');
  assert.equal(data.logos[0].src, 'https://laima.example/logo.svg');
  assert.equal(data.fonts_loaded, true);
  console.log('PASS: website snapshot preserves heading/body fonts, colours and logo source');
})().catch(e=>{console.error(e);process.exitCode=1;});
