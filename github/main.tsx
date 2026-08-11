import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import catalog from "../public/data/monthly-catalog.json";
import { ShorelineApp } from "../app/ShorelineApp";
import "../app/globals.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ShorelineApp catalog={catalog} />
  </StrictMode>,
);
