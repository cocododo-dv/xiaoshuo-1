import React from "react";
import "./styles.css";
import "./ws-ui.css";
import "./ws-scene.css";
import "./ws-manuscripts.css";
import "./ws-settings.css";
import "./wr-redesign.css";
import "./wr-desk.css";
import "./ws-shell.css";
import "./ws-home.css";
import "./wr-recovery.css";
import "./ws-library.css";
import "./ws-author.css";
import "./ws-review.css";
import "./ws-quality.css";
import "./ws-deep.css";
import "./ws-scene-design.css";
import "./ws-styleref.css";
import "./ws-fidelity.css";
import "./ws-snow.css";

import { App } from "./ws-app.jsx";
import ReactDOMClient from "react-dom/client";

ReactDOMClient.createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
