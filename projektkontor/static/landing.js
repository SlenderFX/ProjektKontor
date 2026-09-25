const menuButton=document.querySelector('.menu-button');
const siteNav=document.querySelector('#site-nav');
menuButton.addEventListener('click',()=>{const open=menuButton.getAttribute('aria-expanded')==='true';menuButton.setAttribute('aria-expanded',String(!open));siteNav.classList.toggle('open',!open)});
siteNav.querySelectorAll('a').forEach(link=>link.addEventListener('click',()=>{menuButton.setAttribute('aria-expanded','false');siteNav.classList.remove('open')}));

const sectionLinks=[...siteNav.querySelectorAll('a[href^="#"]')];
const sectionTargets=sectionLinks.map(link=>({link,target:document.querySelector(link.getAttribute('href'))})).filter(item=>item.target);
const prefersReducedMotion=window.matchMedia('(prefers-reduced-motion: reduce)');
function centerSection(target,behavior=prefersReducedMotion.matches?'auto':'smooth'){
  const headerHeight=document.querySelector('.site-header')?.offsetHeight||0;
  const section=target.closest('section')||target;
  const rect=section.getBoundingClientRect();
  const availableHeight=window.innerHeight-headerHeight;
  const breathingRoom=Math.min(48,Math.max(24,availableHeight*.06));
  const sectionTop=window.scrollY+rect.top;
  const top=rect.height<=availableHeight-breathingRoom*2
    ? sectionTop-(availableHeight-rect.height)/2-headerHeight
    : sectionTop-headerHeight-breathingRoom;
  window.scrollTo({top,behavior});
}
sectionTargets.forEach(({link,target})=>link.addEventListener('click',event=>{
  event.preventDefault();
  history.pushState(null,'',link.getAttribute('href'));
  centerSection(target);
  if(event.detail>0)link.blur();
}));
window.addEventListener('popstate',()=>{
  const target=document.querySelector(location.hash);
  if(target)centerSection(target);
});
if(location.hash){
  const initialTarget=document.querySelector(location.hash);
  if(initialTarget)requestAnimationFrame(()=>centerSection(initialTarget,'auto'));
}
let navigationFrame=0;
function updateCurrentSection(){
  navigationFrame=0;
  const headerHeight=document.querySelector('.site-header')?.offsetHeight||0;
  const viewportTop=headerHeight;
  const viewportBottom=window.innerHeight;
  let current=null;
  let largestVisibleArea=0;
  sectionTargets.forEach(item=>{
    const section=item.target.closest('section')||item.target;
    const rect=section.getBoundingClientRect();
    const visibleArea=Math.max(0,Math.min(rect.bottom,viewportBottom)-Math.max(rect.top,viewportTop));
    if(visibleArea>largestVisibleArea){largestVisibleArea=visibleArea;current=item}
  });
  sectionLinks.forEach(link=>link.removeAttribute('aria-current'));
  if(current)current.link.setAttribute('aria-current','location');
}
window.addEventListener('scroll',()=>{if(!navigationFrame)navigationFrame=requestAnimationFrame(updateCurrentSection)},{passive:true});
window.addEventListener('resize',updateCurrentSection);
updateCurrentSection();

const productTabs=[...document.querySelectorAll('[data-product-view]')];
const productViews=[...document.querySelectorAll('.product-view[data-view]')];
const previewNavigation=[...document.querySelectorAll('.preview-sidebar span')];
function showProductView(name){
  productTabs.forEach((button,index)=>{
    const active=button.dataset.productView===name;
    button.classList.toggle('active',active);
    button.setAttribute('aria-selected',String(active));
    button.tabIndex=active?0:-1;
    previewNavigation[index]?.classList.toggle('active',active);
  });
  productViews.forEach(view=>{
    const active=view.dataset.view===name;
    view.hidden=!active;
    view.classList.toggle('active',active);
  });
}
productTabs.forEach((button,index)=>{
  button.addEventListener('click',()=>showProductView(button.dataset.productView));
  button.addEventListener('keydown',event=>{
    if(!['ArrowLeft','ArrowRight'].includes(event.key))return;
    event.preventDefault();
    const offset=event.key==='ArrowRight'?1:-1;
    const next=productTabs[(index+offset+productTabs.length)%productTabs.length];
    showProductView(next.dataset.productView);
    next.focus();
  });
});

if(!prefersReducedMotion.matches&&'IntersectionObserver' in window){
  const revealItems=[...document.querySelectorAll('main>.section, .proof-strip')];
  revealItems.forEach(item=>item.classList.add('reveal-item'));
  const revealObserver=new IntersectionObserver(entries=>entries.forEach(entry=>{
    if(entry.isIntersecting){entry.target.classList.add('is-visible');revealObserver.unobserve(entry.target)}
  }),{rootMargin:'0px 0px -8% 0px',threshold:.08});
  revealItems.forEach(item=>revealObserver.observe(item));
}

