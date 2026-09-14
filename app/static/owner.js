(function () {
'use strict';
document.querySelectorAll('[data-dismiss]').forEach(b => b.addEventListener('click', () => b.parentElement.remove()));
document.querySelectorAll('form[data-confirm]').forEach(form => form.addEventListener('submit', event => {
  if(!window.confirm(form.dataset.confirm)) event.preventDefault();
}));
document.querySelectorAll('[data-upload-zone]').forEach(zone => {
  const input = zone.querySelector('[data-upload-input]');
  const previews = zone.parentElement.querySelector('[data-upload-previews]');
  let urls = [];
  function renderPreviews(){
    urls.forEach(URL.revokeObjectURL);urls=[];previews.replaceChildren();
    const files = Array.from(input.files);
    const total = files.reduce((sum,f)=>sum+f.size,0);
    const invalid = files.some(f=>f.size>3*1024*1024 || !['image/jpeg','image/png','image/webp'].includes(f.type));
    if(invalid || files.length>12 || total>3*1024*1024){
      input.value=''; const error=document.createElement('p');error.className='form-error';
      error.textContent='Please choose JPG, PNG, or WebP images with a combined upload size below 3 MB. On Vercel, upload larger product photos one at a time after compressing them.';
      previews.append(error);return;
    }
    files.forEach(file=>{
      const div=document.createElement('div');div.className='upload-preview';
      const image=document.createElement('img');const url=URL.createObjectURL(file);urls.push(url);image.src=url;image.alt='Upload preview';
      const caption=document.createElement('small');caption.textContent=file.name;
      div.append(image,caption);previews.append(div);
    });
  }
  input.addEventListener('change',renderPreviews);
  ['dragenter','dragover'].forEach(t=>zone.addEventListener(t,e=>{e.preventDefault();zone.classList.add('dragging');}));
  ['dragleave','drop'].forEach(t=>zone.addEventListener(t,()=>zone.classList.remove('dragging')));
  zone.addEventListener('drop',e=>{
    e.preventDefault();if(e.dataTransfer?.files.length){input.files=e.dataTransfer.files;renderPreviews();}
  });
});

})();
