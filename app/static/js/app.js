document.querySelectorAll('[data-open-modal]').forEach((b)=>b.addEventListener('click',()=>document.getElementById(b.dataset.openModal)?.showModal()));
document.querySelectorAll('[data-close-modal]').forEach((b)=>b.addEventListener('click',()=>document.getElementById(b.dataset.closeModal)?.close()));
document.querySelectorAll('[data-copy]').forEach((b)=>b.addEventListener('click',async()=>{const t=document.querySelector(b.dataset.copy);if(!t)return;await navigator.clipboard.writeText(t.innerText);const old=b.innerText;b.innerText='Copied';setTimeout(()=>b.innerText=old,1200);}));
