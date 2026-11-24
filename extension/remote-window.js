mountRemote(document.getElementById('remote'), {
  keys: true,
  dropTarget: document.body,               // drop a subtitle file anywhere on the window
  openSubs: location.hash === '#cc',       // opened from the popup's "add your own .srt"
});
