(function () {
  "use strict";

  const MESSAGES = [
    "Kano kept Lisa consistent when her motivation ran out",
    "Kano helped Lena lose 6 kg — without giving up pasta",
    "Kano helped Chris add 20 kg to his deadlift in 8 weeks",
    "Kano turned Mia's Sunday slump into her best training day",
    "Kano pushed Marco to show up — even on bad days",
    "Kano finally got Tom to hit his protein goals every day",
    "Kano coached Nina from the couch to her first 5K",
  ];

  const ROTATE_MS = 4000;
  const FADE_MS = 300;

  const pill = document.getElementById("rotating-pill");
  if (!pill) return;

  // Skip rotation entirely for users who prefer reduced motion.
  const reduceMotion = window.matchMedia(
    "(prefers-reduced-motion: reduce)"
  ).matches;
  if (reduceMotion) return;

  let index = 0;
  let fadeTimeoutId = null;

  const intervalId = window.setInterval(function () {
    pill.classList.add("is-fading");
    fadeTimeoutId = window.setTimeout(function () {
      index = (index + 1) % MESSAGES.length;
      pill.textContent = MESSAGES[index];
      pill.classList.remove("is-fading");
      fadeTimeoutId = null;
    }, FADE_MS);
  }, ROTATE_MS);

  // pagehide covers tab close, navigation, and bfcache eviction.
  window.addEventListener(
    "pagehide",
    function cleanup() {
      window.clearInterval(intervalId);
      if (fadeTimeoutId !== null) window.clearTimeout(fadeTimeoutId);
    },
    { once: true }
  );
})();
