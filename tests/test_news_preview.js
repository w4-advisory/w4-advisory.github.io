const assert = require('node:assert/strict');
const {test} = require('node:test');
const {readFileSync} = require('node:fs');
const vm = require('node:vm');

function load() {
  const element = tag => ({tag, children: [], attributes: {}, classList: {add() {}},
    setAttribute(k, v) { this.attributes[k] = v; },
    append(...items) { this.children.push(...items); },
    replaceChildren(...items) { this.children = items; }
  });
  const context = {window: {}, URL, location: {origin: 'https://w4-advisory.github.io'},
    document: {baseURI: 'https://w4-advisory.github.io/news.html', createElement: element,
      createElementNS: (_, tag) => element(tag)}};
  vm.runInNewContext(readFileSync('news-preview.js', 'utf8'), context);
  return context.window.W4NewsPreview;
}

test('uses the validated local article image', () => {
  const wrap = load().media({title: 'Research', image: 'assets/news/0123456789abcdef.webp'});
  assert.equal(wrap.children[0].tag, 'img');
  assert.equal(wrap.children[0].src, 'https://w4-advisory.github.io/assets/news/0123456789abcdef.webp');
});

test('broken publisher image falls back to its own card, then inline headline', () => {
  const wrap = load().media({title: 'Claude sandbox escape', source: 'Example', image: 'assets/news/0123456789abcdef.webp', image_fallback: 'assets/news/fedcba9876543210.svg'});
  wrap.children[0].onerror();
  assert.match(wrap.children[0].src, /fedcba9876543210.svg$/);
  wrap.children[0].onerror();
  assert.equal(wrap.children[0].tag, 'svg');
  assert.equal(wrap.children[0].attributes['aria-label'], 'Claude sandbox escape');
  assert.equal(wrap.children[0].children[2].children[0].textContent, 'Claude sandbox escape');
});

test('missing images and unsafe URL schemes render escaped text through DOM', () => {
  const wrap = load().media({title: '<script>alert(1)</script>', image: 'javascript:alert(1)'});
  assert.equal(wrap.children[0].tag, 'svg');
  assert.equal(wrap.children[0].attributes['aria-label'], '<script>alert(1)</script>');
});
