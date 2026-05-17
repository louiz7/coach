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
  };

  const initCompareTabs = (group) => {
    const tabs = group.querySelectorAll("[data-compare-tab]");
    const section = group.closest("section");
    const titleName = section?.querySelector(".compare__title em");
    const subtextLead = section?.querySelector(".compare__subtext-lead");
    const subtextTail = section?.querySelector(".compare__subtext-tail");

    const activate = (tab) => {
      tabs.forEach((t) => {
        const active = t === tab;
        t.classList.toggle("is-active", active);
        t.setAttribute("aria-selected", active ? "true" : "false");
      });
      const name = tab.dataset.name;
      const achievement = tab.dataset.achievement;
      if (titleName && name) titleName.textContent = name;
      if (subtextLead && name) subtextLead.textContent = `With Kano, ${name}`;
      if (subtextTail) subtextTail.textContent = achievement || "";
    };

    tabs.forEach((tab) => tab.addEventListener("click", () => activate(tab)));
  };

  document.querySelectorAll("[data-compare]").forEach(initCompareSlider);
  document.querySelectorAll("[data-compare-tabs]").forEach(initCompareTabs);

  /* ----- Goal selector drives features + testimonial ----- */
  const GOAL_CONFIG = {
    "lose-weight": {
      person: "alex",
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
      person: "marcus",
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
      person: "luke",
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

    const personTab = document.querySelector(
      `[data-compare-tab="${config.person}"]`
    );
    if (personTab && personTab.getAttribute("aria-selected") !== "true") {
      personTab.click();
    }
  };

  document.querySelectorAll('input[name="goal"]').forEach((input) => {
    input.addEventListener("change", () => {
      if (!input.checked) return;
      document.body.classList.remove("is-locked");
      applyGoal(input.value);
    });
  });
})();
