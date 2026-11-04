/* ARTIFEX – German on-screen keyboard (QWERTZ) for the kiosk touch display. */
(function () {
  "use strict";
  const ROWS = {
    abc: [
      ["1", "2", "3", "4", "5", "6", "7", "8", "9", "0", "ß"],
      ["q", "w", "e", "r", "t", "z", "u", "i", "o", "p", "ü"],
      ["a", "s", "d", "f", "g", "h", "j", "k", "l", "ö", "ä"],
      ["⇧", "y", "x", "c", "v", "b", "n", "m", ",", ".", "⌫"],
      ["?123", "-", "space", "\"", "↵", "✓"],
    ],
    sym: [
      ["1", "2", "3", "4", "5", "6", "7", "8", "9", "0", "%"],
      ["@", "#", "&", "*", "(", ")", "/", "+", "=", ":", ";"],
      ["€", "°", "×", "!", "?", "'", "„", "“", "<", ">", "_"],
      ["⇧", "mm", "cm", "Ø", "²", "~", "[", "]", ",", ".", "⌫"],
      ["ABC", "-", "space", "\"", "↵", "✓"],
    ],
  };

  class OSK {
    constructor(el) {
      this.el = el; this.target = null; this.shift = false; this.layout = "abc";
      el.addEventListener("pointerdown", (e) => e.preventDefault()); // keep focus in the field
      el.addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) this.press(b.dataset.k); });
      document.addEventListener("focusin", (e) => {
        const t = e.target;
        if (this.enabled() && t.matches && t.matches("textarea, input[type=text]")) this.open(t);
      });
    }
    enabled() {
      return window.matchMedia("(pointer: coarse)").matches || new URLSearchParams(location.search).has("kiosk");
    }
    render() {
      this.el.innerHTML = ROWS[this.layout].map((r) => `<div class="r">${r.map((k) => {
        let label = k, cls = "";
        if (k === "space") { label = "Leerzeichen"; cls = "space"; }
        else if (k === "✓") { cls = "go w2"; label = "Fertig"; }
        else if (["⇧", "⌫", "?123", "ABC", "↵"].includes(k)) { cls = "w2" + (k === "⇧" && this.shift ? " on" : ""); }
        else if (this.shift && k.length === 1) label = k.toUpperCase();
        return `<button type="button" class="${cls}" data-k="${k}">${label}</button>`;
      }).join("")}</div>`).join("");
    }
    open(target) {
      this.target = target; this.render(); this.el.hidden = false; document.body.classList.add("osk-open");
      setTimeout(() => target.scrollIntoView({ block: "center", behavior: "smooth" }), 50);
    }
    close() {
      this.el.hidden = true; document.body.classList.remove("osk-open");
      if (this.target) this.target.blur();
      this.target = null;
    }
    insert(text) {
      const t = this.target; if (!t) return;
      const s = t.selectionStart ?? t.value.length, e = t.selectionEnd ?? t.value.length;
      if (t.maxLength > 0 && t.value.length - (e - s) + text.length > t.maxLength) return;
      t.value = t.value.slice(0, s) + text + t.value.slice(e);
      t.selectionStart = t.selectionEnd = s + text.length;
      t.dispatchEvent(new Event("input", { bubbles: true }));
    }
    press(k) {
      const t = this.target; if (!t) return;
      switch (k) {
        case "⇧": this.shift = !this.shift; this.render(); return;
        case "?123": this.layout = "sym"; this.render(); return;
        case "ABC": this.layout = "abc"; this.render(); return;
        case "✓": this.close(); return;
        case "space": this.insert(" "); break;
        case "↵": if (t.tagName === "TEXTAREA") this.insert("\n"); else { t.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true })); this.close(); } return;
        case "⌫": {
          const s = t.selectionStart, e = t.selectionEnd;
          if (s !== e) { t.value = t.value.slice(0, s) + t.value.slice(e); t.selectionStart = t.selectionEnd = s; }
          else if (s > 0) { t.value = t.value.slice(0, s - 1) + t.value.slice(s); t.selectionStart = t.selectionEnd = s - 1; }
          t.dispatchEvent(new Event("input", { bubbles: true }));
          return;
        }
        default:
          this.insert(this.shift && k.length === 1 ? k.toUpperCase() : k);
          if (this.shift) { this.shift = false; this.render(); }
      }
    }
  }
  window.ArtifexOSK = OSK;
})();
