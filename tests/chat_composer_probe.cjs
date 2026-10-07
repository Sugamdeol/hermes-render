const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[2], 'utf8');
const composer = source.slice(source.indexOf('  function Composer('), source.indexOf('  // ── session sidebar'));
function mount(overrides = {}) {
  const changes = [];
  let calls = 0, resolve;
  const pending = new Promise(r => resolve = r);
  const ctx = {
    useState: value => [value, () => {}], useRef: value => ({current: value}), useEffect: () => {},
    h: (type, props, ...children) => ({type, props: props || {}, children}),
    cn: (...values) => values.filter(Boolean).join(' '), CommandBar() {},
  };
  vm.createContext(ctx);
  vm.runInContext(composer + '\nthis.Composer = Composer;', ctx);
  const props = {
    text: 'hello', setText: value => changes.push(value), onSend: () => { calls++; return pending; },
    onBackground: () => { calls++; return pending; }, onSteer: () => {}, onDispatchCommand: () => {},
    commandCatalog: [], generating: false, disabled: false, readOnly: false, attachments: [],
    onAttach() {}, onRemoveAttachment() {}, onOpenTools() {}, modes: [], enterToSend: true,
    setEnterToSend() {}, ...overrides,
  };
  const tree = ctx.Composer(props);
  const nodes = [];
  function visit(n) { if (!n || typeof n !== 'object') return; if (Array.isArray(n)) return n.forEach(visit); nodes.push(n); (n.children || []).forEach(visit); }
  visit(tree);
  return {input: nodes.find(n => n.type === 'textarea'), button: nodes.find(n => n.props.className === 'hcd-send'), changes, calls: () => calls, resolve};
}
(async () => {
  const normal = mount();
  const first = normal.button.props.onClick();
  await normal.button.props.onClick();
  assert.equal(normal.calls(), 1, 'Rapid clicks must dispatch only once');
  normal.resolve(); await first;
  for (const options of [{disabled:true}, {readOnly:true}, {attachments:[{status:'uploading',name:'file'}]}]) {
    const item = mount(options); await item.button.props.onClick(); assert.equal(item.calls(), 0);
  }
  const ime = mount();
  ime.input.props.onKeyDown({key:'Enter', nativeEvent:{isComposing:true}, preventDefault(){throw Error('IME submit prevented');}});
  assert.equal(ime.calls(), 0);
  const failed = mount({onSend: async () => {throw Error('offline');}});
  await failed.button.props.onClick();
  assert.equal(failed.changes.at(-1), 'hello', 'Failed dispatch must keep the draft');
  const shift = mount(); shift.input.props.onKeyDown({key:'Enter',shiftKey:true}); assert.equal(shift.calls(),0);
  console.log('Chat composer: duplicate, disabled, read-only, uploads, IME, Shift+Enter and failed-draft checks passed');
})();
