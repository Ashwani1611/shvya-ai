(() => {
  'use strict';
  const features = {
    "crm": [
      "CUSTOMER CONTEXT",
      "Every lead. The whole story.",
      "Keep contacts, conversations, custom attributes, notes, pipelines and reminders together. Bring a full lead history into the next customer conversation.",
      [
        "CAPTURE|Bring enquiries together|Manage customer records and import leads|Contacts",
        "ORGANIZE|Pipelines and stages|Track progress with custom attributes|CRM",
        "FOLLOW THROUGH|Never lose the context|Use timelines, notes and reminders|Activity"
      ],
      "Contacts · Pipelines · Custom fields · Imports · Bulk actions · Reminders",
      "/docs/crm-leads/overview/"
    ],
    "sales": [
      "YOUR DAILY WORKSPACE",
      "Know what needs attention.",
      "Use Sales Desk to review lead context and recent activity, focus the next action and keep your sales team moving with a shared customer story.",
      [
        "REVIEW|See the full picture|Open the conversation and CRM context|Lead",
        "PRIORITIZE|Find the next move|Keep important follow-through visible|Focus",
        "HAND OFF|Stay in sync|Continue with the right team member|Team"
      ],
      "Sales Desk · Customer context · Actionable follow-through",
      "/docs/crm-leads/sales-desk/"
    ],
    "calendar": [
      "MEETINGS & BOOKINGS",
      "Make the next meeting easy.",
      "Publish booking pages, configure available slots and manage appointments in calendar views. Connected Google Calendar can help reconcile busy time.",
      [
        "INVITE|Share a booking page|Let customers choose an available slot|Booking",
        "SCHEDULE|Work around availability|Use notice, capacity and time-zone rules|Calendar",
        "FOLLOW UP|Keep plans connected|Review appointments and configured reminders|CRM"
      ],
      "Booking pages · Day/week/month calendar · Rescheduling · Google sync when connected",
      "/docs/sales-calendar/calendar/"
    ],
    "calls": [
      "CALL INTELLIGENCE",
      "Turn phone activity into context.",
      "Connect the Android Call Intelligence companion to the CRM. Review call logs, related leads, outcomes, notes and follow-up activity.",
      [
        "CAPTURE|Sync Android calls|Link supported SIM call activity to leads|Calls",
        "REVIEW|Understand the outcome|See call details, notes and dispositions|Context",
        "FOLLOW UP|Carry the next action|Track follow-up tasks and call activity|CRM"
      ],
      "Android companion · Call logs · Lead linking · Notes · Dispositions · Analytics",
      "/docs/operations/call-intelligence/"
    ],
    "documents": [
      "SHVYA SALES",
      "From proposal to payment.",
      "Prepare CRM-linked quotations, agreements and invoices. Generate PDFs, send through configured channels and manage available payment and post-sale actions.",
      [
        "PREPARE|Quotation or agreement|Use sales templates and revision history|Document",
        "SEND|Professional PDFs|Share through supported email or WhatsApp|Delivery",
        "CLOSE|Invoice and payments|Track eligible payments, credit notes and refunds|Sales"
      ],
      "Quotations · Agreements · Invoices · PDF · Delivery tracking · Payments",
      "/docs/sales-calendar/sales-overview/"
    ],
    "playbooks": [
      "AI PLAYBOOKS",
      "Your business. Your AI voice.",
      "Shape AI behaviour with business context, approved answers, qualification questions, knowledge URLs and files, plus language instructions. Test before going live.",
      [
        "SET UP|Explain your business|Products, services and customer needs|Context",
        "GROUND|Add trusted knowledge|FAQs, links and supporting documents|Sources",
        "REHEARSE|Try the AI Sandbox|Review sample replies and qualification flow|Testing"
      ],
      "AI Brain · FAQs · Knowledge sources · Qualification · Languages · Sandbox",
      "/docs/ai-automation/ai-brain/"
    ],
    "engagement": [
      "AI ENGAGEMENT",
      "Helpful answers, guided by context.",
      "Configure AI-assisted conversations across supported WhatsApp and Instagram connections. Guide qualification, capture responses and escalate to your team when appropriate.",
      [
        "RESPOND|Use approved context|Answer enquiries with the configured Playbook|Reply",
        "QUALIFY|Capture what matters|Ask questions and save eligible CRM attributes|Lead",
        "HAND OFF|Keep people involved|Route next steps through configured rules|Team"
      ],
      "AI replies · Qualification · CRM updates · Language settings · Human handoff",
      "/docs/ai-automation/qualification/"
    ],
    "cadence": [
      "CADENCE & TOUCHPOINTS",
      "Stay in touch, with purpose.",
      "Build ordered WhatsApp, Instagram, email and call-reminder sequences. Reuse Touchpoints with CRM attribute placeholders and file attachments.",
      [
        "SEQUENCES|Plan each step|Set timing and supported delivery channels|Cadence",
        "TOUCHPOINTS|Personalize a reply|Use saved CRM placeholders and attachments|Reusable",
        "FOLLOW THROUGH|Keep the next step clear|Review message and reminder activity|Team"
      ],
      "Sequences · Touchpoints · CRM placeholders · Attachments · Reminder steps",
      "/docs/ai-automation/quick-replies/"
    ],
    "workflows": [
      "WORKFLOWS",
      "Make your rules work for you.",
      "Connect CRM events to your configured conditions and actions. Use execution history to review what ran, what was skipped and what needs attention.",
      [
        "TRIGGER|A lead changes|Start from an eligible CRM event|Event",
        "CHECK|Apply your conditions|Match the configured rules|Logic",
        "ACT|Run the next step|Review action and execution history|Workflow"
      ],
      "Event triggers · Conditional logic · Configured actions · Execution history",
      "/docs/ai-automation/workflows/"
    ],
    "insights": [
      "INSIGHTS",
      "See the work behind the numbers.",
      "Review pipeline and sales activity trends. Where messaging data is available, use campaign and template delivery evidence to understand follow-through.",
      [
        "PIPELINES|Spot movement|Understand lead stage progress|CRM",
        "ACTIVITY|Review engagement|Bring follow-up work into focus|Team",
        "MESSAGING|Examine results|Inspect eligible campaign and template metrics|Metrics"
      ],
      "CRM reports · Activity analytics · Messaging metrics where available",
      "/docs/ai-automation/insights/"
    ],
    "whatsapp": [
      "WHATSAPP",
      "Stay close to every conversation.",
      "Use supported Cloud API, Coexistence or enabled Hosted connections to manage customer chats, approved templates and bulk campaigns alongside CRM context.",
      [
        "CONNECT|Choose the right connection|API, Coexistence or Hosted when enabled|Channel",
        "CONVERSE|See customer messages|Work with CRM-linked chats and replies|Inbox",
        "CAMPAIGNS|Reach eligible leads|Use approved templates and delivery receipts|WhatsApp"
      ],
      "Connected numbers · Chats · Templates · Bulk campaigns · 24-hour window rules",
      "/docs/messaging-channels/overview/"
    ],
    "instagram": [
      "INSTAGRAM",
      "Turn a DM into a customer story.",
      "Connect professional Instagram messaging, respond to enquiries and link conversations to CRM leads. You can capture a phone number later if the customer provides one.",
      [
        "CONNECT|Professional account|Authorize Instagram messaging access|Connection",
        "CONVERSE|Manage enquiries|Work from the Instagram DM inbox|DMs",
        "CAPTURE|Link the customer|Create or connect a CRM lead as appropriate|CRM"
      ],
      "Instagram connection · DM inbox · Lead association · CRM context",
      "/docs/messaging-channels/instagram/"
    ],
    "connect": [
      "CONNECT HUB",
      "Bring your tools together.",
      "Connect supported lead sources and business systems, including JustDial, IndiaMART, Meta Lead Ads, Google Sheets and email. Configure APIs or webhooks for eligible integrations.",
      [
        "LEADS|Capture external enquiries|JustDial, IndiaMART and Meta Lead Forms|Sources",
        "SYNC|Work with your data|Google Sheets and connected email|Connections",
        "EXTEND|Build on SHVYA|Organization API and webhook options|Integration"
      ],
      "Lead sources · Google Sheets · Meta · Email · SHVYA API · Webhooks",
      "/docs/connect-hub/overview/"
    ],
    "vault": [
      "CLIENT ONBOARDING",
      "Give every client a place to start.",
      "A staff-managed private Vault link lets clients contribute business notes, links and files. Setup information is reviewed before any live AI or CRM changes.",
      [
        "INVITE|Private client workspace|Share an access-controlled setup page|Vault",
        "COLLECT|Gather what matters|Brochures, answers, notes and resources|Sources",
        "REVIEW|Build with confidence|Staff review the setup draft before changes|Onboarding"
      ],
      "SHVYA Vault · Private links · Client uploads · Staff review · Not a CRM sidebar module",
      "/docs/operations/shvya-vault/"
    ],
    "teams": [
      "TEAMS",
      "People, roles and responsibility.",
      "Organize team members and support their work with CRM-scoped access, lead ownership and shared customer history.",
      [
        "PEOPLE|Manage your team|Keep members and roles organized|Users",
        "OWNERSHIP|Route the right lead|Work with allowed pipelines and assignees|Access",
        "COLLABORATE|Share the context|Keep customer activity connected|Team"
      ],
      "Team members · Roles · Pipeline access · Lead ownership",
      "/docs/operations/teams/"
    ],
    "support": [
      "HELP & SUPPORT",
      "Support that stays on record.",
      "Open organization-scoped support tickets, track their status and exchange replies with SHVYA Ops. Product guides help with common setup and troubleshooting tasks.",
      [
        "RAISE|Create a support ticket|Describe the issue and useful context|Ticket",
        "TRACK|Follow the conversation|Check replies and ticket status|Updates",
        "LEARN|Find setup guidance|Use product docs and troubleshooting guides|Docs"
      ],
      "Ticket portal · Status and reply history · SHVYA Ops · Documentation",
      "/docs/operations/support-tickets/"
    ]
  };
  const tabs = [...document.querySelectorAll('[data-feature]')];
  function selectFeature(button) {
    tabs.forEach(tab => {tab.setAttribute('aria-selected', String(tab === button)); tab.tabIndex = tab === button ? 0 : -1;});
    const data = features[button.dataset.feature];
    if (!data) return;
    const [label,title,description,columns,note,guideUrl] = data;
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
    document.getElementById('panel-guide').setAttribute('href', guideUrl);
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
        if(next!==undefined){event.preventDefault();activate(items[next]);items[next].focus();items[next].scrollIntoView({block:'nearest',inline:'nearest'});}
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