const contactForm=document.querySelector('#contact-form');
const contactStatus=document.querySelector('#contact-status');
const contactButton=contactForm.querySelector('button[type="submit"]');
const turnstileContainer=document.querySelector('#turnstile-container');
contactButton.disabled=true;

const betaContactLinks=document.querySelectorAll('.beta-contact-link');
const pilotQualification=document.querySelector('#pilot-qualification');
function setPilotQualification(active){
  if(!pilotQualification)return;
  pilotQualification.hidden=!active;
  pilotQualification.querySelectorAll('input,select').forEach(control=>{
    control.disabled=!active;
    if(['organization','usage_outlook'].includes(control.name))control.required=active;
  });
}
function openPreparedContact(subjectText,messageText,pilot=false){
  const subject=contactForm.elements.namedItem('subject');
  const message=contactForm.elements.namedItem('message');
  setPilotQualification(pilot);
  if(subject)subject.value=subjectText;
  if(message&&!message.value.trim())message.value=messageText;
  history.pushState(null,'','#kontakt');
  centerSection(document.querySelector('#kontakt'));
}
function prepareBetaRequest(event){
  event.preventDefault();
  openPreparedContact('Vierwöchiger ProjektKontor-Pilot','Ich möchte ProjektKontor in einem konkreten Unterrichtsvorhaben vier Wochen lang erproben.',true);
}
betaContactLinks.forEach(link=>link.addEventListener('click',prepareBetaRequest));
document.querySelectorAll('.contact-prefill-link').forEach(link=>link.addEventListener('click',event=>{
  event.preventDefault();
  openPreparedContact(link.dataset.subject||'Interesse an ProjektKontor',link.dataset.message||'Ich interessiere mich für ProjektKontor.',false);
}));
async function prepareHumanCheck(){
  try{
    const response=await fetch('/api/contact/config',{headers:{Accept:'application/json'}});
    const config=await response.json();
    if(!response.ok||!config.turnstile_sitekey)throw new Error('Die Menschprüfung ist noch nicht eingerichtet.');
    turnstileContainer.replaceChildren();
    const widget=document.createElement('div');
    widget.className='cf-turnstile';
    widget.dataset.sitekey=config.turnstile_sitekey;
    widget.dataset.theme='light';
    widget.dataset.size='flexible';
    widget.dataset.language='de';
    turnstileContainer.append(widget);
    const script=document.createElement('script');
    script.src='https://challenges.cloudflare.com/turnstile/v0/api.js';
    script.async=true;
    script.defer=true;
    script.addEventListener('load',()=>{contactButton.disabled=false});
    script.addEventListener('error',()=>{turnstileContainer.innerHTML='<p>Die Menschprüfung konnte nicht geladen werden.</p>';contactStatus.textContent='Bitte laden Sie die Seite neu.';contactStatus.classList.add('error')});
    document.head.append(script);
  }catch(error){
    turnstileContainer.innerHTML='<p>Das Kontaktformular ist derzeit nicht verfügbar.</p>';
    contactStatus.textContent=error.message;
    contactStatus.classList.add('error');
  }
}

contactForm.addEventListener('submit',async event=>{
  event.preventDefault();
  contactStatus.textContent='';
  contactStatus.classList.remove('error');
  const fields=new FormData(contactForm);
  const token=fields.get('cf-turnstile-response')||'';
  if(!token){contactStatus.textContent='Bitte führen Sie die Menschprüfung durch.';contactStatus.classList.add('error');return}
  contactButton.disabled=true;
  try{
    const response=await fetch('/api/contact',{method:'POST',headers:{'Content-Type':'application/json','Accept':'application/json'},body:JSON.stringify({name:fields.get('name'),email:fields.get('email'),organization:fields.get('organization'),pilot_start:fields.get('pilot_start'),usage_outlook:fields.get('usage_outlook'),subject:fields.get('subject'),message:fields.get('message'),website:fields.get('website'),privacy_accepted:fields.get('privacy_accepted')==='on',turnstile_token:token})});
    const result=await response.json();
    if(!response.ok)throw new Error(result.error||'Die Anfrage konnte nicht versendet werden.');
    contactForm.reset();
    contactStatus.textContent=result.message;
  }catch(error){
    contactStatus.textContent=error.message;
    contactStatus.classList.add('error');
  }finally{
    if(window.turnstile)window.turnstile.reset();
    contactButton.disabled=false;
  }
});

prepareHumanCheck();
