import metadata from "../public/data/metadata.json";
import trend from "../public/data/trend.json";
import shorelines from "../public/data/shorelines.json";
import { ShorelineApp } from "./ShorelineApp";

export default function Home() {
  return (
    <ShorelineApp
      metadata={metadata}
      trend={trend}
      shorelines={shorelines}
    />
  );
}
