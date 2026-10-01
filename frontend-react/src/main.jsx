import React from "react";
import "./styles.css";
import "./ws-ui.css";
import "./ws-scene.css";
import "./ws-manuscripts.css";
import "./ws-settings.css";
import "./wr-room.css";
import "./ws-shell.css";
import "./ws-home.css";
import "./wr-recovery.css";
import "./ws-library.css";
import "./ws-author.css";
import "./ws-review.css";
import "./ws-quality.css";
import "./ws-cost.css";
import "./ws-deep.css";
import "./ws-scene-design.css";
import "./ws-styleref.css";
import "./ws-fidelity.css";
import "./ws-snow.css";
import "./ws-snow-chapters.css";
import "./ws-snow-editors.css";

import { App } from "./ws-app.jsx";
import { installTestSeam } from "./ws-test-seam.js";
import ReactDOMClient from "react-dom/client";

installTestSeam(); // 只在开发态：契约 E2E 冒烟经 window.__wsStores 拿 store（生产构建里是空操作）
ReactDOMClient.createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
