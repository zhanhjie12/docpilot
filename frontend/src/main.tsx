import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "./styles.css";

// 在 root 节点挂载整个 Web 工作台，并启用严格模式帮助发现副作用问题。
ReactDOM.createRoot(document.getElementById("root")!).render(<React.StrictMode><App /></React.StrictMode>);
