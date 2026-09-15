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
    support: ['HELP & SUPPORT','A home for help.','Help & Support has a place in the sidebar. The dedicated Support Portal is marked as coming soon.', ['SIDEBAR|Help & Support|Find the entry in your workspace|Navigation','PORTAL|Support Portal|Dedicated support experience|Coming soon','STATUS|In development|Not yet an available support workflow|Upcoming'], 'Support Portal is coming soon.']
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
  const jobs = [
    ['CRM + CONVERSATIONS','Turn “hello” into a beginning.','Bring the enquiry and the conversation into the same customer story, so the first reply starts with context.'],
    ['AI PLAYBOOK + QUALIFICATION','Understand what matters first.','Give Shvya your business context and qualification requirements, so customer engagement has a useful direction.'],
    ['CADENCE + FOLLOW-UP','Make the next step a habit.','Connect WhatsApp messages, emails and call reminders in a reusable sequence that matches your sales process.'],
    ['SALES DESK + CRM','Make the handoff feel seamless.','Keep the customer history, notes and pipeline stage close at hand, so your team can focus on the conversation.']
  ];
  const jobTabs=[...document.querySelectorAll('[data-job]')];
  keyboardTabs(jobTabs,button=>{
    jobTabs.forEach(tab=>{tab.setAttribute('aria-selected',String(tab===button));tab.tabIndex=tab===button?0:-1;});
    const [label,title,copy]=jobs[Number(button.dataset.job)];
    document.getElementById('job-panel').setAttribute('aria-labelledby',button.id);
    document.getElementById('job-label').textContent=label;document.getElementById('job-title').textContent=title;document.getElementById('job-copy').textContent=copy;
  });
  const motion=matchMedia('(prefers-reduced-motion: reduce)');
  const bots=[...document.querySelectorAll('.shvya-bot')];
  let frame=0,mouseX=0,mouseY=0;
  function resetBots(){bots.forEach(bot=>['--rx','--ry','--eye-x','--eye-y'].forEach(name=>bot.style.removeProperty(name)));}
  document.addEventListener('pointermove',event=>{
    if(motion.matches||event.pointerType==='touch')return;
    mouseX=event.clientX;mouseY=event.clientY;
    if(frame)return;
    frame=requestAnimationFrame(()=>{
      bots.forEach(bot=>{const r=bot.getBoundingClientRect();if(r.bottom<0||r.top>innerHeight)return;const x=Math.max(-1,Math.min(1,(mouseX-r.left-r.width/2)/260));const y=Math.max(-1,Math.min(1,(mouseY-r.top-r.height/2)/260));bot.style.setProperty('--ry',`${x*12}deg`);bot.style.setProperty('--rx',`${-y*9}deg`);bot.style.setProperty('--eye-x',`${x*r.width*.045}px`);bot.style.setProperty('--eye-y',`${y*r.width*.04}px`);});frame=0;
    });
  },{passive:true});
  document.documentElement.addEventListener('pointerleave',resetBots);
  window.addEventListener('blur',resetBots);
  motion.addEventListener('change',resetBots);
})();
