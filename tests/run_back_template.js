'use strict';
// Test helper: runs the <script> of the Anki back template against a small fake `document`
// and prints what the script built, as JSON. It lets the tests check the real template
// without a browser.
//
//   node run_back_template.js back.html meaning.html [toggle | break]
//
//   toggle  also presses the translation button twice and reports its state
//   break   makes the script fail half way, to check that the card is not left empty

const fs = require('fs');
const vm = require('vm');

const [templatePath, meaningPath, mode] = process.argv.slice(2);
const template = fs.readFileSync(templatePath, 'utf8');
const meaning = fs.readFileSync(meaningPath, 'utf8');
const script = template.slice(template.indexOf('<script>') + 8, template.lastIndexOf('</script>'));

const NAMED = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: '\u00a0' };

// What a browser does with the text of a <textarea>: character references are decoded, tags are not.
function decode(text) {
  return text.replace(/&(#x[0-9a-f]+|#\d+|[a-z]+);/gi, (whole, name) => {
    if (name[0] === '#') {
      const code = name[1].toLowerCase() === 'x' ? parseInt(name.slice(2), 16) : parseInt(name.slice(1), 10);
      return String.fromCodePoint(code);
    }
    return Object.prototype.hasOwnProperty.call(NAMED, name.toLowerCase()) ? NAMED[name.toLowerCase()] : whole;
  });
}

function stripTags(html) {
  return decode(html.replace(/<[^>]*>/g, ''));
}

function makeElement(html) {
  const classes = new Set();
  return {
    style: {},
    classes,
    classList: { add: (name) => classes.add(name), remove: (name) => classes.delete(name) },
    _html: html || '',
    _text: null,
    get innerHTML() { return this._html; },
    set innerHTML(value) { this._html = String(value); this._text = null; },
    get textContent() { return this._text !== null ? this._text : stripTags(this._html); },
    set textContent(value) { this._text = String(value); this._html = ''; },
  };
}

const elements = {
  'raw-data': makeElement(meaning),
  'new-ui': makeElement(''),
};

const document = {
  getElementById: (id) => elements[id] || null,
  createElement: (tag) => {
    if (mode === 'break') throw new Error('broken on purpose');
    if (tag === 'textarea') {
      return {
        _value: '',
        set innerHTML(value) { this._value = decode(String(value)); },
        get value() { return this._value; },
      };
    }
    return makeElement('');
  },
};

const sandbox = { document, window: {} };
sandbox.window.document = document;
vm.runInNewContext(script, sandbox, { filename: templatePath });

const ui = elements['new-ui'];
const raw = elements['raw-data'];
const result = {
  ui: ui.innerHTML,
  uiText: ui._text,
  raw: raw.innerHTML,
  rawDisplay: raw.style.display || '',
  hasToggle: typeof sandbox.window.myToggleFa === 'function',
};

if (mode === 'toggle') {
  // the script builds these two inside the page; here they stand in for the real elements,
  // and the box starts closed, as the markup says (style="display:none;")
  elements['my-fabox'] = makeElement('');
  elements['my-fabox'].style.display = 'none';
  elements['my-ftgl'] = makeElement('');
  const states = [];
  for (let press = 0; press < 2; press++) {
    sandbox.window.myToggleFa();
    states.push({
      boxDisplay: elements['my-fabox'].style.display,
      open: elements['my-ftgl'].classes.has('my-open'),
      button: elements['my-ftgl'].innerHTML,
    });
  }
  result.toggle = states;
}

console.log(JSON.stringify(result));
