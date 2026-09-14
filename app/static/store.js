(function () {
'use strict';
const menuToggle = document.querySelector('.mobile-menu-toggle');
const menu = document.querySelector('#site-nav');
menuToggle?.addEventListener('click', () => {
  const open = menu.classList.toggle('open');
  menuToggle.setAttribute('aria-expanded', String(open));
  menuToggle.setAttribute('aria-label', open ? 'Close navigation' : 'Open navigation');
});
const searchDialog = document.querySelector('.search-dialog');
document.querySelector('[data-search-open]')?.addEventListener('click', () => {
  searchDialog.showModal(); document.querySelector('#search-field')?.focus();
});
document.querySelector('.search-close')?.addEventListener('click', () => searchDialog.close());
searchDialog?.addEventListener('click', e => {if(e.target === searchDialog) searchDialog.close();});
document.querySelectorAll('[data-dismiss]').forEach(b => b.addEventListener('click', () => b.parentElement.remove()));
document.querySelectorAll('[data-gallery-src]').forEach(button => button.addEventListener('click', () => {
  const image = document.querySelector('#main-product-image');
  image.src = button.dataset.gallerySrc; image.alt = button.dataset.galleryAlt;
  document.querySelectorAll('[data-gallery-src]').forEach(b => b.classList.toggle('selected', b === button));
}));
const zoomDialog = document.querySelector('.zoom-dialog');
document.querySelector('[data-zoom]')?.addEventListener('click', () => {
  const source = document.querySelector('#main-product-image');
  const target = zoomDialog.querySelector('img');
  target.src = source.src; target.alt = source.alt; zoomDialog.showModal();
});
document.querySelector('.zoom-close')?.addEventListener('click', () => zoomDialog.close());
zoomDialog?.addEventListener('click', e => {if (e.target === zoomDialog) zoomDialog.close();});
const variantSelect = document.querySelector('#variant-selector');
function updateVariantPrice(){
  const option = variantSelect?.selectedOptions[0];
  if(option && document.querySelector('#variant-price')) document.querySelector('#variant-price').textContent = option.dataset.price;
}
variantSelect?.addEventListener('change', updateVariantPrice);updateVariantPrice();

})();
