(function jarvisDefaultLanding() {
  try {
    var path = location.pathname || "/";
    if (path.indexOf("/jarvis-orb") === 0) return;
    if (path !== "/" && path !== "") return;
    if (/[?&]chat=1\b/.test(location.search || "")) return;
    if (/^\/(auth|signin|signup|oauth|sso)/i.test(path)) return;

    function hasToken() {
      try {
        return Boolean(localStorage.getItem("token"));
      } catch (_) {
        return false;
      }
    }

    function go() {
      if (/[?&]chat=1\b/.test(location.search || "")) return;
      if ((location.pathname || "/") !== "/" && location.pathname !== "") return;
      if (!hasToken()) return;
      location.replace("/jarvis-orb/");
    }

    go();
    var tries = 0;
    var timer = setInterval(function () {
      tries += 1;
      go();
      if (tries > 45) clearInterval(timer);
    }, 1000);
  } catch (_) {}
})();
