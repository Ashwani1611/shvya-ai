(function(){
  const menuBtn=document.getElementById('menuBtn');
  const mobileNav=document.getElementById('mobileNav');
  if(menuBtn&&mobileNav){
    menuBtn.addEventListener('click',()=>{
      const open=mobileNav.classList.toggle('open');
      menuBtn.setAttribute('aria-expanded',String(open));
    });
    mobileNav.querySelectorAll('a').forEach(a=>a.addEventListener('click',()=>{
      mobileNav.classList.remove('open');
      menuBtn.setAttribute('aria-expanded','false');
    }));
  }

  const userMenu=document.getElementById('userMenu');
  const userTrigger=document.getElementById('userTrigger');
  if(userMenu&&userTrigger){
    const closeUser=()=>{userMenu.classList.remove('open');userTrigger.setAttribute('aria-expanded','false');};
    userTrigger.addEventListener('click',(e)=>{
      e.stopPropagation();
      const open=userMenu.classList.toggle('open');
      userTrigger.setAttribute('aria-expanded',String(open));
    });
    document.addEventListener('click',(e)=>{if(!userMenu.contains(e.target))closeUser();});
    document.addEventListener('keydown',(e)=>{if(e.key==='Escape')closeUser();});
  }

  document.querySelectorAll('.faq-item').forEach(item=>{
    const btn=item.querySelector('.faq-btn');
    const answer=item.querySelector('.faq-answer');
    const sync=()=>{answer.style.maxHeight=item.classList.contains('open')?answer.scrollHeight+'px':'0px';};
    sync();
    btn.addEventListener('click',()=>{
      document.querySelectorAll('.faq-item.open').forEach(other=>{
        if(other!==item){other.classList.remove('open');other.querySelector('.faq-answer').style.maxHeight='0px';}
      });
      item.classList.toggle('open');
      sync();
    });
  });

  const leadCount=document.getElementById('leadCount');
  const dealValue=document.getElementById('dealValue');
  const recovery=document.getElementById('recoveryRate');
  const closeRate=document.getElementById('closeRate');
  const recoveryLabel=document.getElementById('recoveryLabel');
  const closeLabel=document.getElementById('closeLabel');
  const monthlyValue=document.getElementById('monthlyValue');
  const annualValue=document.getElementById('annualValue');
  const money=new Intl.NumberFormat('en-IN',{style:'currency',currency:'INR',maximumFractionDigits:0});
  function updateCalc(){
    const leads=Math.max(0,Number(leadCount.value)||0);
    const deal=Math.max(0,Number(dealValue.value)||0);
    const recovered=(Number(recovery.value)||0)/100;
    const close=(Number(closeRate.value)||0)/100;
    const monthly=leads*recovered*close*deal;
    recoveryLabel.textContent=recovery.value+'%';
    closeLabel.textContent=closeRate.value+'%';
    monthlyValue.textContent=money.format(monthly);
    annualValue.textContent=money.format(monthly*12);
  }
  [leadCount,dealValue,recovery,closeRate].forEach(el=>el&&el.addEventListener('input',updateCalc));
  updateCalc();

  const reveals=document.querySelectorAll('.reveal');
  if('IntersectionObserver' in window){
    const observer=new IntersectionObserver(entries=>{
      entries.forEach(entry=>{if(entry.isIntersecting){entry.target.classList.add('visible');observer.unobserve(entry.target);}});
    },{threshold:.12,rootMargin:'0px 0px -35px'});
    reveals.forEach(el=>observer.observe(el));
  }else{reveals.forEach(el=>el.classList.add('visible'));}
})();
