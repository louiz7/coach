(() => {
  const clamp = (n, lo, hi) => Math.max(lo, Math.min(hi, n));

  const initCompareSlider = (slider) => {
    const beforeBadge = slider.querySelector(".compare__badge--before");
    const afterBadge = slider.querySelector(".compare__badge--after");

    const updateBadges = (pct) => {
      const sliderRect = slider.getBoundingClientRect();
      if (!sliderRect.width) return;
      const dividerX = (pct / 100) * sliderRect.width;
      if (beforeBadge) {
        const r = beforeBadge.getBoundingClientRect();
        const rightEdge = r.right - sliderRect.left;
        beforeBadge.classList.toggle("is-hidden", dividerX <= rightEdge);
      }
      if (afterBadge) {
        const r = afterBadge.getBoundingClientRect();
        const leftEdge = r.left - sliderRect.left;
        afterBadge.classList.toggle("is-hidden", dividerX >= leftEdge);
      }
    };

    const setPos = (pct) => {
      const v = clamp(pct, 0, 100);
      slider.style.setProperty("--pos", v + "%");
      slider.setAttribute("aria-valuenow", String(Math.round(v)));
      updateBadges(v);
    };
    const setFromClientX = (clientX) => {
      const rect = slider.getBoundingClientRect();
      if (!rect.width) return;
      setPos(((clientX - rect.left) / rect.width) * 100);
    };

    let dragging = false;
    let activePointer = null;

    slider.addEventListener("pointerdown", (e) => {
      dragging = true;
      activePointer = e.pointerId;
      slider.setPointerCapture?.(e.pointerId);
      setFromClientX(e.clientX);
      slider.focus({ preventScroll: true });
    });
    slider.addEventListener("pointermove", (e) => {
      if (!dragging || e.pointerId !== activePointer) return;
      setFromClientX(e.clientX);
    });
    const endDrag = (e) => {
      if (e.pointerId !== activePointer) return;
      dragging = false;
      activePointer = null;
      slider.releasePointerCapture?.(e.pointerId);
    };
    slider.addEventListener("pointerup", endDrag);
    slider.addEventListener("pointercancel", endDrag);

    slider.addEventListener("keydown", (e) => {
      const cur = parseFloat(slider.getAttribute("aria-valuenow") || "50");
      const step = e.shiftKey ? 10 : 2;
      let next = cur;
      switch (e.key) {
        case "ArrowLeft":
        case "ArrowDown":
          next = cur - step;
          break;
        case "ArrowRight":
        case "ArrowUp":
          next = cur + step;
          break;
        case "Home":
          next = 0;
          break;
        case "End":
          next = 100;
          break;
        default:
          return;
      }
      e.preventDefault();
      setPos(next);
    });

    setPos(parseFloat(slider.getAttribute("aria-valuenow") || "50"));

    // Auto-tease: when the slider first scrolls into view, briefly drag
    // it right → left → centre so the user sees it's interactive.
    const reducedMotion = window.matchMedia(
      "(prefers-reduced-motion: reduce)"
    ).matches;
    if (reducedMotion) return;

    let teased = false;
    const tease = () => {
      if (teased) return;
      teased = true;
      slider.classList.add("is-teasing");
      const timeouts = [
        setTimeout(() => setPos(78), 250),
        setTimeout(() => setPos(22), 1100),
        setTimeout(() => setPos(50), 1900),
        setTimeout(() => slider.classList.remove("is-teasing"), 2600),
      ];
      const cancel = () => {
        timeouts.forEach(clearTimeout);
        slider.classList.remove("is-teasing");
      };
      // Capture phase so we strip the transition class BEFORE the existing
      // pointerdown handler sets --pos, otherwise the drag would lag.
      slider.addEventListener("pointerdown", cancel, {
        once: true,
        capture: true,
      });
      slider.addEventListener("keydown", cancel, { once: true });
    };

    const io = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting) {
            tease();
            io.unobserve(slider);
          }
        });
      },
      { threshold: 0.3 }
    );
    io.observe(slider);
  };

  const setCompareLayerImage = (el, file) => {
    if (!el) return;
    if (file) {
      el.style.backgroundImage = `url("/static/images/${file}")`;
      el.style.backgroundSize = "cover";
      el.style.backgroundPosition = "center";
    } else {
      el.style.backgroundImage = "";
      el.style.backgroundSize = "";
      el.style.backgroundPosition = "";
    }
  };

  const setCompareProfile = (person) => {
    if (!person) return;
    const titleEm = document.querySelector(".compare__title em");
    const subtextLead = document.querySelector(".compare__subtext-lead");
    const subtextTail = document.querySelector(".compare__subtext-tail");
    if (titleEm) titleEm.textContent = person.name;
    if (subtextLead) subtextLead.textContent = `With Kano, ${person.name}`;
    if (subtextTail) subtextTail.textContent = person.achievement || "";
    setCompareLayerImage(
      document.querySelector(".compare__before"),
      person.before
    );
    setCompareLayerImage(
      document.querySelector(".compare__after"),
      person.after
    );
  };

  document.querySelectorAll("[data-compare]").forEach(initCompareSlider);

  /* ----- Rotating testimonial pill above the hero ----- */
  const TESTIMONIAL_PHRASES = [
    "Alex lost over 9kg in 12 weeks with Kano",
    "Marcus gained 6kg of lean mass with Kano",
    "Luke ran his 10k in record time with Kano",
    "Mary is hitting the gym regulary with Kano",
    "Robert ran his first marathon with Kano",
    "Elisa fixed her eating habits to live healthier",
  ];

  const initTestimonialPill = (pill) => {
    const text = pill.querySelector(".testimonial-pill__text");
    if (!text) return;
    let i = TESTIMONIAL_PHRASES.indexOf(text.textContent.trim());
    if (i < 0) i = 0;
    setInterval(() => {
      text.classList.add("is-fading");
      setTimeout(() => {
        i = (i + 1) % TESTIMONIAL_PHRASES.length;
        text.textContent = TESTIMONIAL_PHRASES[i];
        text.classList.remove("is-fading");
      }, 300);
    }, 3500);
  };

  document
    .querySelectorAll("[data-testimonial-pill]")
    .forEach(initTestimonialPill);

  /* ----- Goal selector drives features + testimonial ----- */
  const GOAL_CONFIG = {
    "lose-weight": {
      person: {
        name: "Alex",
        achievement: "lost 9kg in 12 weeks",
        before: "before-alex.png",
        after: "after-alex.png",
      },
      title: "Lose weight<br>with Kano",
      subtext:
        "Nutrition coaching, adaptive workout plans and daily accountability to get you to your goal",
      closingLead: "Your body transformation",
      cards: {
        nutrition: {
          order: 1,
          body: "Snap a photo and see exactly how it fits your weight loss goal",
          image: "feature-card-loseweight-nutrition.png",
        },
        plans: {
          order: 2,
          body: "Kano reads your body data and builds a plan that burns fat and preserves muscle",
          image: "feature-card-loseweight-plans.png",
        },
        schedule: {
          order: 3,
          body: "Kano knows when you're free and makes sure you never miss a session",
          image: "feature-card-schedule.png",
        },
      },
    },
    "build-muscle": {
      person: {
        name: "Marcus",
        achievement: "gained 6kg of lean mass",
        before: "before-marcus.png",
        after: "after-marcus.png",
      },
      title: "Build muscle<br>with Kano",
      subtext:
        "Progressive training plans, smart nutrition and the consistency that actually builds mass",
      closingLead: "The physique you want",
      cards: {
        plans: {
          order: 1,
          body: "Kano tracks your progress and pushes you when your body is ready for more",
          image: "feature-card-buildmuscle-plans.png",
        },
        nutrition: {
          order: 2,
          body: "Snap a photo and see exactly how it fits your muscle building goal",
          image: "feature-card-buildmuscle-nutrition.png",
        },
        schedule: {
          order: 3,
          body: "Kano knows when you're free and makes sure you never miss a session",
          image: "feature-card-schedule.png",
        },
      },
    },
    "level-up": {
      person: {
        name: "Luke",
        achievement: "ran his first sub-20 5K",
        before: "before-luke.png",
        after: "after-luke.png",
      },
      title: "Optimize your performance with Kano",
      subtext:
        "Data-driven coaching, optimized training and the structure to hit your next milestone",
      closingLead: "Your best performance",
      cards: {
        plans: {
          order: 1,
          body: "Kano reads your wearable data and structures every week around your next goal",
          image: "feature-card-levelup-plans.png",
        },
        nutrition: {
          order: 2,
          body: "Snap a photo and see exactly how it fuels your next session",
          image: "feature-card-levelup-nutrition.png",
        },
        schedule: {
          order: 3,
          body: "Kano knows when you're free and makes sure you never miss a session",
          image: "feature-card-schedule.png",
        },
      },
    },
  };

  const applyGoal = (goalKey) => {
    const config = GOAL_CONFIG[goalKey];
    if (!config) return;

    const featuresTitle = document.querySelector(".features__title");
    const featuresSubtext = document.querySelector(".features__subtext");
    if (featuresTitle) featuresTitle.innerHTML = config.title;
    if (featuresSubtext) featuresSubtext.textContent = config.subtext;

    const closingLead = document.querySelector(".closing-cta__subtext-lead");
    if (closingLead && config.closingLead) {
      closingLead.textContent = config.closingLead;
    }

    Object.entries(config.cards).forEach(([id, def]) => {
      const card = document.querySelector(`[data-card="${id}"]`);
      if (!card) return;
      card.style.order = String(def.order);
      const body = card.querySelector(".feature-card__body");
      if (body) body.textContent = def.body;
      const img = card.querySelector(".feature-card__image img");
      if (img) {
        if (!img.dataset.defaultSrc) img.dataset.defaultSrc = img.src;
        img.src = def.image
          ? `/static/images/${def.image}`
          : img.dataset.defaultSrc;
      }
    });

    if (config.person) setCompareProfile(config.person);
  };

  document.querySelectorAll('input[name="goal"]').forEach((input) => {
    input.addEventListener("change", () => {
      if (!input.checked) return;
      document.body.classList.remove("is-locked");
      applyGoal(input.value);
    });
  });

  // Apply whichever goal radio starts out checked (build-muscle by default)
  // so the rest of the page renders against the correct profile on first paint.
  const initialGoal = document.querySelector('input[name="goal"]:checked');
  if (initialGoal) {
    document.body.classList.remove("is-locked");
    applyGoal(initialGoal.value);
  }
})();
