const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync('extension_src/background.js', 'utf8');

// Small DOM fixture for both ChatGPT layouts, including a gallery that shares
// the user's data-turn-key wrapper (the actual October 2026 page structure).
class Element {
  constructor(tag, attrs = {}, children = []) {
    this.tagName = tag; this.attrs = attrs; this.children = children;
    this.alt = ''; this.src = ''; this.complete = true; this.naturalWidth = 1024;
    children.forEach(child => child.parentElement = this);
  }
  matches(selector) {
    return selector.split(',').some(s => {
      s = s.trim();
      if (!s.startsWith('[')) return s.toUpperCase() === this.tagName;
      const m = s.match(/^\[([\w-]+)(?:(\^=|\$=|~=|=)"([^"]*)")?\]$/);
      if (!m) throw new Error('Unsupported fixture selector: ' + s);
      const value = this.attrs[m[1]];
      if (value === undefined) return false;
      if (!m[2]) return true;
      return m[2] === '=' ? value === m[3] : m[2] === '^=' ? value.startsWith(m[3]) :
        m[2] === '$=' ? value.endsWith(m[3]) : value.split(' ').includes(m[3]);
    });
  }
  closest(s) { return this.matches(s) ? this : this.parentElement?.closest(s); }
  all() { return [this, ...this.children.flatMap(child => child.all())]; }
  querySelectorAll(s) { return this.all().slice(1).filter(el => el.matches(s)); }
  compareDocumentPosition(other) {
    let root = this;
    while (root.parentElement) root = root.parentElement;
    const all = root.all();
    return all.indexOf(other) > all.indexOf(this) ? 4 : 2;
  }
  scrollIntoView() {}
}
const image = (src, alt = 'Generated image 1', loaded = true) => Object.assign(new Element('IMG'),
  {src, alt, complete: loaded, naturalWidth: loaded ? 1024 : 0});
const gallery = img => new Element('DIV', {'data-testid': 'generated-image-gallery'}, [img]);
function detect(children) {
  const main = new Element('MAIN', {}, children);
  const page = vm.runInNewContext(source.slice(source.indexOf('const PAGE =')) + '\nPAGE;',
    {document: {body: main, querySelector: () => main}});
  return Array.from(page.turn_images().imgs);
}
const old = () => gallery(image('https://example.com/old.png'));
const upload = () => image('blob:product', 'User attachment');
const user = () => new Element('DIV', {'data-chatgpt-search-unit-key': 'turn-4:0:user'}, [upload()]);
assert.deepEqual(detect([old(), new Element('DIV', {'data-turn-key': 'turn4'},
  [user(), gallery(image('https://example.com/new.png'))])]), ['https://example.com/new.png']);
assert.deepEqual(detect([old(), new Element('DIV', {'data-message-author-role': 'user'}, [upload()]),
  new Element('DIV', {'data-message-author-role': 'assistant'}, [image('https://example.com/new.png')])]),
  ['https://example.com/new.png']);
assert.deepEqual(detect([old()]), []); // No boundary must never count all historical images.
assert.deepEqual(detect([old(), user(), gallery(image('https://example.com/loading.png', '', false))]), []);
assert.deepEqual(detect([old(), user()]), []);
console.log('PASS: 5 DOM scenarios — same-turn gallery, legacy layout, absent boundary, loading image, uploads');
