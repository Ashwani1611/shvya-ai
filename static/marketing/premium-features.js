(() => {
  'use strict';
  const features = {
    crm: ['CUSTOMER CONTEXT', 'Every lead. The whole story.', 'Bring contacts, conversations, notes and pipeline stages together. Pick up the relationship without piecing it back together.', ['NEW ENQUIRY|Website enquiry|Interested in a product demo|New lead','QUALIFIED|WhatsApp conversation|Requirements captured|Ready for a call','FOLLOW-UP|Proposal shared|Next action in one place|Reminder set'], 'Contacts · Lead timeline · Notes · Pipeline stages'],
    sales: ['YOUR DAILY WORKSPACE','Start with what needs you.','Bring your sales work into focus with Sales Desk, so the team can move from customer context to a useful next action.', ['CONTEXT|Review the lead|See the conversation so far|Sales Desk','PRIORITY|Plan the next step|Keep your sales work focused|Team action','FOLLOW THROUGH|Continue the conversation|Work with the customer story|Connected CRM'], 'Sales workspace · Customer context · Team follow-through'],
    playbooks: ['AI PLAYBOOK','Make it sound like you.','Shape AI engagement with your business information, languages, qualification requirements, instructions and knowledge sources.', ['AI SETUP|Your business context|Describe your offering and audience|Your voice','KNOWLEDGE|Documents and URLs|Give AI relevant source material|Your knowledge','FAQ|Answers that matter|Prepare common customer questions|Your expertise'], 'AI Setup · Qualification · Engagement instructions · Documents · URLs · FAQs'],
    cadence: ['FOLLOW-UP SEQUENCES','Keep the conversation moving.','Create ordered WhatsApp, email and call-reminder steps. Choose when each step should run and reuse the sequence.', ['STEP 01|Start the conversation|WhatsApp · Immediately|Message','STEP 02|Share useful details|Email · After 1 day|Follow-up','STEP 03|Make a personal call|Call reminder · After 2 days|Team action'], 'Sequences are available. Touchpoints is an upcoming feature. Timing shown is an example.'],
    workflows: ['RULES & ACTIONS','Give repeatable work a trigger.','Organize event-driven sales actions with workflows, keeping your process connected to what happens in the CRM.', ['TRIGGER|Something changes|Start from a CRM event|Event','CONDITION|Check the context|Match the rules you set|Rule','ACTION|Move work forward|Apply the configured next step|Workflow'], 'Event-driven automation · Rules · Configured actions'],
    insights: ['SALES VISIBILITY','See the pattern behind the pipeline.','Use Insights to understand sales activity and give the team a clearer view of its customer work.', ['PIPELINE|See progress|Review where opportunities stand|Visibility','ACTIVITY|Understand the work|Review sales activity|Context','REVIEW|Plan the next move|Use the view to guide your team|Decision'], 'Sales analytics · Reporting · Team visibility'],
    whatsapp: ['CONNECTED CONVERSATIONS','Keep WhatsApp in the customer story.','Connect business numbers and manage chats, templates and broadcasts alongside your CRM. Available options depend on your connected account.', ['CONNECT|Business numbers|Link your WhatsApp Business account|Connection','CONVERSE|Customer chats|Keep messages and lead context close|Chats','REACH OUT|Templates & broadcasts|Manage business messaging|Messaging'], 'Connect API · Connected Numbers · Chats · Templates · Broadcasts · Hosted Account (when enabled)'],
    instagram: ['SOCIAL CONVERSATIONS','Make the DM part of the journey.','Connect Instagram professional messaging and keep customer conversations within your sales workspace.', ['CONNECT|Instagram account|Connect professional messaging|Account','REPLY|Customer DMs|Continue the conversation|Chats','CONTEXT|Sales workspace|Keep your team closer to the enquiry|Connected'], 'Connect Instagram · Chats'],
    connect: ['YOUR CONNECTED STACK','Bring your tools into the flow.','Use Connect Hub to manage integrations and connect lead sources and business tools to your sales workflow.', ['SOURCES|Bring leads in|Connect supported lead sources|Capture','CONNECTIONS|Business tools|Manage available integrations|Connect','DATA FLOW|APIs & webhooks|Configure supported data flows|Integrate'], 'Integrations · Connections · APIs · Webhooks. Call Scheduler and Call Tracker are upcoming.'],
    teams: ['PEOPLE & WORK','One workspace for your team.','Manage team members so your sales workspace reflects the people moving customer conversations forward.', ['PEOPLE|Team members|Organize your sales team|Members','WORKSPACE|Shared context|Bring people into the CRM|Collaboration','CUSTOMERS|Follow-through|Keep the conversation moving|Teamwork'], 'Teams · Members · Shared sales workspace'],
    support: ['HELP & SUPPORT','Find your next step.','Explore SHVYA documentation and request a guided sales session for your business.', ['DOCS|Product guides|Understand your sales workspace|Documentation','SESSION|Book a call|Discuss your sales process|Guidance','WORKFLOWS|Learn the essentials|Explore cadence and playbooks|Resources'], 'Read the documentation or book a sales session.']
  };
  const tabs = [...document.querySelectorAll('[data-feature]')];
  function selectFeature(button) {
    tabs.forEach(tab => {tab.setAttribute('aria-selected', String(tab === button)); tab.tabIndex = tab === button ? 0 : -1;});
    const [label,title,description,columns,note] = features[button.dataset.feature];
    document.getElementById('feature-panel').setAttribute('aria-labelledby',button.id);
    document.getElementById('panel-kicker').textContent=label;
    document.getElementById('panel-title').textContent=title;
    document.getElementById('panel-description').textContent=description;
    const demo=document.getElementById('panel-demo'); demo.replaceChildren();
    columns.forEach(column => {
      const [label,title,copy,status]=column.split('|');
      const box=document.createElement('div'); box.className='demo-column';
      const caption=document.createElement('span'); caption.textContent=label;
      const card=document.createElement('article');
      [['b',title],['p',copy],['em',status]].forEach(([tag,text])=>{const el=document.createElement(tag);el.textContent=text;card.append(el);});
      box.append(caption,card);demo.append(box);
    });
    document.getElementById('panel-note').textContent=note;
  }
  function keyboardTabs(items,activate) {
    items.forEach((button,index)=>{
      button.addEventListener('click',()=>activate(button));
      button.addEventListener('keydown',event=>{
        let next;
        if(['ArrowDown','ArrowRight'].includes(event.key)) next=(index+1)%items.length;
        if(['ArrowUp','ArrowLeft'].includes(event.key)) next=(index+items.length-1)%items.length;
        if(event.key==='Home') next=0;
        if(event.key==='End') next=items.length-1;
        if(next!==undefined){event.preventDefault();activate(items[next]);items[next].focus();}
      });
    });
  }
  keyboardTabs(tabs,selectFeature);
  document.querySelectorAll('[data-select]').forEach(link=>link.addEventListener('click',()=>selectFeature(tabs.find(tab=>tab.dataset.feature===link.dataset.select))));
  selectFeature(tabs[0]);
})();
(() => {
  'use strict';
  const page = document.querySelector('.premium-features');
  if (!page) return;
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  const pause = page.querySelector('.p-motion');
  let paused = reduced.matches, frame = 0, pointer = null;
  const states = [...page.querySelectorAll('.p-bot')].map((bot, index) => ({bot,index,x:0,y:0,tx:0,ty:0,timer:0,visible:false}));
  function mood(s, name, duration=2300) {
    clearTimeout(s.timer);
    if (paused) return;
    s.bot.dataset.mood = name;
    s.timer = setTimeout(() => { s.bot.dataset.mood='idle'; }, duration);
  }
  function animate() {
    frame=0;
    if (paused || document.hidden) return;
    let moving=false;
    states.forEach(s => {
      if (!s.visible) return;
      s.x+=(s.tx-s.x)*.095; s.y+=(s.ty-s.y)*.095;
      moving ||= Math.abs(s.tx-s.x)+Math.abs(s.ty-s.y)>.003;
      const size=s.bot.clientWidth;
      s.bot.style.setProperty('--look-x', `${s.x*size*.043}px`);
      s.bot.style.setProperty('--look-y', `${s.y*size*.034}px`);
      s.bot.style.setProperty('--tilt-y', `${s.x*18}deg`);
      s.bot.style.setProperty('--tilt-x', `${-s.y*13}deg`);
      s.bot.style.setProperty('--roll', `${s.x*6}deg`);
      s.bot.style.setProperty('--lift', `${-Math.abs(s.x)*9}px`);
    });
    if (moving) frame=requestAnimationFrame(animate);
  }
  function schedule(){if(!frame&&!paused)frame=requestAnimationFrame(animate);}
  function look(event){
    if(paused||event.pointerType==='touch')return;
    pointer={x:event.clientX,y:event.clientY};
    states.forEach(s=>{if(!s.visible)return;const r=s.bot.getBoundingClientRect();s.tx=Math.max(-1,Math.min(1,(pointer.x-r.left-r.width/2)/230));s.ty=Math.max(-1,Math.min(1,(pointer.y-r.top-r.height/2)/210));});
    schedule();
  }
  page.addEventListener('pointermove',look,{passive:true});
  page.addEventListener('pointerleave',()=>{pointer=null;states.forEach(s=>s.tx=s.ty=0);schedule();});
  const observer=new IntersectionObserver(entries=>entries.forEach(entry=>{
    const s=states.find(s=>s.bot===entry.target);s.visible=entry.isIntersecting;
    if(s.visible){mood(s,s.bot.closest('[data-scene]')?.dataset.scene||'hello');if(pointer)look({clientX:pointer.x,clientY:pointer.y});}
  }),{threshold:.3});
  states.forEach(s=>{
    observer.observe(s.bot);
    let greeting=0;
    s.bot.addEventListener('click',()=>{
      const gestures=['hello','wink','celebrate','nod'];mood(s,gestures[greeting++%gestures.length]);
      const status=page.querySelector('[data-bot-status]');
      if(s.index===0)status.textContent=['Hello! Let’s give your leads a better next step.','Your business. Your voice. Your Shvya.','A little momentum goes a long way.','Ready for the next conversation.'][(greeting-1)%4];
    });
    s.bot.addEventListener('pointerenter',()=>mood(s,'listen',1500));
    s.bot.addEventListener('focus',()=>mood(s,'hello'));
  });
  page.querySelector('[data-gesture]').addEventListener('click',()=>mood(states.find(s=>s.bot.closest('.p-companion')),'listen',4000));
  const idle=setInterval(()=>{if(paused||document.hidden||pointer)return;states.filter(s=>s.visible).forEach(s=>{s.tx=Math.sin(Date.now()/1900+s.index)*.38;s.ty=-.12;});schedule();},2600);
  function updateMotion(){page.classList.toggle('p-paused',paused);pause.setAttribute('aria-pressed',String(paused));pause.textContent=paused?'Resume motion ▶':'Pause motion Ⅱ';if(paused){cancelAnimationFrame(frame);frame=0;states.forEach(s=>{clearTimeout(s.timer);s.bot.dataset.mood='idle';s.bot.removeAttribute('style');s.x=s.y=s.tx=s.ty=0;});}}
  pause.addEventListener('click',()=>{paused=!paused;updateMotion();});
  reduced.addEventListener('change',()=>{paused=reduced.matches;updateMotion();});
  document.addEventListener('visibilitychange',()=>{if(document.hidden){cancelAnimationFrame(frame);frame=0;}else schedule();});
  window.addEventListener('pagehide',()=>{clearInterval(idle);cancelAnimationFrame(frame);states.forEach(s=>clearTimeout(s.timer));observer.disconnect();},{once:true});
  updateMotion();
  const steps=[
    ['Hi, we’re looking for an office in Gurugram.','A new conversation. A customer record. A place to begin.','CRM + CONVERSATIONS','hello'],
    ['We need space for 12 people. Ready to move.','Business knowledge and qualification instructions help guide the next question.','AI PLAYBOOK + QUALIFICATION','think'],
    ['Could you share the details with me?','A configured sequence connects a WhatsApp message, a useful email and a call reminder.','CADENCE + FOLLOW-UP','nod'],
    ['This looks right. Can we visit tomorrow?','The conversation, requirements and next action stay together for your sales team.','SALES DESK + HUMAN HANDOFF','celebrate']
  ];
  const tabs=[...page.querySelectorAll('[data-step]')];
  function step(tab){const i=Number(tab.dataset.step),data=steps[i];tabs.forEach(t=>{t.setAttribute('aria-selected',String(t===tab));t.tabIndex=t===tab?0:-1;});page.querySelector('#journey-panel').setAttribute('aria-labelledby',tab.id);['journey-message','journey-reply','journey-note'].forEach((id,j)=>{page.querySelector('#'+id).textContent=data[j];});mood(states.find(s=>s.bot.closest('.p-journey-bot')),data[3],3000);}
  tabs.forEach((tab,i)=>{tab.addEventListener('click',()=>step(tab));tab.addEventListener('keydown',e=>{let n;if(['ArrowRight','ArrowDown'].includes(e.key))n=(i+1)%4;if(['ArrowLeft','ArrowUp'].includes(e.key))n=(i+3)%4;if(e.key==='Home')n=0;if(e.key==='End')n=3;if(n!==undefined){e.preventDefault();step(tabs[n]);tabs[n].focus();}});});
  const dialog=page.querySelector('.p-film-dialog'),video=dialog.querySelector('video'),cover=page.querySelector('.p-film-cover');
  cover.addEventListener('click',()=>{dialog.showModal();video.play().catch(()=>{});});
  dialog.querySelector('.p-film-close').addEventListener('click',()=>dialog.close());
  dialog.addEventListener('close',()=>{video.pause();cover.focus();});
  dialog.addEventListener('click',event=>{if(event.target===dialog){const r=dialog.getBoundingClientRect();if(event.clientX<r.left||event.clientX>r.right||event.clientY<r.top||event.clientY>r.bottom)dialog.close();}});
})();

