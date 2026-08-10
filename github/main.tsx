import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import metadata from "../public/data/metadata.json";
import trend from "../public/data/trend.json";
import shorelines from "../public/data/shorelines.json";
import { ShorelineApp } from "../app/ShorelineApp";
import "../app/globals.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ShorelineApp metadata={metadata} trend={trend} shorelines={shorelines} />
  </StrictMode>,
);
