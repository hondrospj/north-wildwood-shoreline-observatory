import catalog from "../public/data/monthly-catalog.json";
import { ShorelineApp } from "./ShorelineApp";

export default function Home() {
  return <ShorelineApp catalog={catalog} />;
}
