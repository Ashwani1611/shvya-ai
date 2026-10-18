const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const code = fs.readFileSync('static/marketing/analytics.js', 'utf8');
function harness({consent, lead = false, sharedSession = new Map(), privacy = {}, blockedStorage = false, signup = false, article = false, deferred = false} = {}) {
  const events = {}, nodes = [], buttons = {};
  const local = new Map(consent ? [['shvya-marketing-consent-v1', consent]] : []);
  const storage = map => ({getItem:k=>map.get(k), setItem:(k,v)=>map.set(k,v)});
  const banner = {hidden:true};
  const makeButton = value => ({dataset:{analyticsConsent:value}, addEventListener:(_,fn)=>buttons[value]=fn});
  const context = {
    URL, Set, Date, encodeURIComponent, navigator: privacy,
    location:{origin:'https://shvya-ai.com', pathname:'/book-a-call/', hostname:'shvya-ai.com', search:'?email=secret@example.com&ref=private'},
    localStorage:storage(local), sessionStorage:storage(sharedSession),
    document:{currentScript:deferred?null:{dataset:{gaId:'G-TEST123'}}, referrer:'https://google.com/search?q=private',
      getElementById:id=>id==='marketing-consent'?banner:{addEventListener:(_,fn)=>buttons.settings=fn},
      querySelectorAll:()=>['granted','denied'].map(makeButton),
      querySelector:sel=>sel==='script[data-ga-id]'&&deferred?{dataset:{gaId:'G-TEST123'}}:sel==='.blog-article-heading'&&article?{}:sel==='[data-analytics-signup]'&&signup?{dataset:{analyticsSignup:'random-receipt'}}:sel==='[data-analytics-lead]'&&lead?{dataset:{analyticsLead:'private-reference'}}:null,
      createElement:()=>({}), head:{appendChild:node=>nodes.push(node)},
      addEventListener:(name,fn)=>events[name]=fn, documentElement:{scrollHeight:2000}},
    innerHeight:1000, scrollY:950, addEventListener:(name,fn)=>events[name]=fn,
  };
  if (blockedStorage) { Object.defineProperty(context, 'localStorage', {get(){throw new Error('blocked');}}); Object.defineProperty(context, 'sessionStorage', {get(){throw new Error('blocked');}}); }
  context.window=context;
  vm.runInNewContext(code,context);
  return {context,events,nodes,buttons,banner, sharedSession,
    sent:()=>context.dataLayer.map(a=>Array.from(a)).filter(a=>a[0]==='event')};
}
test('no Google loading or events before consent or after decline',()=>{
  const h=harness({lead:true});
  assert.equal(h.banner.hidden,false);
  assert.equal(h.nodes.length,0);
  h.buttons.denied();
  assert.equal(h.nodes.length,0);
  assert.equal(h.sent().length,0);
});
test('saved lead counts once across confirmation reloads, without reference leakage',()=>{
  const h=harness({consent:'granted',lead:true});
  assert.equal(h.nodes.length,1);
  assert.equal(h.sent().filter(a=>a[1]==='generate_lead').length,1);
  const reload=harness({consent:'granted',lead:true,sharedSession:h.sharedSession});
  assert.equal(reload.sent().filter(a=>a[1]==='generate_lead').length,0);
  const payload=JSON.stringify(h.context.dataLayer);
  assert.ok(!payload.includes('private'));
  assert.ok(!payload.includes('secret@example'));
  assert.ok(payload.includes('https://shvya-ai.com/book-a-call/'));
});
test('form attempts do not count as saved leads; consent withdrawal stops events',()=>{
  const h=harness({consent:'granted'});
  h.events.submit({target:{action:'https://shvya-ai.com/book-a-call/'}});
  assert.equal(h.sent().filter(a=>a[1]==='demo_form_submit').length,1);
  assert.equal(h.sent().filter(a=>a[1]==='generate_lead').length,0);
  h.buttons.denied();
  const count=h.sent().length;
  h.events.scroll();
  h.events.submit({target:{action:'https://shvya-ai.com/book-a-call/'}});
  assert.equal(h.sent().length,count);
  assert.equal(h.context['ga-disable-G-TEST123'],true);
});
test('repeated acceptance loads one Google tag and one page view',()=>{
  const h=harness();h.buttons.granted();h.buttons.granted();
  assert.equal(h.nodes.length,1);
  assert.equal(h.sent().filter(a=>a[1]==='page_view').length,1);
});

test('deferred Safari scripts still initialize from the tagged script element',()=>{
  const h=harness({deferred:true});
  assert.equal(h.banner.hidden,false);
  h.buttons.granted();
  assert.equal(h.nodes.length,1);
  assert.equal(h.sent().filter(a=>a[1]==='page_view').length,1);
});

test('privacy signals suppress analytics even with saved consent',()=>{
  for (const privacy of [{globalPrivacyControl:true},{doNotTrack:'1'}]) {
    const h=harness({consent:'granted',privacy});assert.equal(h.nodes.length,0);
  }
});
test('unavailable storage still permits explicit in-memory consent',()=>{
  const h=harness({blockedStorage:true,signup:true});h.buttons.granted();h.buttons.granted();
  assert.equal(h.sent().filter(a=>a[1]==='sign_up').length,1);
  assert.ok(!JSON.stringify(h.context.dataLayer).includes('random-receipt'));
});
test('withdrawal in another tab disables collection and expires GA cookies',()=>{
  const h=harness({consent:'granted'});h.events.storage({key:'shvya-marketing-consent-v1',newValue:'denied'});
  assert.equal(h.context['ga-disable-G-TEST123'],true);
  assert.match(h.context.document.cookie,/Max-Age=0/);
});

test('only published blog article markup produces article_view',()=>{
  assert.match(fs.readFileSync('templates/blog/public/article.html','utf8'),/class="blog-article-heading"/);
  assert.equal(harness({consent:'granted',article:true}).sent().filter(a=>a[1]==='article_view').length,1);
  assert.equal(harness({consent:'granted'}).sent().filter(a=>a[1]==='article_view').length,0);
});
