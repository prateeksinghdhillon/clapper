// Adds a "Clapper" tab to ComfyUI's sidebar that opens the Clapper page.
import { app } from "../../scripts/app.js";

app.registerExtension({
  name: "clapper.sidebar",
  setup() {
    const open = () => window.open("/clapper/", "clapper");
    if (app.extensionManager?.registerSidebarTab) {
      app.extensionManager.registerSidebarTab({
        id: "clapper",
        icon: "pi pi-video",
        title: "Clapper",
        tooltip: "Clapper — scenes, takes, references",
        type: "custom",
        render: (el) => {
          el.innerHTML = "";
          const wrap = document.createElement("div");
          wrap.style.cssText = "padding:16px;display:flex;flex-direction:column;gap:10px;font-size:14px";
          wrap.innerHTML = "<b>Clapper</b><span style='opacity:.75'>Make videos scene by scene: retake any scene, keep every take, extend or insert anywhere.</span>";
          const btn = document.createElement("button");
          btn.textContent = "Open Clapper";
          btn.style.cssText = "padding:9px 14px;border-radius:8px;border:0;background:#2f6fed;color:#fff;font-weight:600;cursor:pointer";
          btn.onclick = open;
          wrap.append(btn);
          el.append(wrap);
        },
      });
    }
  },
});
