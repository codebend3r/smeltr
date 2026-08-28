
/* Runs before first paint. Anything later flashes the wrong theme on load. */
(function(){
  var v=null;
  try{ v=localStorage.getItem("smeltr.theme"); }catch(e){}
  if(v!=="light"&&v!=="dark"){
    v=(window.matchMedia&&window.matchMedia("(prefers-color-scheme: light)").matches)
      ? "light" : "dark";
  }
  document.documentElement.setAttribute("data-theme",v);
})();
