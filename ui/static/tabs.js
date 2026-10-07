/** Tab navigation between Sentinel-1 SAR and Sentinel-2 U-Net (isolated views). */

const TAB_IDS = ["s1", "s2"];

function showTab(tabId) {
  const id = TAB_IDS.includes(tabId) ? tabId : "s1";
  for (const name of TAB_IDS) {
    const view = document.getElementById(`view-${name}`);
    const btn = document.getElementById(`tab-${name}`);
    const on = name === id;
    if (view) {
      view.hidden = !on;
      view.classList.toggle("is-active", on);
    }
    if (btn) {
      btn.classList.toggle("is-active", on);
      btn.setAttribute("aria-selected", on ? "true" : "false");
    }
  }
  if (location.hash !== `#${id}`) {
    history.replaceState(null, "", `#${id}`);
  }
  requestAnimationFrame(() => {
    if (id === "s1" && typeof window.invalidateS1Map === "function") {
      window.invalidateS1Map();
    }
    if (id === "s2" && typeof window.initS2Map === "function") {
      window.initS2Map();
    }
  });
}

document.querySelectorAll(".tab-btn").forEach((btn) => {
  btn.addEventListener("click", () => showTab(btn.dataset.tab));
});

window.addEventListener("hashchange", () => {
  showTab(location.hash.replace("#", ""));
});

showTab(location.hash.replace("#", "") || "s1");
