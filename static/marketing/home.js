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
    if(!btn||!answer)return;
    const sync=()=>{answer.style.maxHeight=item.classList.contains('open')?answer.scrollHeight+'px':'0px';};
    sync();
    btn.addEventListener('click',()=>{
      document.querySelectorAll('.faq-item.open').forEach(other=>{
        if(other!==item){
          other.classList.remove('open');
          const otherAnswer=other.querySelector('.faq-answer');
          if(otherAnswer)otherAnswer.style.maxHeight='0px';
        }
      });
      item.classList.toggle('open');
      sync();
    });
  });

  const reveals=document.querySelectorAll('.reveal');
  if('IntersectionObserver' in window){
    const observer=new IntersectionObserver(entries=>{
      entries.forEach(entry=>{
        if(entry.isIntersecting){
          entry.target.classList.add('visible');
          observer.unobserve(entry.target);
        }
      });
    },{threshold:.12,rootMargin:'0px 0px -35px'});
    reveals.forEach(el=>observer.observe(el));
  }else{
    reveals.forEach(el=>el.classList.add('visible'));
  }
})();
