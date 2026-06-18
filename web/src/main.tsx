// First import, side-effecting: guarantees window.localStorage exists
// before any component reads it (WebKitGTK on Linux can leave it
// absent — see the module for the full story).
import "./installStorageShim";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./index.css";
import App from "./App.tsx";
import { ErrorBoundary } from "@/components/ErrorBoundary";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </StrictMode>,
);
