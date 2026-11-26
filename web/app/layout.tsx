import type { Metadata } from "next";
import { connection } from "next/server";
import { Nav } from "@/components/Nav.tsx";
import "./globals.css";

export const metadata: Metadata = {
  title: "Tula Insights",
  description: "What pet owners are asking, deduplicated and ranked daily.",
  robots: { index: false, follow: false },
};

export default async function RootLayout({ children }: { children: React.ReactNode }) {
  // Read VIEW_PASSWORD per request, not once at build time.
  await connection();
  return (
    <html lang="en-ZA">
      <head>
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="" />
        <link
          rel="stylesheet"
          href="https://fonts.googleapis.com/css2?family=Poppins:wght@400;500;600;700&display=swap"
        />
      </head>
      <body>
        <header className="top">
          <div className="wrap top-inner">
            <span className="brand">Tula <span>Insights</span></span>
            <Nav gated={Boolean(process.env.VIEW_PASSWORD)} />
          </div>
        </header>
        <main className="wrap">{children}</main>
      </body>
    </html>
  );
}
