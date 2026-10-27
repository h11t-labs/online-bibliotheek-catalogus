// Which browser this is, as one class on <html>, so the stylesheet can show the
// wording and the store button that belong to it (the invitation under the borrow
// button in book.html, the "Installeren" section of extensie.html). Exactly one of
// is-chrome, is-edge, is-chromium (Brave, Opera, Vivaldi: they install from the
// Chrome Web Store too) or is-other, plus is-mobile on a phone. Nothing leaves the
// browser; the server never knows. Without this script the pages keep their neutral
// wording, which is also what a browser that is none of these gets.
(function () {
  var html = document.documentElement;
  var data = navigator.userAgentData;
  var ua = navigator.userAgent || '';
  var browser = 'is-other';
  var mobile;
  if (data && data.brands && data.brands.length) {
    var brands = data.brands.map(function (b) { return b.brand; }).join(' ');
    if (brands.indexOf('Microsoft Edge') !== -1) browser = 'is-edge';
    else if (brands.indexOf('Google Chrome') !== -1) browser = 'is-chrome';
    else if (brands.indexOf('Chromium') !== -1) browser = 'is-chromium';
    mobile = data.mobile === true;
  } else {
    if (/Edg\//.test(ua)) browser = 'is-edge';
    else if (/OPR\/|Vivaldi\//.test(ua)) browser = 'is-chromium';
    else if (/Chrome\//.test(ua)) browser = 'is-chrome';
    mobile = /Mobi|Android|iPhone|iPad|iPod/i.test(ua);
  }
  html.classList.add(browser);
  if (mobile) html.classList.add('is-mobile');
})();
