import type { Metadata } from "next";
import { headers } from "next/headers";
import { Geist, Geist_Mono } from "next/font/google";
import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export async function generateMetadata(): Promise<Metadata> {
  const requestHeaders = await headers();
  const host = requestHeaders.get("x-forwarded-host") ?? requestHeaders.get("host") ?? "localhost:3000";
  const isLocal = host.startsWith("localhost") || host.startsWith("127.0.0.1") || host.startsWith("[::1]");
  const protocol = requestHeaders.get("x-forwarded-proto") ?? (isLocal ? "http" : "https");
  const origin = `${protocol}://${host}`;

  return {
    title: "North Wildwood Shoreline Observatory",
    description:
      "Draw a shoreline transect, log one monthly Sentinel-2 shoreline point at a time, and export the measurements to Excel.",
    icons: { icon: "/favicon.svg", shortcut: "/favicon.svg" },
    openGraph: {
      title: "North Wildwood Shoreline Observatory",
      description: "A minimal monthly and low-tide Sentinel-2 shoreline logger for North Wildwood.",
      type: "website",
      url: origin,
      images: [{ url: `${origin}/og.png`, width: 1200, height: 630, alt: "North Wildwood monthly Sentinel-2 shoreline logger" }],
    },
    twitter: {
      card: "summary_large_image",
      title: "North Wildwood Shoreline Observatory",
      description: "Draw a transect, log monthly shoreline points, and export the measurements to Excel.",
      images: [`${origin}/og.png`],
    },
  };
}

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body className={`${geistSans.variable} ${geistMono.variable}`}>
        {children}
      </body>
    </html>
  );
}
