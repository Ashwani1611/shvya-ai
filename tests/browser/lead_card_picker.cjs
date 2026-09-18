const {chromium}=require(process.env.PLAYWRIGHT_PATH || 'playwright');
const fs=require('fs');const path=require('path');const assert=require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch({headless:true,...(process.env.BROWSER_EXECUTABLE?{executablePath:process.env.BROWSER_EXECUTABLE}:{})});
 const page=await browser.newPage({viewport:{width:1280,height:800}});
 const root=path.resolve(__dirname,'../..');
 await page.setContent('<div class="lead-card" id="lead-card-1"><form><select name="stage" aria-label="Lead stage"><option value="new">New lead</option><option value="qualified">Qualified</option><option value="won">Won</option></select></form></div><select id="pipeline-select" name="pipeline"><option value="a">Sales</option><option value="b">Support</option></select><div id="modal-root"><select name="pipeline"><option>Modal pipeline</option></select></div>');
 await page.addStyleTag({content:fs.readFileSync(path.join(root,'static/css/lead_card_upgrade.css'),'utf8')});
 const js=fs.readFileSync(path.join(root,'static/js/lead_card_upgrade.js'),'utf8');
 await page.addScriptTag({content:js});

 const stage=page.getByRole('button',{name:'Change lead stage'});
 const pipeline=page.getByRole('button',{name:'Switch pipeline'});
 assert.equal(await stage.count(),1);assert.equal(await pipeline.count(),1);
 assert.equal(await page.locator('dialog').count(),0);
 assert.equal(await page.getByRole('button',{name:'Apply',exact:true}).count(),0);
 assert.equal(await page.locator('#modal-root .lead-picker-trigger').count(),0);
 assert.equal(await page.locator('#modal-root select[name=pipeline]').isVisible(),true);

 await stage.click();
 assert.equal(await page.getByRole('listbox',{name:'Change lead stage'}).isVisible(),true);
 await page.getByRole('option',{name:'Qualified',exact:true}).click();
 assert.equal(await page.locator('select[name=stage]').inputValue(),'qualified');
 assert.equal(await page.getByRole('listbox').isVisible(),false);
 assert.match(await stage.innerText(),/Qualified/);

 await pipeline.click();
 await page.getByRole('option',{name:'Support',exact:true}).click();
 assert.equal(await page.locator('#pipeline-select').inputValue(),'b');
 assert.match(await pipeline.innerText(),/Support/);

 await page.addScriptTag({content:js});
 assert.equal(await page.locator('.lead-picker-trigger').count(),2);

 await page.evaluate(()=>{
   const select=document.querySelector('select[name=stage]');
   select.addEventListener('change',e=>{
     document.dispatchEvent(new CustomEvent('htmx:beforeRequest',{detail:{elt:e.target.form}}));
   });
 });

 await stage.click();
 await page.getByRole('option',{name:'Won',exact:true}).click();
 assert.equal(await page.locator('select[name=stage]').inputValue(),'won');
 assert.equal(await stage.getAttribute('aria-busy'),'true');
 await page.evaluate(()=>document.dispatchEvent(new CustomEvent('htmx:afterRequest',{detail:{elt:document.querySelector('.lead-card form'),successful:false}})));
 assert.equal(await page.locator('select[name=stage]').inputValue(),'qualified');
 assert.match(await stage.innerText(),/Qualified/);
 assert.equal(await stage.getAttribute('aria-busy'),'false');

 await stage.click();
 await page.getByRole('option',{name:'Won',exact:true}).click();
 await page.evaluate(()=>document.dispatchEvent(new CustomEvent('htmx:afterRequest',{detail:{elt:document.querySelector('.lead-card form'),successful:true}})));
 assert.equal(await page.locator('select[name=stage]').inputValue(),'won');
 assert.match(await stage.innerText(),/Won/);

 await stage.focus();await page.keyboard.press('ArrowDown');
 assert.equal(await page.getByRole('listbox').isVisible(),true);
 await page.keyboard.press('Escape');
 assert.equal(await page.getByRole('listbox').isVisible(),false);
 assert.equal(await stage.getAttribute('aria-expanded'),'false');

 await page.setViewportSize({width:375,height:812});
 await pipeline.click();
 const box=await page.getByRole('listbox').boundingBox();
 assert.ok(box.x>=0&&box.x+box.width<=375);
 await page.keyboard.press('Escape');

 await page.evaluate(()=>{
   const card=document.createElement('div');
   card.className='lead-card';
   card.innerHTML='<form><select name="stage"><option value="n">New</option><option value="q">Qualified</option></select></form>';
   document.body.appendChild(card);
   document.dispatchEvent(new CustomEvent('htmx:afterSwap',{detail:{target:card}}));
 });
 assert.equal(await page.locator('.lead-picker-trigger').count(),3);

 if(process.env.PICKER_SCREENSHOT){
   await stage.click();
   await page.screenshot({path:process.env.PICKER_SCREENSHOT});
 }
 await browser.close();
 console.log('Picker browser checks passed: direct selection, no modal/apply, rollback, keyboard, mobile positioning, modal isolation and HTMX enhancement.');
})().catch(error=>{console.error(error);process.exit(1);});
