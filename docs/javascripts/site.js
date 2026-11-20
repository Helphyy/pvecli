// Recordings of pvecli's interactive menus (docs/assets/casts, made by
// scripts/docs/casts.py): still on their first frame until scrolled into
// view, then played. Runs again after each instant navigation.
(function () {
  function mountCasts() {
    if (!window.AsciinemaPlayer) return;
    const seen = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting && entry.target._player) {
          entry.target._player.play();
          seen.unobserve(entry.target);
        }
      });
    }, { threshold: 0.5 });
    document.querySelectorAll(".cast[data-src]:not([data-ready])").forEach((el) => {
      el.dataset.ready = "1";
      const options = {
        theme: "pvecli",
        fit: "width",
        loop: true,
        autoPlay: false,
        idleTimeLimit: 1.5,
        terminalFontFamily: '"pvecli Box", "JetBrains Mono", monospace',
        terminalLineHeight: 1.25,
        controls: true,
        poster: el.dataset.poster || "npt:0:3",
      };
      if (el.dataset.rows) options.rows = Number(el.dataset.rows);
      el._player = window.AsciinemaPlayer.create(el.dataset.src, el, options);
      seen.observe(el);
    });
  }
  if (window.document$ && window.document$.subscribe) {
    window.document$.subscribe(mountCasts);
  } else {
    document.addEventListener("DOMContentLoaded", mountCasts);
  }
})();
